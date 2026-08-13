"""Collection: one polling thread per site, and the set that owns them.

The `Poller` keeps the shape it had when there was only one of it. It opens
its own `RealtimeStore` **inside the thread that will use it**, because
sqlite3 connections are not shareable between threads; it backs off
exponentially with jitter when the API refuses; it waits on the shared
`stop_event` rather than sleeping, so quitting never has to wait out an
interval; and it catches `Exception` around the whole poll on purpose. That
last one is not an oversight to be tidied away: the loop is what keeps the
dashboard collecting for hours unattended, and a poller that dies on an
exception nobody predicted takes the entire point of the tool with it.

What multi-site adds is `PollerSet`: N threads, one shared `stop_event`, and
one `PollStatus` per site in a dict the UI reads. `PollStatus` itself gains
nothing -- the dict key carries the site, so there is no field to keep in
sync with it.

**This module has no UI dependency, and that is deliberate.** It imports
nothing from `ui/` or `commands/`, prints nothing, and raises rather than
reports: `prime_all` hands back a `PrimeResult` per site and lets the caller
decide what a terminal is for. A future headless `collect` -- the thing that
would let collection survive closing the terminal -- can then reuse this file
whole rather than reimplementing the half of it that matters.
"""

import random
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from google.api_core import exceptions as gexc

from .config import SiteConfig
from .errors import ConfigError
from .ga4 import GA4Client, PropertyInfo
from .logging_setup import log, site_logger
from .store import RealtimeStore
from .timeutil import utcnow

# The ceiling on the exponential backoff. Five minutes is roughly the default
# interval: past that point a site that has been refused for an hour is
# retrying no less often than a healthy one polls, and the failure is a
# standing one that a shorter retry would not fix any sooner.
MAX_BACKOFF = 300
# Jitter added on top of every backoff delay. It matters less here than in a
# fleet, but it keeps a restarting process from lining its retries up with
# the previous run's -- and, now that there are N pollers, keeps two sites
# that failed together from retrying together forever after.
BACKOFF_JITTER = 1.0

# How long `PollerSet.stop()` gives the threads, in total rather than each.
# A poll in flight is a network round-trip, so the wait is real; but it is
# bounded, because a quit that hangs on an unresponsive API is a quit the
# user will do with the window close button instead.
JOIN_TIMEOUT = 15.0

# How a `PollerSet` builds one site's client. Injected rather than imported
# by the tests, which is also what keeps the network guard in conftest.py
# from having to be defeated to test a polling loop.
ClientFactory = Callable[[SiteConfig], GA4Client]


def build_client(site: SiteConfig) -> GA4Client:
    """The default factory: one `GA4Client` per site, sharing the key cache.

    Two sites configured with the same service-account key get the same pair
    of Google clients out of `ga4.client_pair`, so this is cheaper than it
    looks -- the per-site object is a thin wrapper around a shared pair.
    """
    return GA4Client(site.name, site.property_id, site.credentials)


@dataclass
class PollStatus:
    """One site's poller state, read by the UI. Every access takes the lock.

    There is no `site` field. `PollerSet` holds these in a dict keyed by site
    name, so the name is already carried by whoever has the object; a copy on
    the status would only be a second place for it to be wrong.
    """

    lock: threading.Lock = field(default_factory=threading.Lock)
    last_success: datetime | None = None
    last_error: str | None = None
    in_flight: bool = False
    poll_count: int = 0
    error_count: int = 0
    quota: object | None = None

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "last_success": self.last_success,
                "last_error": self.last_error,
                "in_flight": self.in_flight,
                "poll_count": self.poll_count,
                "error_count": self.error_count,
                "quota": self.quota,
            }


@dataclass(frozen=True)
class PrimeResult:
    """What one site's priming poll did, in a form the caller can print.

    `error` is a finished sentence rather than an exception, because the only
    thing left to do with it is show it to somebody -- and by the time the
    Live region owns stdout, nobody can. `warning` is the same for the softer
    case: the Admin API refused, the site works, and its day boundaries are
    UTC until that is fixed.
    """

    site: str
    ok: bool
    info: PropertyInfo | None = None
    rows: int = 0
    error: str | None = None
    warning: str | None = None


