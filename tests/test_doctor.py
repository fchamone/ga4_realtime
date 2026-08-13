"""What `doctor` promises: a full diagnosis, and not one file written.

Three things are pinned here.

**It writes nothing.** The check is a directory listing taken before and
after, because that is the only assertion that would survive somebody
"tidying" the command into opening a `RealtimeStore` to report the database
size. `RealtimeStore` *creates* the file it is pointed at, and a diagnostic
that brings the very file it is describing into existence has told the user
something that was not true when they asked. The rotating log is the same
trap, one `ctx.start_logging()` away.

**It reports rather than raises.** Every other command lets a `ConfigError`
travel to `cli.main`, which prints it on stderr and exits 1. `doctor` catches
it: an unfilled `property_id` is what it exists to find, and the report has to
name the field and the file to edit and still say what it did *not* get to
check.

**It reaches the API the way a poll does.** The clients are built through
`ga4.py`, so the fakes here are installed at the Google boundary -- exactly
where `tests/test_ga4.py` installs its own -- rather than by replacing the
command's own helpers. The autouse guard in `conftest.py` already makes the
real constructors raise; a test that reached Google would have to defeat both.
"""

import ast
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from google.api_core import exceptions as gexc

from ga4_realtime import cli, ga4, paths
from ga4_realtime.commands import doctor

# One enabled site, no timezone of its own, so the Admin API is consulted --
# which is the path with the UTC fallback in it and therefore the one worth
# defaulting to.
ONE_SITE_CONFIG = """
    [defaults]
    credentials = "secrets/ga4-service-account.json"
    conversions = ["generate_lead"]
    timezone    = ""

    [storage]
    database = "out/ga4_realtime.db"

    [[sites]]
    name        = "mysite"
    property_id = "123456789"
"""

# What `init` leaves behind: everything filled in except the two fields that
# cannot be guessed. `property_id` is checked before `credentials`, so this
# one reports the property ID and the missing key file gets its own config
# below rather than hiding behind it.
UNFILLED_CONFIG = """
    [defaults]
    credentials = "secrets/ga4-service-account.json"
    conversions = ["generate_lead"]

    [storage]
    database = "out/ga4_realtime.db"

    [[sites]]
    name        = "mysite"
    property_id = ""
"""

MISSING_KEY_CONFIG = """
    [defaults]
    credentials = "secrets/not-downloaded-yet.json"
    conversions = ["generate_lead"]

    [storage]
    database = "out/ga4_realtime.db"

    [[sites]]
    name        = "mysite"
    property_id = "123456789"
"""

DISABLED_SITE_CONFIG = """
    [defaults]
    credentials = "secrets/ga4-service-account.json"
    conversions = ["generate_lead"]
    timezone    = ""

    [storage]
    database = "out/ga4_realtime.db"

    [[sites]]
    name        = "mysite"
    property_id = "123456789"

    [[sites]]
    name        = "paused"
    property_id = "987654321"
    enabled     = false
"""


# --------------------------------------------------------------------------
# Fakes, installed where `ga4.py` reaches into Google
# --------------------------------------------------------------------------


