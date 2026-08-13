"""Fixtures shared by the whole suite.

Four things live here, and the first one had to be here before any other test
was written: **no test may reach Google.** Both API clients are replaced, for
every test, by something that raises when it is constructed.

The alternative is worse than it looks. A test that quietly builds a real
client does not fail -- it authenticates from whatever credentials the machine
happens to have, makes a real request, and passes. On CI, with no credentials,
the same test fails with an auth error that reads like a broken build rather
than like a test that should never have been talking to a network at all.
Failing loudly at construction turns both cases into the same, obvious
message.

The second is a temporary config directory that looks like a real one --
config file, `.env` beside it, and a service-account key stub that exists
only so `credentials` validates. It is a fixture rather than a helper each
test writes for itself because `config.load_config` resolves relative paths
against the config file's own directory, so *where* the file is written is
part of what is under test.

The third is a database with two sites of rows already in it. Every consumer
of the store -- the dashboard's height sweep, `report`, `sites` -- needs
plausible data more than it needs *particular* data, and seeding it once here
keeps those tests about what they are actually asserting.

The fourth is `FakeGA4Client`, which is what a poller polls in a suite that
may not reach Google. It is a fixture rather than a mock library because what
the poller tests need is not "was this called" but *timing*: which call
failed, when each call happened, and a way to block until the Nth one has
happened so no test ever has to sleep and hope.
"""

import importlib
import textwrap
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from google.api_core import exceptions as gexc

from ga4_realtime.ga4 import MinuteRow, PropertyInfo
from ga4_realtime.store import RealtimeStore
from ga4_realtime.timeutil import (
    day_bounds_utc,
    floor_minute,
    iso_minute,
    utcnow,
)

# Not a network error and not a ConfigError: neither is true, and either would
# invite a test to catch it and carry on. A RuntimeError naming the client and
# the rule is what a reader of the failure needs.
_GUARD_MESSAGE = (
    "{name} was constructed inside a test. No test may reach the Google "
    "Analytics API -- pass a fake client in, or monkeypatch the function "
    "under test. See tests/conftest.py."
)

# Where each client name has to be blocked. The library module is the origin,
# but patching it alone is not enough: a module doing
# `from google.analytics.data_v1beta import BetaAnalyticsDataClient` at import
# time binds the real class into its own namespace, and that binding is what
# the code under test actually calls. So every module that imports a client by
# name is listed here too.
_DATA_CLIENT_MODULES = (
    "google.analytics.data_v1beta",
    # Lands in a later task; skipped until it exists.
    "ga4_realtime.ga4",
)
_ADMIN_CLIENT_MODULES = (
    "google.analytics.admin",
    "ga4_realtime.ga4",
)


def _raiser(name: str):
    """Build a stand-in that refuses to be constructed."""

    def _refuse(*args, **kwargs):
        raise RuntimeError(_GUARD_MESSAGE.format(name=name))

    return _refuse


def _block(monkeypatch, attr: str, module_names: tuple[str, ...]) -> None:
    for module_name in module_names:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            # A module in the list that does not exist yet is not a reason to
            # fail collection of a suite that cannot be calling it either.
            continue
        if not hasattr(module, attr):
            continue
        monkeypatch.setattr(module, attr, _raiser(attr))


@pytest.fixture(autouse=True)
def no_data_api_client(monkeypatch):
    """Make constructing the Realtime API client raise."""
    _block(monkeypatch, "BetaAnalyticsDataClient", _DATA_CLIENT_MODULES)


@pytest.fixture(autouse=True)
def no_admin_api_client(monkeypatch):
    """Make constructing the Admin API client raise.

    Separate from the Data API fixture rather than folded into one, so a
    failure names which of the two APIs the test reached for.
    """
    _block(monkeypatch, "AnalyticsAdminServiceClient", _ADMIN_CLIENT_MODULES)


# --------------------------------------------------------------------------
# A config directory on disk
# --------------------------------------------------------------------------

