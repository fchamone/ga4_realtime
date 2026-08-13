"""What `ga4.py` promises: explicit credentials, and one pair of clients.

The two things worth proving here are both about *how* the module reaches
Google rather than what it gets back. First, that it never writes to
``os.environ``: the single-property version set
``GOOGLE_APPLICATION_CREDENTIALS`` and let the libraries discover it, which
cannot survive two sites with two different keys, and a regression to it would
not fail any test that only looked at return values. Second, that the client
cache is keyed on the resolved key path, so ten sites sharing one service
account share one pair.

Every Google object is a fake. The autouse guard in `conftest.py` already
makes the real client constructors raise; the `google` fixture below replaces
them again with recording stand-ins, so a test that reached the network would
have to defeat both.
"""

import ast
import os
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from google.api_core import exceptions as gexc

from ga4_realtime import ga4
from ga4_realtime.errors import ConfigError
from ga4_realtime.ga4 import (
    FANOUT_DIMENSIONS,
    FANOUT_METRICS,
    GA4Client,
    client_pair,
)
from ga4_realtime.timeutil import floor_minute, iso_minute, utcnow

# A key file that parses as JSON but is not a service account key. The
# contents only matter to the two tests that deliberately use the real loader;
# everywhere else the loader is faked, because what a valid key looks like is
# google-auth's business and not this module's.
NOT_A_KEY = '{"hello": "world"}'


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeCredentials:
    """Stands in for whatever from_service_account_file returns."""

    def __init__(self, path: str) -> None:
        self.path = path


class FakeValue:
    def __init__(self, value: str) -> None:
        self.value = value


class FakeApiRow:
    def __init__(self, dimensions: tuple, metrics: tuple) -> None:
        self.dimension_values = [FakeValue(v) for v in dimensions]
        self.metric_values = [FakeValue(v) for v in metrics]


class FakeResponse:
    def __init__(self, rows: list, property_quota: object = None) -> None:
        self.rows = rows
        self.property_quota = property_quota


class FakeDataClient:
    def __init__(self, credentials=None) -> None:
        self.credentials = credentials
        self.requests: list = []
        self.response = FakeResponse([])
        self.error: Exception | None = None
        # Called on every request, so a test can make the clock move *during*
        # the call and prove the minutesAgo anchor was taken before it.
        self.on_request = None

    def run_realtime_report(self, request):
        self.requests.append(request)
        if self.on_request is not None:
            self.on_request()
        if self.error is not None:
            raise self.error
        return self.response


class FakeProperty:
    def __init__(self, display_name: str, time_zone: str) -> None:
        self.display_name = display_name
        self.time_zone = time_zone


class FakeAdminClient:
    def __init__(self, credentials=None) -> None:
        self.credentials = credentials
        self.calls: list[str] = []
        self.property = FakeProperty("Example Site", "America/Sao_Paulo")
        self.error: Exception | None = None

    def get_property(self, name: str):
        self.calls.append(name)
        if self.error is not None:
            raise self.error
        return self.property


@dataclass
class Google:
    """Everything the fixture recorded, in construction order."""

    data: list[FakeDataClient] = field(default_factory=list)
    admin: list[FakeAdminClient] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def empty_client_cache():
    """The cache is module-global, so it has to be emptied around each test.

    Without this, the second test to ask for a key path would be handed the
    first test's fakes -- and the cache tests would pass for the wrong reason,
    which is the failure mode a cache test exists to rule out.
    """
    ga4.clear_client_cache()
    yield
    ga4.clear_client_cache()


@pytest.fixture
def google(monkeypatch) -> Google:
    """Replace the three things `ga4.py` reaches into Google for."""
    recorded = Google()

    def fake_from_file(path, *args, **kwargs):
        recorded.keys.append(str(path))
        return FakeCredentials(str(path))

    def make_data(credentials=None, **kwargs):
        client = FakeDataClient(credentials=credentials)
        recorded.data.append(client)
        return client

    def make_admin(credentials=None, **kwargs):
        client = FakeAdminClient(credentials=credentials)
        recorded.admin.append(client)
        return client

    monkeypatch.setattr(
        ga4.service_account.Credentials,
        "from_service_account_file",
        fake_from_file,
    )
    monkeypatch.setattr(ga4, "BetaAnalyticsDataClient", make_data)
    monkeypatch.setattr(ga4, "AnalyticsAdminServiceClient", make_admin)
    return recorded