def stagger_delay(index: int, interval: float, count: int) -> float:
    """Site *i* of *n* starts `i * interval / n` seconds after the set does.

    Five sites left unstaggered fire five requests in the same instant every
    cycle, then nothing for five minutes. The problem that creates is **local
    write contention, not quota**: GA4's quota is charged per property, so
    five properties polled together cost each of them exactly one request
    either way. What the five share is the SQLite file, and WAL allows one
    writer at a time -- so simultaneous polls queue on the busy handler for
    no reason, having arrived together purely because they started together.

    The offset is measured from the moment the set starts, not from a full
    interval after it. A `PollerSet` started without priming -- which is what
    a headless collector would do -- must not sit silent for an entire
    interval before its first request. Site 0's offset is therefore zero and
    it re-reads the window `prime_all` has just read: the UPSERT corrects
    rather than accumulates, so that costs one request and nothing else.

    Each site's offset is a fraction of **its own** interval. With mixed
    intervals there is no single cycle length to divide, and a site polling
    every 60 s should not be pushed a quarter of somebody else's 300 s cycle.
    """
    if count <= 1 or index <= 0:
        return 0.0
    return interval * index / count


def diagnose(site: SiteConfig, exc: Exception) -> str:
    """Turn one site's startup failure into something worth reading.

    Named per site, always. With five sites configured, "403 PermissionDenied"
    on its own does not say which of the five keys, which of the five
    properties, or which of the five blocks in the config file to go and edit.
    """
    if isinstance(exc, ConfigError):
        # Already a finished explanation naming its own site and file --
        # `ga4.py` writes these. Wrapping it again would say the site twice
        # and bury the fix under the wrapper.
        return str(exc)
    if isinstance(exc, gexc.PermissionDenied):
        return (
            f"site {site.name!r}: the service account cannot read property "
            f"{site.property_id}.\n"
            "Add its e-mail (the client_email in the JSON key) as Viewer in\n"
            "GA4 -> Admin -> Property access management, and confirm the\n"
            "Google Analytics Data API is enabled in the GCP project.\n"
            f"API said: {exc.message}"
        )
    return (
        f"site {site.name!r}: {exc.__class__.__name__}: {exc}\n"
        f"(property {site.property_id}, key {site.credentials})"
    )


class Poller(threading.Thread):
    """One site's polling loop. Must never die on an API error."""

    def __init__(
        self,
        site: SiteConfig,
        db_path: Path,
        status: PollStatus,
        stop_event: threading.Event,
        *,
        client_factory: ClientFactory = build_client,
        first_delay: float = 0.0,
    ) -> None:
        # The site name is in the thread name because a stack dump of a
        # 12-site run is otherwise twelve identical frames.
        super().__init__(name=f"ga4-poller-{site.name}", daemon=True)
        self.site = site
        self.db_path = Path(db_path)
        self.status = status
        self.stop_event = stop_event
        self.client_factory = client_factory
        self.first_delay = first_delay
        self.log = site_logger(site.name)
        self._client: GA4Client | None = None

    def client(self) -> GA4Client:
        """Build this site's client on first use, then reuse it.

        Built inside the loop rather than in `__init__` so that a key file
        that has gone missing, or a config edited while the tool runs, is one
        more failure the loop backs off from instead of an exception in the
        thread's first instruction -- which nothing would catch and nowhere
        would show. `ga4.client_pair` caches per key path, so the sites that
        share a service account still share one pair of Google clients.
        """
        if self._client is None:
            self._client = self.client_factory(self.site)
        return self._client

    def run(self) -> None:
        # The stagger, and it is interruptible like every other wait here: a
        # user who quits during startup must not be held by site 12's offset.
        if self.first_delay and self.stop_event.wait(self.first_delay):
            return
        try:
            store = RealtimeStore(self.db_path)  # opened in the owning thread
        except Exception as exc:  # noqa: BLE001 - nothing above can catch it
            # An exception escaping a thread reaches threading.excepthook,
            # which writes a traceback to stderr -- into the Live region,
            # tearing the frame the main thread is repainting. So the one
            # thing that happens before the loop gets the same treatment as
            # everything inside it.
            self.log.error("could not open %s: %s", self.db_path, exc)
            with self.status.lock:
                self.status.last_error = f"{exc.__class__.__name__}: {exc}"
                self.status.error_count += 1
            return

        failures = 0
        try:
            while not self.stop_event.is_set():
                delay: float = self.site.interval
                try:
                    self.poll_once(store)
                    failures = 0
                except Exception as exc:  # noqa: BLE001 - loop must survive
                    failures += 1
                    # Exponential backoff, capped, with jitter on top -- see
                    # MAX_BACKOFF and BACKOFF_JITTER for why each is there.
                    delay = min(MAX_BACKOFF, 2**failures) + random.uniform(
                        0, BACKOFF_JITTER
                    )
                    message = f"{exc.__class__.__name__}: {exc}"
                    self.log.error(
                        "poll failed (%d in a row): %s", failures, message
                    )
                    with self.status.lock:
                        self.status.last_error = message
                        self.status.error_count += 1
                        self.status.in_flight = False
                    try:
                        store.log_poll(self.site.name, 0, 0, 0, message)
                    except sqlite3.Error:
                        self.log.exception("could not write poll_log")
                # Interruptible: a stop request must not wait out the
                # interval, and with N sites it must not wait out N of them.
                self.stop_event.wait(delay)
        finally:
            store.close()
            self.log.info("poller stopped")

    def poll_once(self, store: RealtimeStore) -> None:
        with self.status.lock:
            self.status.in_flight = True
        try:
            client = self.client()
            # The site's own window, wider than its own interval, so late
            # events and skipped cycles are re-read and corrected.
            rows = client.poll_realtime(self.site.poll_window)
            inserted, updated = store.upsert_minutes(self.site.name, rows)
            store.log_poll(self.site.name, len(rows), inserted, updated, None)
            self.log.info(
                "poll ok: %d rows (%d new, %d corrected)",
                len(rows),
                inserted,
                updated,
            )
            with self.status.lock:
                self.status.last_success = utcnow()
                self.status.last_error = None
                self.status.poll_count += 1
                self.status.quota = client.latest_quota
        finally:
            with self.status.lock:
                self.status.in_flight = False


