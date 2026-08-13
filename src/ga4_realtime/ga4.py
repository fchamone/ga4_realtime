"""Everything that talks to Google: the two clients, and one site's reads.

The credentials handling here is a real change from the single-property
version, not a rename. That code wrote the key's path into
``os.environ["GOOGLE_APPLICATION_CREDENTIALS"]`` and let the Google libraries
discover it for themselves. The environment is process-global, so with two
sites using two different service accounts the second assignment would decide
which key *both* of them authenticated with -- and it would do it silently,
because the first site's requests would keep working right up until the key
that answered them stopped being the key it was configured with. So the
credentials are loaded explicitly, passed to both client constructors, and
``os.environ`` is not touched at all.

Clients are cached per **resolved credentials path**, not per site. Ten sites
sharing one service account -- which is the ordinary case, since one key can
be granted Viewer on every property an account owns -- then share one pair of
clients rather than opening ten of each.

Nothing here reads configuration. A site arrives as three plain values (its
name, its property ID and the path to its key) for the same reason the store
takes rows structurally: the module is built *under* `config.py`, and it stays
testable without one. The site name is carried only so the diagnostics can say
which site a broken key belongs to, which is the question a user with five
sites configured actually has.
"""

import threading
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google.analytics.admin import AnalyticsAdminServiceClient
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    Dimension,
    Metric,
    MinuteRange,
    RunRealtimeReportRequest,
)
from google.api_core import exceptions as gexc
from google.auth import exceptions as auth_exc
from google.oauth2 import service_account

from .errors import ConfigError
from .logging_setup import site_logger
from .timeutil import floor_minute, iso_minute, utcnow

# The Realtime API has no pagePath dimension; its only page dimension is
# unifiedScreenName, which on web returns the page *title*. On a one-page site
# that is nearly useless, so the breakdown is by eventName instead -- which is
# also what puts the events a site counts as conversions on screen live,
# minute by minute, instead of hours later.
FANOUT_DIMENSIONS = ("minutesAgo", "eventName", "deviceCategory", "country")
# activeUsers is absent on purpose, and the API enforces it: pairing that
# user-scoped metric with the event-scoped eventName dimension is rejected
# outright with "Selected dimensions and metrics cannot be queried together."
# It was never usable at this grain anyway: one visitor firing page_view,
# scroll and session_start appears in three rows of the same minute, so the
# figure could not be summed across rows any more than across minutes.
FANOUT_METRICS = ("screenPageViews", "eventCount")


class PropertyInfo(NamedTuple):
    """What the Admin API knows about one property -- or the UTC fallback.

    `warning` carries the reason the timezone is UTC when the Admin API could
    not be asked, and is None when it answered. It is returned rather than
    printed because this module may run while the Live region owns the
    terminal: only the caller knows whether stdout is still a plain terminal
    (priming, `doctor`) or a frame Rich is repainting. The line is logged here
    either way, so the file always has it.
    """

    id: str
    display_name: str
    tz_name: str
    tz: ZoneInfo
    warning: str | None = None


@dataclass(frozen=True)
class MinuteRow:
    """One fan-out row: a single minute, event, device and country.

    No `site` field. The site is what the caller is writing *as* and is passed
    once to `RealtimeStore.upsert_minutes`, rather than repeated on every row
    the API was never told about it.
    """

    minute_ts_utc: str
    event_name: str
    device: str
    country: str
    # Only additive metrics live at this grain. There is deliberately no
    # active_users field: it cannot be summed across minutes *or* across the
    # rows of one minute, so carrying it here would only invite a wrong SUM
    # further down -- and there is no column to put it in either.
    screen_page_views: int
    event_count: int

    def key(self) -> tuple[str, str, str, str]:
        return (self.minute_ts_utc, self.event_name, self.device, self.country)


class ClientPair(NamedTuple):
    """The two Google clients built from one service-account key."""

    data: BetaAnalyticsDataClient
    admin: AnalyticsAdminServiceClient