@pytest.fixture
def key_file(tmp_path) -> Path:
    """A key file that exists. Its contents are never read by the fakes."""
    path = tmp_path / "secrets" / "ga4-service-account.json"
    path.parent.mkdir(parents=True)
    path.write_text(NOT_A_KEY, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# The network guard reaches this module
# --------------------------------------------------------------------------


def test_the_conftest_guard_covers_this_module():
    """The guard lists `ga4_realtime.ga4`; until now there was nothing there.

    It patches the names *this* module bound at import time, not the library
    module, because `from google.analytics... import BetaAnalyticsDataClient`
    copies the class into this namespace and that copy is what the code calls.
    Deliberately does not use the `google` fixture, which replaces the guard
    with recording fakes.
    """
    for constructor in (
        ga4.BetaAnalyticsDataClient,
        ga4.AnalyticsAdminServiceClient,
    ):
        with pytest.raises(RuntimeError) as excinfo:
            constructor(credentials=None)
        assert "No test may reach" in str(excinfo.value)


# --------------------------------------------------------------------------
# No environment, ever
# --------------------------------------------------------------------------


def test_module_source_never_touches_os_environ():
    """Read as an AST rather than as text.

    The module docstring quotes the old
    ``os.environ["GOOGLE_APPLICATION_CREDENTIALS"]`` line to explain what it
    stopped doing, so a substring search would fail on the explanation. What
    matters is that no *code* names `os.environ`, and that the module does not
    import `os` at all.
    """
    tree = ast.parse(Path(ga4.__file__).read_text(encoding="utf-8"))
    attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert "environ" not in attributes
    assert "putenv" not in attributes

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert "os" not in imported


def test_building_a_client_leaves_the_environment_alone(
    google, key_file, monkeypatch
):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    before = dict(os.environ)

    GA4Client("mysite", "123456789", key_file)

    assert dict(os.environ) == before
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ


def test_credentials_are_passed_to_both_constructors(google, key_file):
    GA4Client("mysite", "123456789", key_file)

    assert google.keys == [str(key_file.resolve())]
    creds = google.data[0].credentials
    assert isinstance(creds, FakeCredentials)
    # The *same* credentials object on both clients, not two loads of one
    # file: that is what makes the pair a pair.
    assert google.admin[0].credentials is creds


# --------------------------------------------------------------------------
# The cache, keyed on the resolved path
# --------------------------------------------------------------------------


def test_one_key_shared_by_two_sites_builds_one_client_pair(google, key_file):
    first = GA4Client("mysite", "111", key_file)
    # The same file, spelled the way a second [[sites]] block might spell it.
    detoured = key_file.parent / ".." / key_file.parent.name / key_file.name
    second = GA4Client("clientb", "222", detoured)

    assert first._data is second._data
    assert first._admin is second._admin
    assert len(google.data) == 1
    assert len(google.admin) == 1
    # One credentials load as well, not one per site.
    assert google.keys == [str(key_file.resolve())]


def test_two_keys_get_two_client_pairs(google, key_file, tmp_path):
    other = tmp_path / "secrets" / "clientb-sa.json"
    other.write_text(NOT_A_KEY, encoding="utf-8")

    first = GA4Client("mysite", "111", key_file)
    second = GA4Client("clientb", "222", other)

    assert first._data is not second._data
    assert first._admin is not second._admin
    assert len(google.data) == 2
    assert sorted(google.keys) == sorted(
        [str(key_file.resolve()), str(other.resolve())]
    )


def test_clear_client_cache_forces_a_rebuild(google, key_file):
    first = client_pair(key_file, site="mysite")
    ga4.clear_client_cache()
    second = client_pair(key_file, site="mysite")

    assert first.data is not second.data
    assert len(google.data) == 2


# --------------------------------------------------------------------------
# Diagnostics: which file, and which site
# --------------------------------------------------------------------------


def test_missing_key_file_names_the_file_and_the_site(tmp_path):
    missing = tmp_path / "secrets" / "gone.json"

    with pytest.raises(ConfigError) as excinfo:
        GA4Client("clientb", "123456789", missing)

    message = str(excinfo.value)
    assert str(missing.resolve()) in message
    assert "clientb" in message


def test_malformed_key_file_names_the_file_and_the_site(key_file):
    # The real loader, on real JSON that is not a service account key. This is
    # the case the library reports without naming either the file it was
    # handed or the site that asked for it.
    with pytest.raises(ConfigError) as excinfo:
        GA4Client("mysite", "123456789", key_file)

    message = str(excinfo.value)
    assert str(key_file.resolve()) in message
    assert "mysite" in message
    assert "Google Cloud Console" in message


def test_a_broken_key_is_not_cached(key_file):
    """A failed load must not leave anything behind for the next caller."""
    for _ in range(2):
        with pytest.raises(ConfigError):
            GA4Client("mysite", "123456789", key_file)


# --------------------------------------------------------------------------
# fetch_property
# --------------------------------------------------------------------------


def test_fetch_property_uses_the_admin_answer(google, key_file):
    client = GA4Client("mysite", "123456789", key_file)

    info = client.fetch_property()

    assert google.admin[0].calls == ["properties/123456789"]
    assert info.id == "123456789"
    assert info.display_name == "Example Site"
    assert info.tz_name == "America/Sao_Paulo"
    assert info.tz == ZoneInfo("America/Sao_Paulo")
    assert info.warning is None


def test_permission_denied_degrades_to_utc(google, key_file):
    client = GA4Client("mysite", "123456789", key_file)
    google.admin[0].error = gexc.PermissionDenied("nope")

    info = client.fetch_property()

    assert info.tz_name == "UTC"
    assert info.tz == ZoneInfo("UTC")
    assert info.display_name == "property 123456789"
    # The explanation survives, and now says which site it is about.
    assert "mysite" in info.warning
    assert "Admin API" in info.warning
    assert "Property access management" in info.warning


def test_other_admin_failures_also_degrade_to_utc(google, key_file):
    client = GA4Client("mysite", "123456789", key_file)
    google.admin[0].error = gexc.ServiceUnavailable("later")

    info = client.fetch_property()

    assert info.tz_name == "UTC"
    assert "mysite" in info.warning


def test_timezone_override_skips_the_admin_api(google, key_file):
    client = GA4Client("mysite", "123456789", key_file)

    info = client.fetch_property("Asia/Tokyo")

    assert google.admin[0].calls == []
    assert info.tz_name == "Asia/Tokyo"
    assert info.tz == ZoneInfo("Asia/Tokyo")
    assert info.warning is None


def test_unresolvable_zone_explains_tzdata(google, key_file):
    client = GA4Client("mysite", "123456789", key_file)
    google.admin[0].property = FakeProperty("Example", "Not/AZone")

    with pytest.raises(ConfigError) as excinfo:
        client.fetch_property()

    message = str(excinfo.value)
    assert "tzdata" in message
    assert "mysite" in message
    assert "Not/AZone" in message


# --------------------------------------------------------------------------
# poll_realtime
# --------------------------------------------------------------------------


@pytest.mark.parametrize("poll_window", [1, 10, 30])
def test_poll_window_comes_from_the_caller(google, key_file, poll_window):
    client = GA4Client("mysite", "123456789", key_file)

    client.poll_realtime(poll_window)

    request = google.data[0].requests[0]
    assert len(request.minute_ranges) == 1
    assert request.minute_ranges[0].start_minutes_ago == poll_window - 1
    assert request.minute_ranges[0].end_minutes_ago == 0


def test_request_carries_the_fan_out_and_no_user_metric(google, key_file):
    client = GA4Client("mysite", "123456789", key_file)

    client.poll_realtime(10)

    request = google.data[0].requests[0]
    assert [d.name for d in request.dimensions] == list(FANOUT_DIMENSIONS)
    assert [m.name for m in request.metrics] == list(FANOUT_METRICS)
    assert "activeUsers" not in FANOUT_METRICS
    assert request.property == "properties/123456789"


def test_rows_are_placed_against_the_anchor_taken_before_the_request(
    google, key_file, monkeypatch
):
    """The clock moves *during* the call; the keys must not.

    minutesAgo is relative to the moment the server answered, so the anchor is
    read just before sending. If it were read afterwards, a request that took
    long enough to cross a minute boundary would file every row one minute
    late -- which is exactly what this fake request does.
    """
    start = floor_minute(utcnow())
    readings = iter([start, start + timedelta(minutes=1)])
    monkeypatch.setattr(ga4, "utcnow", lambda: next(readings))

    client = GA4Client("mysite", "123456789", key_file)
    data = google.data[0]
    # The second reading is consumed inside the request, standing in for a
    # call that straddles the minute boundary.
    data.on_request = lambda: ga4.utcnow()
    data.response = FakeResponse(
        [FakeApiRow(("3", "page_view", "desktop", "Brazil"), ("2", "5"))]
    )

    rows = client.poll_realtime(10)

    assert rows[0].minute_ts_utc == iso_minute(start - timedelta(minutes=3))


def test_rows_map_dimensions_metrics_and_blank_values(google, key_file):
    client = GA4Client("mysite", "123456789", key_file)
    google.data[0].response = FakeResponse(
        [
            FakeApiRow(("0", "page_view", "mobile", "Japan"), ("4", "6")),
            # Blanks come back from the API for unset dimensions, and an empty
            # metric string is not the same as a zero-length count.
            FakeApiRow(("1", "", "", ""), ("", "")),
        ],
        property_quota="quota-object",
    )

    rows = client.poll_realtime(10)

    assert len(rows) == 2
    first, second = rows
    assert (first.event_name, first.device, first.country) == (
        "page_view",
        "mobile",
        "Japan",
    )
    assert (first.screen_page_views, first.event_count) == (4, 6)
    assert (second.event_name, second.device, second.country) == (
        "(not set)",
        "(not set)",
        "(not set)",
    )
    assert (second.screen_page_views, second.event_count) == (0, 0)
    assert client.latest_quota == "quota-object"


def test_minute_row_key_matches_what_the_store_writes(google, key_file):
    client = GA4Client("mysite", "123456789", key_file)
    google.data[0].response = FakeResponse(
        [FakeApiRow(("0", "purchase", "desktop", "Japan"), ("0", "1"))]
    )

    row = client.poll_realtime(10)[0]

    assert row.key() == (
        row.minute_ts_utc,
        "purchase",
        "desktop",
        "Japan",
    )