class FakeCredentials:
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
    def __init__(self, credentials=None, error=None, rows=None) -> None:
        self.credentials = credentials
        self.requests: list = []
        self.response = FakeResponse(list(rows or []))
        self.error = error

    def run_realtime_report(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


class FakeProperty:
    def __init__(self, display_name: str, time_zone: str) -> None:
        self.display_name = display_name
        self.time_zone = time_zone


class FakeAdminClient:
    def __init__(self, credentials=None, error=None) -> None:
        self.credentials = credentials
        self.calls: list[str] = []
        self.property = FakeProperty("Example Site", "America/Sao_Paulo")
        self.error = error

    def get_property(self, name: str):
        self.calls.append(name)
        if self.error is not None:
            raise self.error
        return self.property


@dataclass
class Google:
    """The fakes that were built, and what they were told to do first.

    `data_error` and `admin_error` are set *before* the command runs rather
    than on a client afterwards, because `doctor` builds its clients itself:
    by the time a test could reach into `google.data[0]`, the call it wanted
    to break has already happened.
    """

    data: list[FakeDataClient] = field(default_factory=list)
    admin: list[FakeAdminClient] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    data_error: Exception | None = None
    admin_error: Exception | None = None
    rows: list = field(
        default_factory=lambda: [
            FakeApiRow(("0", "page_view", "desktop", "Brazil"), ("2", "3"))
        ]
    )


@pytest.fixture(autouse=True)
def empty_client_cache():
    """`ga4.py` caches client pairs per key path, process-wide."""
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
        client = FakeDataClient(
            credentials=credentials,
            error=recorded.data_error,
            rows=recorded.rows,
        )
        recorded.data.append(client)
        return client

    def make_admin(credentials=None, **kwargs):
        client = FakeAdminClient(
            credentials=credentials, error=recorded.admin_error
        )
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
def one_site(write_config) -> Path:
    return write_config(ONE_SITE_CONFIG)


@pytest.fixture
def no_user_config(monkeypatch, tmp_path) -> Path:
    """Point discovery's third step at a file that is not there.

    The machine running the suite may well have a real
    `%APPDATA%\\ga4-realtime\\config.toml`, and a test about "no config
    anywhere" that quietly loaded the author's own would pass for the wrong
    reason on one machine and fail on every other.
    """
    missing = tmp_path / "nowhere" / "config.toml"
    monkeypatch.setattr(paths, "user_config_path", lambda **kwargs: missing)
    return missing


def _snapshot(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def _doctor(config: Path) -> int:
    return cli.main(["--config", str(config), "doctor"])


# --------------------------------------------------------------------------
# Nothing is written
# --------------------------------------------------------------------------


def test_doctor_creates_no_file(google, one_site, config_home, capsys):
    """The acceptance test, and the reason `database()` returns a path.

    A listing before and against a listing after: no database, no `-wal`, no
    log, and not even the `out/` directory the two would have gone in.
    """
    before = _snapshot(config_home)

    assert _doctor(one_site) == 0

    assert _snapshot(config_home) == before
    assert not (config_home / "out").exists()


def test_doctor_writes_nothing_even_when_it_fails(
    google, write_config, config_home, capsys
):
    config = write_config(UNFILLED_CONFIG)
    before = _snapshot(config_home)

    assert _doctor(config) == 1

    assert _snapshot(config_home) == before


def test_doctor_never_names_the_store():
    """Read as an AST, so the docstring may still explain the rule.

    `RealtimeStore` creates the file it is handed, which is exactly what
    `doctor` must not do -- so the command may not so much as import it. The
    module docstring says why, and a substring search would trip over the
    explanation.
    """
    tree = ast.parse(Path(doctor.__file__).read_text(encoding="utf-8"))
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            used |= {alias.name for alias in node.names}

    assert "RealtimeStore" not in used
    assert "start_logging" not in used


# --------------------------------------------------------------------------
# A working config: every check, and the paths
# --------------------------------------------------------------------------


def test_a_working_config_passes_every_check(google, one_site, capsys):
    assert _doctor(one_site) == 0

    out = capsys.readouterr().out
    for label in (
        "config",
        "database",
        "log",
        "credentials",
        "Data API",
        "Admin API",
        "timezone",
    ):
        assert label in out
    assert "FAIL" not in out


def test_it_prints_the_resolved_database_and_log_paths(
    google, one_site, config_home, capsys
):
    assert _doctor(one_site) == 0

    out = capsys.readouterr().out
    database = (config_home / "out" / "ga4_realtime.db").resolve()
    assert str(database) in out
    assert str(database.with_suffix(".log")) in out


def test_the_data_api_probe_reads_one_minute(google, one_site):
    # The smallest read the API accepts. `doctor` is asking whether the
    # property answers at all, not what it says.
    assert _doctor(one_site) == 0

    request = google.data[0].requests[0]
    assert request.property == "properties/123456789"
    assert len(request.minute_ranges) == 1
    assert request.minute_ranges[0].start_minutes_ago == 0
    assert request.minute_ranges[0].end_minutes_ago == 0


def test_the_admin_answer_is_reported(google, one_site, capsys):
    assert _doctor(one_site) == 0

    out = capsys.readouterr().out
    assert google.admin[0].calls == ["properties/123456789"]
    assert "Example Site" in out
    assert "America/Sao_Paulo" in out


# --------------------------------------------------------------------------
# Exit 1: the field, and the file to edit
# --------------------------------------------------------------------------


def test_an_unfilled_property_id_is_named_with_the_file(
    google, write_config, capsys
):
    """`doctor` immediately after `init`, which is its first real use."""
    config = write_config(UNFILLED_CONFIG)

    assert _doctor(config) == 1

    out = capsys.readouterr().out
    assert "property_id" in out
    assert str(config.resolve()) in out
    assert "FAIL" in out


def test_a_missing_key_file_is_named_with_the_file(
    google, write_config, capsys
):
    config = write_config(MISSING_KEY_CONFIG)

    assert _doctor(config) == 1

    out = capsys.readouterr().out
    assert "credentials" in out
    assert str(config.resolve()) in out


def test_doctor_after_init_names_every_unfilled_field(
    monkeypatch, tmp_path, capsys
):
    """The first real use: init, then doctor, then fill what it named.

    `load_config` stops at the first problem, which is right for the
    dashboard and would be wrong here: the starter config has two gaps, and
    a doctor that named one per run would take two rounds to say what one
    glance at the file shows. Both fields, one run.
    """
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init"]) == 0
    capsys.readouterr()

    assert cli.main(["doctor"]) == 1

    out = capsys.readouterr().out
    assert "property_id" in out
    assert "credentials" in out
    # The resolved key path, so the user knows where the file is expected.
    assert "ga4-service-account.json" in out
    assert "Nothing was written" in out


def test_with_no_config_at_all_it_names_the_places_it_looked(
    no_user_config, monkeypatch, tmp_path, capsys
):
    monkeypatch.chdir(tmp_path)

    assert cli.main(["doctor"]) == 1

    out = capsys.readouterr().out
    assert "no configuration file found" in out
    assert "ga4-realtime init" in out


def test_a_refused_data_api_names_the_site_and_exits_one(
    google, one_site, capsys
):
    google.data_error = gexc.PermissionDenied("nope")

    assert _doctor(one_site) == 1

    out = capsys.readouterr().out
    assert "mysite" in out
    assert "Property access management" in out
    assert "FAIL" in out


def test_an_auth_failure_is_reported_not_raised(google, one_site, capsys):
    """A revoked key -- RefreshError -- is not a GoogleAPICallError.

    It is the exact failure a diagnostic command exists to explain, and it
    arrives from the auth layer before the API answers, so a probe that
    caught only the API's own exceptions met it with a traceback.
    """
    google.data_error = RuntimeError("invalid_grant: Invalid JWT Signature.")

    assert _doctor(one_site) == 1

    out = capsys.readouterr().out
    assert "invalid_grant" in out
    assert "mysite" in out
    assert "Nothing was written" in out


def test_an_admin_failure_outside_the_api_is_reported_not_raised(
    google, one_site, capsys
):
    # The same class of failure on the Admin API call. `fetch_property`
    # degrades the API's refusals to UTC; what it does not catch must still
    # end as a FAIL line, not a traceback -- and exit 1 mirrors priming,
    # which marks the site failed rather than falling back.
    google.admin_error = RuntimeError("connection reset by transport")

    assert _doctor(one_site) == 1

    out = capsys.readouterr().out
    assert "Admin API" in out
    assert "connection reset" in out


# --------------------------------------------------------------------------
# The degradations that are not failures
# --------------------------------------------------------------------------


def test_the_utc_fallback_is_reported_but_exits_zero(google, one_site, capsys):
    """A refused Admin API is a degradation the tool is built to survive.

    Exit 1 is for what stops collection. Day boundaries falling back to UTC
    does not, and it is fixed in the Google console rather than in a field of
    a file -- so it is reported in full, with the fix, at exit 0.
    """
    google.admin_error = gexc.PermissionDenied("nope")

    assert _doctor(one_site) == 0

    out = capsys.readouterr().out
    assert "UTC" in out
    assert "Property access management" in out


def test_a_site_with_its_own_timezone_skips_the_admin_api(
    google, tmp_config, capsys
):
    # `clientb` pins Asia/Tokyo, so the Admin API is never asked about it at
    # run time either -- and a check that asked anyway would report a failure
    # the dashboard would never meet.
    assert cli.main(["--config", str(tmp_config), "doctor"]) == 0

    out = capsys.readouterr().out
    assert google.admin[0].calls == ["properties/123456789"]
    assert "Asia/Tokyo" in out


def test_a_disabled_site_is_listed_and_never_called(
    google, write_config, capsys
):
    config = write_config(DISABLED_SITE_CONFIG)

    assert _doctor(config) == 0

    out = capsys.readouterr().out
    assert "paused" in out
    assert "disabled" in out
    assert google.admin[0].calls == ["properties/123456789"]
    assert [r.property for r in google.data[0].requests] == [
        "properties/123456789"
    ]