# The name of the key stub every generated config points at. Its contents are
# never parsed here: config.py only proves the file exists and can be opened,
# because whether the JSON is a usable key is something only the Google client
# constructor can answer.
KEY_FILENAME = "ga4-service-account.json"

# A two-site config that exercises the layering rather than the minimum: the
# first site inherits everything from [defaults], the second overrides four
# keys and is disabled. Tests that need something else pass their own body to
# `write_config`; this is the shape most of them would otherwise retype.
TWO_SITE_CONFIG = """
    [defaults]
    credentials = "secrets/ga4-service-account.json"
    interval    = 300
    poll_window = 10
    conversions = ["generate_lead"]
    timezone    = ""

    [ui]
    refresh = 10
    window  = 6

    [storage]
    database = "out/ga4_realtime.db"

    [[sites]]
    name        = "mysite"
    property_id = "123456789"

    [[sites]]
    name        = "clientb"
    property_id = "987654321"
    conversions = ["purchase", "sign_up"]
    interval    = 120
    timezone    = "Asia/Tokyo"
    color       = "magenta"
"""


@pytest.fixture
def config_home(tmp_path) -> Path:
    """A directory that looks like a project carrying its own config.

    Deliberately *not* `tmp_path` itself. Several tests run with the working
    directory set somewhere else entirely to prove that relative paths in the
    config resolve against the config file rather than the cwd, and that
    proves nothing if the two are the same directory.
    """
    home = tmp_path / "project"
    (home / "secrets").mkdir(parents=True)
    (home / "secrets" / KEY_FILENAME).write_text("{}", encoding="utf-8")
    return home


@pytest.fixture
def write_config(config_home):
    """Write a config file, and optionally a `.env`, into `config_home`.

    Returns the path to the config file, which is what every entry point in
    `config.py` takes or discovers. `textwrap.dedent` is applied so test
    bodies can stay indented with the code around them.
    """

    def _write(
        body: str,
        *,
        filename: str = "ga4-realtime.toml",
        env: str | None = None,
        directory: Path | None = None,
    ) -> Path:
        target = config_home if directory is None else Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        path = target / filename
        path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
        if env is not None:
            (target / ".env").write_text(
                textwrap.dedent(env).lstrip(), encoding="utf-8"
            )
        return path

    return _write


@pytest.fixture
def tmp_config(write_config) -> Path:
    """A written, loadable two-site config. Path to the file."""
    return write_config(TWO_SITE_CONFIG)


# --------------------------------------------------------------------------
# A store, empty and seeded
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeMinuteRow:
    """A stand-in for the `MinuteRow` that `ga4.py` will produce.

    The store accepts rows structurally (`store.MinuteRowLike`) rather than by
    import, so tests can write rows before the API client exists. Frozen for
    the same reason the real one is: a row is a reading, and a reading that
    can be edited after it has been counted is a bug waiting for a place to
    happen.
    """

    minute_ts_utc: str
    event_name: str
    device: str
    country: str
    screen_page_views: int
    event_count: int

    def key(self) -> tuple[str, str, str, str]:
        return (self.minute_ts_utc, self.event_name, self.device, self.country)


# The seeded shape, spelled out so a test can assert against it by name
# instead of by magic number. The two sites deliberately mirror `tmp_config`:
# one conversion event for the first site and two for the second, in different
# timezones, so anything that reads config and database together lines up.
SEED_SITES = ("mysite", "clientb")
SEED_MINUTES = 30
SEED_DEVICES = ("desktop", "mobile")
SEED_PROPERTY_IDS = {"mysite": "123456789", "clientb": "987654321"}
SEED_TZ_NAMES = {"mysite": "America/Sao_Paulo", "clientb": "Asia/Tokyo"}
SEED_COUNTRIES = {"mysite": "Brazil", "clientb": "Japan"}
SEED_CONVERSIONS = {
    "mysite": ("generate_lead",),
    "clientb": ("purchase", "sign_up"),
}
# Every tenth minute back from the anchor carries conversions -- on *both*
# devices, and for both of the second site's events. That is what makes the
# fan-out visible: three conversion minutes per site, reached through six rows
# for the first site and twelve for the second.
SEED_CONVERSION_EVERY = 10