# The cache, keyed by the resolved key path. Module-global because the sharing
# it exists for is process-wide: two sites configured with the same key are
# two different objects in two different threads, and nothing else is in a
# position to notice they authenticate identically.
_CLIENTS: dict[Path, ClientPair] = {}
# Held across the construction, not merely around the dict lookup. Startup
# primes every site at once in a short-lived thread pool, so without the lock
# two threads racing on the same key both miss, both construct, and one of the
# two pairs is discarded seconds after being built -- the very duplication the
# cache exists to prevent.
_CLIENTS_LOCK = threading.Lock()


def clear_client_cache() -> None:
    """Forget every cached client pair.

    For tests, and for anything that reloads configuration inside one process.
    The clients are not closed: a gapic client owns a transport that closes
    itself when it is collected, and closing one still referenced by a running
    poller would break a thread this function cannot see.
    """
    with _CLIENTS_LOCK:
        _CLIENTS.clear()


def _load_credentials(path: Path, site: str):
    """Read a service-account key, or say which file and which site.

    This is where a truncated, wrong-format or wrong-kind-of-JSON key fails --
    at the file, rather than at the first request with a message from the
    library that names neither the file it was handed nor the site that asked
    for it. With N sites in one config, "which of these keys" is the entire
    question.
    """
    try:
        return service_account.Credentials.from_service_account_file(str(path))
    except (OSError, ValueError, auth_exc.GoogleAuthError) as exc:
        # Three unrelated failures land in the same place and deserve the same
        # sentence: the file is gone (OSError), it is not JSON at all
        # (ValueError from the decoder), or it is JSON but not a service
        # account key (MalformedError, which is both).
        raise ConfigError(
            f"the service account key for site {site!r} could not be "
            f"loaded:\n  {path}\n"
            "It must be the JSON key downloaded from Google Cloud Console "
            "-> IAM -> Service accounts -> Keys -> Add key -> JSON, used "
            "as-is.\n"
            f"Details: {exc}"
        ) from None


def client_pair(credentials: Path, *, site: str) -> ClientPair:
    """The Data and Admin clients for one key, built once and shared.

    Keyed on the *resolved* path so two sites naming the same file by
    different spellings -- one relative to the config file, one absolute --
    share a pair rather than each opening their own.

    Both clients are built together even though a poller only ever uses the
    Data one: they come from a single credentials load, the pair is what the
    cache holds, and a gapic client opens nothing until its first request, so
    an unused Admin client costs a few objects and no connection.

    The clients are then used from several threads at once -- one poller per
    site, all sharing this pair. That is supported: the generated clients are
    thread-safe, and their transport multiplexes concurrent calls.
    """
    resolved = Path(credentials).resolve()
    with _CLIENTS_LOCK:
        cached = _CLIENTS.get(resolved)
        if cached is not None:
            return cached
        creds = _load_credentials(resolved, site)
        # Explicit credentials on both constructors. Neither reads
        # GOOGLE_APPLICATION_CREDENTIALS this way, which is what makes
        # per-site keys possible at all.
        pair = ClientPair(
            data=BetaAnalyticsDataClient(credentials=creds),
            admin=AnalyticsAdminServiceClient(credentials=creds),
        )
        _CLIENTS[resolved] = pair
        return pair