class PollerSet:
    """The threads, the shared stop signal, and one status per site.

    Nothing here knows what a terminal is. `prime_all` returns results and
    raises `ConfigError`; the caller prints. That is what lets a future
    headless `collect` command reuse this class unchanged, and it is also why
    the priming diagnostics are strings on a dataclass rather than lines
    already written to a stream that may or may not still be a terminal.
    """

    def __init__(
        self,
        sites: Sequence[SiteConfig],
        db_path: Path,
        *,
        client_factory: ClientFactory = build_client,
    ) -> None:
        # Filtered here rather than trusted from the caller: a disabled site
        # that gets polled anyway would write rows nobody asked for, under a
        # name the dashboard never shows (D17).
        self.sites = [site for site in sites if site.enabled]
        self.db_path = Path(db_path)
        self.client_factory = client_factory
        self.stop_event = threading.Event()
        # One status per site from the start, before anything has polled, so
        # the status strip can draw every site's marker on the first frame
        # rather than growing a column as each thread reports in.
        self.statuses: dict[str, PollStatus] = {
            site.name: PollStatus() for site in self.sites
        }
        self.properties: dict[str, PropertyInfo] = {}
        self.threads: list[Poller] = []

    # -- priming -----------------------------------------------------------

    def prime_all(self) -> dict[str, PrimeResult]:
        """Poll every enabled site once, concurrently, before the UI opens.

        Concurrently, so startup costs one round-trip rather than N: twelve
        sites polled in series at a second each is twelve seconds of a blank
        terminal. The burst this creates is the exact thing `stagger_delay`
        avoids in steady state, and that is accepted -- it happens once, GA4
        quota is per property, and it exercises the SQLite busy handler
        immediately instead of at some unlucky moment hours later.

        Before the UI opens, because this is the last moment stdout is a
        plain terminal: every credential, permission and timezone failure
        gets a plain paragraph here, where the alternative is a red marker in
        a status strip and a log file to go and find.

        **Aborts only if every site failed.** One bad key must never hide two
        working sites -- the failing one starts anyway, keeps its thread, and
        wears a `x` in the strip until it recovers or the user fixes it.
        """
        if not self.sites:
            # Config refuses a file with no enabled site, so this is only
            # reachable from a caller that built a set by hand.
            return {}

        self._check_database()
        log.info("priming %d site(s)", len(self.sites))
        with ThreadPoolExecutor(
            max_workers=len(self.sites), thread_name_prefix="ga4-prime"
        ) as pool:
            results = list(pool.map(self._prime_one, self.sites))

        for result in results:
            if result.info is not None:
                self.properties[result.site] = result.info

        failed = [result for result in results if not result.ok]
        if len(failed) == len(results):
            raise ConfigError(
                "every configured site failed to start, so there is nothing "
                "to show.\n\n"
                + "\n\n".join(result.error or "" for result in failed)
            )
        return {result.site: result for result in results}

    def _check_database(self) -> None:
        """Open and close the database once, on the calling thread.

        The version guard and the "this is not a database" message belong to
        the *file*, not to any one site. Left to the priming threads, a stale
        pre-C-0001 database would come back as N identical per-site failures
        and then, being N of N, as an abort whose message repeated the same
        paragraph once per site. Opened here it raises once, plainly, and
        `prime_all` never starts.
        """
        RealtimeStore(self.db_path).close()

    def _prime_one(self, site: SiteConfig) -> PrimeResult:
        """One site's priming poll. Never raises; reports instead."""
        site_log = site_logger(site.name)
        status = self.statuses[site.name]
        store: RealtimeStore | None = None
        try:
            client = self.client_factory(site)
            # Ahead of the poll: the timezone decides which UTC keys "today"
            # spans, and `report` reads it back out of site_meta with no
            # config file and no network at all.
            info = client.fetch_property(site.timezone)
            store = RealtimeStore(self.db_path)  # this thread's own connection
            store.save_site(
                site.name,
                property_id=info.id,
                display_name=info.display_name,
                tz_name=info.tz_name,
            )
            rows = client.poll_realtime(site.poll_window)
            inserted, updated = store.upsert_minutes(site.name, rows)
            store.log_poll(site.name, len(rows), inserted, updated, None)
            with status.lock:
                status.last_success = utcnow()
                status.last_error = None
                status.poll_count += 1
                status.quota = client.latest_quota
            site_log.info("primed: %d rows", len(rows))
            return PrimeResult(
                site=site.name,
                ok=True,
                info=info,
                rows=len(rows),
                warning=info.warning,
            )
        except Exception as exc:  # noqa: BLE001 - one site, not the run
            message = diagnose(site, exc)
            site_log.error("priming failed: %s", exc)
            with status.lock:
                status.last_error = f"{exc.__class__.__name__}: {exc}"
                status.error_count += 1
            if store is not None:
                # Best effort, and only when the store opened: the failure
                # belongs in poll_log for the same reason the poller's do --
                # it is the record of a site that was tried and refused.
                try:
                    store.log_poll(site.name, 0, 0, 0, message)
                except sqlite3.Error:
                    site_log.exception("could not write poll_log")
            return PrimeResult(site=site.name, ok=False, error=message)
        finally:
            if store is not None:
                store.close()

    # -- the threads -------------------------------------------------------

    def start(self) -> None:
        """Start one thread per enabled site, staggered."""
        if self.threads:
            raise RuntimeError(
                "PollerSet.start() was called twice; build a new set instead."
            )
        count = len(self.sites)
        for index, site in enumerate(self.sites):
            poller = Poller(
                site,
                self.db_path,
                self.statuses[site.name],
                self.stop_event,
                client_factory=self.client_factory,
                first_delay=stagger_delay(index, site.interval, count),
            )
            poller.start()
            self.threads.append(poller)

    def stop(self, timeout: float = JOIN_TIMEOUT) -> None:
        """Signal every thread once, then wait for all of them together.

        One deadline shared by the whole set, not `timeout` per thread: with
        twelve sites, a per-thread timeout would let a quit take twelve times
        as long as the number the caller passed appears to promise.

        The threads are kept in `self.threads` afterwards rather than
        cleared, so a caller -- or a test -- can still ask whether they
        actually ended.
        """
        self.stop_event.set()
        deadline = time.monotonic() + timeout
        for thread in self.threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        alive = [t.site.name for t in self.threads if t.is_alive()]
        if alive:
            # Daemon threads, so the process still exits; this is a note for
            # the log file rather than something the user can act on.
            log.warning(
                "poller thread(s) still running after %.0fs: %s",
                timeout,
                ", ".join(alive),
            )