def seed_rows(site: str, anchor: datetime) -> list[FakeMinuteRow]:
    """Build one site's rows, counting backwards from `anchor`.

    The counts are a formula rather than a fixture file so the numbers are
    reproducible and the file stays readable; nothing depends on their exact
    values except tests that recompute them the same way.
    """
    rows: list[FakeMinuteRow] = []
    for index in range(SEED_MINUTES):
        key = iso_minute(anchor - timedelta(minutes=index))
        for offset, device in enumerate(SEED_DEVICES):
            views = (index % 4) + 1 + offset
            rows.append(
                FakeMinuteRow(
                    minute_ts_utc=key,
                    event_name="page_view",
                    device=device,
                    country=SEED_COUNTRIES[site],
                    screen_page_views=views,
                    event_count=views,
                )
            )
            if index % SEED_CONVERSION_EVERY:
                continue
            for event in SEED_CONVERSIONS[site]:
                rows.append(
                    FakeMinuteRow(
                        minute_ts_utc=key,
                        event_name=event,
                        device=device,
                        country=SEED_COUNTRIES[site],
                        # A conversion is an event, not a page view. Leaving
                        # this at 0 is also what keeps the page-view total
                        # independent of how many conversions were seeded.
                        screen_page_views=0,
                        event_count=1,
                    )
                )
    return rows


@dataclass(frozen=True)
class SeededStore:
    """An open store with rows in it, plus the ranges that select them."""

    store: RealtimeStore
    path: Path
    # The most recent minute seeded, floored. Rows run backwards from here.
    anchor: datetime

    def span(self) -> tuple[str, str]:
        """Half-open key range covering exactly the seeded minutes.

        Use this whenever the assertion is about the seeded numbers. `bounds`
        is anchored to a real clock, so within half an hour of local midnight
        it covers only part of the seed -- correct, and useless to count with.
        """
        first = self.anchor - timedelta(minutes=SEED_MINUTES - 1)
        last = self.anchor + timedelta(minutes=1)
        return (iso_minute(first), iso_minute(last))

    def bounds(self, site: str) -> tuple[str, str]:
        """The site's local day containing the anchor, as UTC keys."""
        tz = ZoneInfo(SEED_TZ_NAMES[site])
        return day_bounds_utc(self.anchor.astimezone(tz).date(), tz)

    @property
    def sites(self) -> tuple[str, ...]:
        return SEED_SITES

    def conversions(self, site: str) -> tuple[str, ...]:
        return SEED_CONVERSIONS[site]

    def tz_name(self, site: str) -> str:
        return SEED_TZ_NAMES[site]

    def property_id(self, site: str) -> str:
        return SEED_PROPERTY_IDS[site]


@pytest.fixture
def make_row():
    """Build one fan-out row, naming only the fields the test cares about.

    A fixture rather than an importable helper because `--import-mode=
    importlib` keeps the repo root off `sys.path`, so `from conftest import
    ...` inside a test module does not resolve. Everything shared has to come
    through the fixture argument list.
    """

    def _make(
        minute: str,
        event: str = "page_view",
        *,
        device: str = "desktop",
        country: str = "Brazil",
        views: int = 1,
        events: int = 1,
    ) -> FakeMinuteRow:
        return FakeMinuteRow(
            minute_ts_utc=minute,
            event_name=event,
            device=device,
            country=country,
            screen_page_views=views,
            event_count=events,
        )

    return _make


@pytest.fixture
def store_path(tmp_path) -> Path:
    """Where a test database goes: a directory that does not exist yet.

    Nested on purpose. `RealtimeStore` creates its parent directory, and a
    path directly inside `tmp_path` would never exercise that.
    """
    return tmp_path / "data" / "ga4_realtime.db"