class GA4Client:
    """One site's view of the API. Owned by that site's polling thread."""

    def __init__(self, site: str, property_id: str, credentials: Path) -> None:
        self.site = site
        self.property_id = property_id
        self.resource = f"properties/{property_id}"
        # Resolved here as well as in the cache, so `doctor` and every error
        # message name the same file the cache was keyed on rather than
        # whichever spelling the config happened to use.
        self.credentials_path = Path(credentials).resolve()
        self.log = site_logger(site)
        clients = client_pair(self.credentials_path, site=site)
        self._data = clients.data
        self._admin = clients.admin
        self.latest_quota = None

    # -- Admin API ---------------------------------------------------------

    def fetch_property(self, tz_override: str | None = None) -> PropertyInfo:
        """Look up display name and timezone, degrading to UTC if refused.

        The Admin API is a separate API from the Data API and needs to be
        enabled separately, so a service account that can read reports
        perfectly well may still be refused here. That should cost the header
        a nice label, not the whole dashboard -- and with several sites
        configured it must cost only *this* site's label.
        """
        display_name = f"property {self.property_id}"
        tz_name = tz_override or "UTC"
        warning: str | None = None
        if tz_override is None:
            try:
                prop = self._admin.get_property(name=self.resource)
                display_name = prop.display_name or display_name
                tz_name = prop.time_zone or "UTC"
            except gexc.PermissionDenied:
                warning = (
                    f"site {self.site!r}: cannot read the property's "
                    "timezone.\n"
                    "  Enable the Google Analytics Admin API in the same GCP "
                    "project, and confirm the service account has Viewer in "
                    "GA4 -> Admin -> Property access management.\n"
                    "  Falling back to UTC for day boundaries; override with "
                    "--timezone or set `timezone` for this site."
                )
                self.log.warning(
                    "Admin API refused property %s; falling back to UTC",
                    self.property_id,
                )
            except gexc.GoogleAPICallError as exc:
                warning = (
                    f"site {self.site!r}: Admin API lookup failed "
                    f"({exc.__class__.__name__}); falling back to UTC."
                )
                self.log.warning("Admin API call failed: %s", exc)

        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            # Windows ships no IANA database, so this is what a missing tzdata
            # package looks like. Say so rather than leaving the user to
            # guess -- and name the site, because the zone that failed to
            # resolve may well be one the *API* returned rather than anything
            # in the config file.
            raise ConfigError(
                f"timezone {tz_name!r} for site {self.site!r} could not be "
                "resolved.\n"
                "If the name is right, Python has no system timezone "
                "database on this machine -- Windows ships none. Install the "
                "data package:\n"
                "  python -m pip install tzdata"
            ) from None

        return PropertyInfo(
            self.property_id, display_name, tz_name, tz, warning
        )

    # -- Data API ----------------------------------------------------------

    def _minute_range(self, poll_window: int) -> MinuteRange:
        # One range, not ten. The Data API accepts at most two minute ranges
        # per request; per-minute attribution comes from the minutesAgo
        # dimension instead, which is exact and costs nothing.
        return MinuteRange(
            start_minutes_ago=poll_window - 1, end_minutes_ago=0
        )

    def poll_realtime(self, poll_window: int) -> list[MinuteRow]:
        """Read the last `poll_window` minutes as fan-out rows.

        The window is the site's own, passed in on every call rather than read
        from a module constant: it is per-site configuration now, and it is
        deliberately wider than that site's interval so late-arriving events
        and skipped cycles are re-read and corrected rather than lost. A
        default here would be a second place for that number to live, and the
        one a caller forgot to pass would be silently wrong rather than loud.
        """
        request = RunRealtimeReportRequest(
            property=self.resource,
            dimensions=[Dimension(name=n) for n in FANOUT_DIMENSIONS],
            metrics=[Metric(name=n) for n in FANOUT_METRICS],
            minute_ranges=[self._minute_range(poll_window)],
            return_property_quota=True,
        )
        # Anchor before the call, not after: minutesAgo is relative to when
        # the server processed the request, so the closest local clock reading
        # is the one taken just before sending. A request that straddles a
        # minute boundary can still misplace a row by one minute, but the
        # poll window re-reads and corrects it on the next poll.
        anchor = floor_minute(utcnow())
        response = self._data.run_realtime_report(request)
        self.latest_quota = response.property_quota

        rows: list[MinuteRow] = []
        for row in response.rows:
            values = [dv.value for dv in row.dimension_values]
            metrics = [int(mv.value or 0) for mv in row.metric_values]
            minute_ts = anchor - timedelta(minutes=int(values[0]))
            rows.append(
                MinuteRow(
                    minute_ts_utc=iso_minute(minute_ts),
                    event_name=values[1] or "(not set)",
                    device=values[2] or "(not set)",
                    country=values[3] or "(not set)",
                    screen_page_views=metrics[0],
                    event_count=metrics[1],
                )
            )
        return rows