@pytest.fixture
def store(store_path):
    """An open, empty store. Closed afterwards.

    The close matters on Windows more than anywhere else: an open connection
    keeps a handle on the file, and the -wal sidecar is not folded back into
    the database until the last one goes away.
    """
    opened = RealtimeStore(store_path)
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def seeded_store(store) -> SeededStore:
    """Two sites, thirty minutes each, and their site_meta rows.

    The anchor is the current minute rather than a fixed instant in the past,
    because "today" is what almost every consumer asks the store for: pinning
    the seed to 2026-08-05 would put every row outside the day bounds the
    dashboard computes and leave the sweep rendering an empty frame.
    """
    anchor = floor_minute(utcnow())
    for site in SEED_SITES:
        store.upsert_minutes(site, seed_rows(site, anchor))
        store.save_site(
            site,
            property_id=SEED_PROPERTY_IDS[site],
            display_name=f"{site}.example",
            tz_name=SEED_TZ_NAMES[site],
        )
    return SeededStore(store=store, path=store.path, anchor=anchor)


# --------------------------------------------------------------------------
# A GA4 client that never leaves the process
# --------------------------------------------------------------------------

# The failure a poller has to survive, and the one the API actually produces
# when it is briefly unwell. PermissionDenied is the other one that matters,
# but it belongs to priming rather than to the loop: a key that cannot read a
# property will not start reading it on the fourth retry.
DEFAULT_POLL_ERROR = gexc.ServiceUnavailable(
    "the service is currently unavailable"
)

# Stands in for a numeric GA4 property ID wherever a test does not care which
# one. Digits only, because that is the one thing `config.py` insists on.
DEFAULT_PROPERTY_ID = "123456789"


class FakeGA4Client:
    """Stands in for `ga4.GA4Client`, with no Google object behind it.

    It answers the two calls a poller and a priming pass make --
    `fetch_property` and `poll_realtime` -- and it can be told to fail on
    particular calls by number, which is how "raises on the 2nd and 3rd call,
    recovers on the 4th" gets written down.

    Everything it records is guarded by a Condition, because the tests read
    those numbers from the main thread while one or more poller threads are
    writing them. The Condition is also what `wait_for_polls` waits on: a test
    that slept for "long enough" instead would be slow when it passed and
    flaky when the machine was busy.
    """

    def __init__(
        self,
        site: str,
        *,
        property_id: str = DEFAULT_PROPERTY_ID,
        display_name: str | None = None,
        tz_name: str = "America/Sao_Paulo",
        warning: str | None = None,
        fail_polls: tuple[int, ...] = (),
        poll_error: Exception | None = None,
        fetch_error: Exception | None = None,
        quota: object = "quota-object",
        on_poll=None,
        rows: list[MinuteRow] | None = None,
    ) -> None:
        self.site = site
        self.property_id = property_id
        self.display_name = display_name or f"{site}.example"
        self.tz_name = tz_name
        self.warning = warning
        # 1-based call numbers: "the 2nd and 3rd call" reads the same in the
        # test as it does in the task that asked for it.
        self.fail_polls = set(fail_polls)
        self.poll_error = poll_error
        self.fetch_error = fetch_error
        self.quota = quota
        # Called inside poll_realtime, so a test can make several clients meet
        # each other there -- which is how concurrency is proved rather than
        # assumed.
        self.on_poll = on_poll
        self._rows = rows
        self.latest_quota: object | None = None

        self._condition = threading.Condition()
        self.poll_calls = 0
        self.fetch_calls = 0
        self.poll_windows: list[int] = []
        # Monotonic, not wall clock: these are compared with each other to
        # check the stagger, and a clock that can step backwards would make
        # that comparison meaningless once a year.
        self.poll_times: list[float] = []
        self.tz_overrides: list[str | None] = []

    # -- the two calls the real client answers -----------------------------

    def fetch_property(self, tz_override: str | None = None) -> PropertyInfo:
        with self._condition:
            self.fetch_calls += 1
            self.tz_overrides.append(tz_override)
            self._condition.notify_all()
        if self.fetch_error is not None:
            raise self.fetch_error
        tz_name = tz_override or self.tz_name
        return PropertyInfo(
            self.property_id,
            self.display_name,
            tz_name,
            ZoneInfo(tz_name),
            self.warning,
        )

    def poll_realtime(self, poll_window: int) -> list[MinuteRow]:
        with self._condition:
            self.poll_calls += 1
            call = self.poll_calls
            self.poll_windows.append(poll_window)
            self.poll_times.append(time.monotonic())
            self._condition.notify_all()
        if self.on_poll is not None:
            self.on_poll()
        if call in self.fail_polls:
            raise self.poll_error or DEFAULT_POLL_ERROR
        self.latest_quota = self.quota
        return self.rows()

    # -- what it returns ---------------------------------------------------

    def rows(self) -> list[MinuteRow]:
        """Two rows in the current minute: a page view and a conversion.

        Keyed on the minute the poll happens in, so consecutive polls land on
        the same key and the store's UPSERT is exercised the way a real run
        exercises it -- correcting the same row rather than appending a new
        one every cycle.
        """
        if self._rows is not None:
            return list(self._rows)
        minute = iso_minute(floor_minute(utcnow()))
        return [
            MinuteRow(
                minute_ts_utc=minute,
                event_name="page_view",
                device="desktop",
                country="Brazil",
                screen_page_views=3,
                event_count=3,
            ),
            MinuteRow(
                minute_ts_utc=minute,
                event_name="generate_lead",
                device="desktop",
                country="Brazil",
                screen_page_views=0,
                event_count=1,
            ),
        ]

    # -- what a test waits on ----------------------------------------------

    def wait_for_polls(self, count: int, timeout: float = 5.0) -> bool:
        """Block until `poll_realtime` has been entered `count` times."""
        with self._condition:
            return self._condition.wait_for(
                lambda: self.poll_calls >= count, timeout
            )


class FakeClients:
    """A `client_factory` for `PollerSet`, and the clients it handed out.

    One registry rather than a client per test, because every interesting
    poller test has several sites in it and the assertions are about which of
    them did what. `fail_to_build` covers the site whose key is missing or
    malformed: `ga4.GA4Client` raises `ConfigError` from its constructor for
    that case, and priming has to survive it site by site.
    """

    def __init__(self) -> None:
        self.clients: dict[str, FakeGA4Client] = {}
        self.build_errors: dict[str, Exception] = {}
        self.builds: dict[str, int] = {}
        self._lock = threading.Lock()

    def configure(self, site: str, **kwargs) -> FakeGA4Client:
        """Set up (or replace) one site's client before it is asked for."""
        client = FakeGA4Client(site, **kwargs)
        self.clients[site] = client
        return client

    def fail_to_build(self, site: str, error: Exception) -> None:
        self.build_errors[site] = error

    def __getitem__(self, site: str) -> FakeGA4Client:
        """The client for a site, created now if nobody has asked yet.

        Created on demand rather than looked up, so a test can take hold of a
        client and start waiting on it *before* the poller thread that will
        use it has got round to building its own. Handing back a KeyError in
        that window would make every timing test a race with the scheduler.
        """
        with self._lock:
            return self._client_for(site)

    def _client_for(
        self, site: str, property_id: str | None = None
    ) -> FakeGA4Client:
        client = self.clients.get(site)
        if client is None:
            client = FakeGA4Client(
                site, property_id=property_id or DEFAULT_PROPERTY_ID
            )
            self.clients[site] = client
        return client

    def factory(self, site) -> FakeGA4Client:
        """The callable handed to `PollerSet(client_factory=...)`.

        Counts its calls under a lock: it is called from the priming pool and
        from every poller thread, and one of the things worth proving is that
        a poller builds its client once rather than on every cycle.
        """
        with self._lock:
            self.builds[site.name] = self.builds.get(site.name, 0) + 1
            error = self.build_errors.get(site.name)
            if error is not None:
                raise error
            return self._client_for(site.name, site.property_id)


@pytest.fixture
def fake_clients() -> FakeClients:
    """A registry of fake clients, and the factory that hands them out."""
    return FakeClients()
