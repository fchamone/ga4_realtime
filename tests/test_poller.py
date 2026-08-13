"""What `poller.py` promises: survival, isolation, and a startup that says so.

Three things are under test, and none of them is "does it fetch rows".

**The loop must survive anything.** A poller that dies on the second of two
API failures is a dashboard that quietly stops collecting while continuing to
draw yesterday's numbers, which is worse than crashing. So the failures are
scripted by call number and the assertions are about the thread still being
there afterwards.

**Priming must fail per site.** One unusable key out of three is a partial
failure, reported plainly and continued past; three out of three is an abort.
The line between those two is the whole reason `prime_all` returns results
instead of raising on the first one.

**Nothing here may take a real interval.** Every wait in this file is on a
Condition or an Event that something else sets, the backoff constants are
shrunk to milliseconds by an autouse fixture, and the two tests that do need
wall-clock time use a one-second interval and assert on ordering. A poller
test that waits out a 300-second cycle is a bug in the test, not thoroughness.
"""

import ast
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from google.api_core import exceptions as gexc

from ga4_realtime import poller as poller_module
from ga4_realtime.config import SiteConfig
from ga4_realtime.errors import ConfigError
from ga4_realtime.logging_setup import log as package_log
from ga4_realtime.logging_setup import setup_logging
from ga4_realtime.poller import (
    Poller,
    PollerSet,
    PollStatus,
    diagnose,
    stagger_delay,
)
from ga4_realtime.store import RealtimeStore

THREE_SITES = ("mysite", "clientb", "demo")


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    """Milliseconds instead of seconds, everywhere in this file.

    The backoff is `min(MAX_BACKOFF, 2 ** failures) + jitter`, so capping
    MAX_BACKOFF caps every delay the loop can choose. Tests that want to
    watch a poller *not* retry raise it again locally.
    """
    monkeypatch.setattr(poller_module, "MAX_BACKOFF", 0.02)
    monkeypatch.setattr(poller_module, "BACKOFF_JITTER", 0.01)


@pytest.fixture(autouse=True)
def ring(tmp_path):
    """A real log file and ring buffer, restored afterwards.

    Autouse because without it the package logger has no handlers and
    logging's last-resort handler writes every poller error to stderr. It
    also gives one test something to assert against: the site prefix that
    lets the --verbose panel show all sites interleaved.
    """
    before_handlers = list(package_log.handlers)
    before_level = package_log.level
    before_propagate = package_log.propagate
    buffer = setup_logging(True, tmp_path / "logs" / "ga4_realtime.log")
    yield buffer
    for handler in list(package_log.handlers):
        package_log.removeHandler(handler)
        # Only the handlers this fixture attached are closed; closing one
        # that was already there would break whoever owns it.
        if handler not in before_handlers:
            handler.close()
    for handler in before_handlers:
        package_log.addHandler(handler)
    package_log.setLevel(before_level)
    package_log.propagate = before_propagate


@pytest.fixture
def db_path(tmp_path) -> Path:
    """A database in a directory that does not exist yet, as on a first run."""
    return tmp_path / "data" / "ga4_realtime.db"


def make_site(
    name: str,
    *,
    interval: int = 300,
    poll_window: int = 10,
    enabled: bool = True,
    timezone: str | None = None,
    property_id: str | None = None,
) -> SiteConfig:
    """One site as `config.py` would have merged it.

    `credentials` points at nothing that exists: every test here supplies a
    client factory, and a real key file would be a fixture with no reader.
    """
    return SiteConfig(
        name=name,
        property_id=property_id or str(100000000 + len(name)),
        credentials=Path("secrets") / f"{name}.json",
        conversions=["generate_lead"],
        interval=interval,
        poll_window=poll_window,
        timezone=timezone,
        label=None,
        color="cyan",
        enabled=enabled,
    )


@pytest.fixture
def make_set(db_path, fake_clients):
    """Build a `PollerSet` on the fake clients, and stop it afterwards.

    The teardown is the point: a test that fails half way through must not
    leave a thread polling into a tmp_path pytest is about to delete.
    """
    created: list[PollerSet] = []

    def _make(sites, *, path: Path | None = None) -> PollerSet:
        pollers = PollerSet(
            sites,
            path or db_path,
            client_factory=fake_clients.factory,
        )
        created.append(pollers)
        return pollers

    yield _make
    for pollers in created:
        pollers.stop(timeout=5)


@pytest.fixture
def run_poller(db_path, fake_clients):
    """Start one bare `Poller`, and make sure it is stopped afterwards."""
    started: list[Poller] = []

    def _run(site: SiteConfig, *, first_delay: float = 0.0) -> Poller:
        poller = Poller(
            site,
            db_path,
            PollStatus(),
            threading.Event(),
            client_factory=fake_clients.factory,
            first_delay=first_delay,
        )
        started.append(poller)
        poller.start()
        return poller

    yield _run
    for poller in started:
        poller.stop_event.set()
        poller.join(timeout=5)


def wait_until(predicate, timeout: float = 5.0) -> bool:
    """Poll a predicate to a deadline. Used only where no Condition exists."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def poll_log(db_path: Path, site: str) -> list[tuple]:
    """Read one site's poll_log rows with a connection of this test's own.

    Straight sqlite3 rather than `RealtimeStore`: the store deliberately has
    no poll_log reader, since nothing in the tool reads that table back, and
    adding one for a test would put a method in the shipped API that only
    tests call.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT rows_fetched, rows_inserted, error FROM poll_log "
            "WHERE site = ? ORDER BY ts_utc",
            (site,),
        ).fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# No UI dependency
# --------------------------------------------------------------------------


def test_the_module_imports_nothing_from_ui_or_commands():
    """A future headless `collect` has to be able to reuse this file whole.

    Read as an AST rather than as text, because the module docstring says the
    words "ui" and "commands" while explaining the rule it obeys. What
    matters is that no *import* names them -- nor `rich` or `plotext`, which
    would smuggle the same dependency in one layer down.
    """
    tree = ast.parse(Path(poller_module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # A relative import has module=None at level 1 for `from . import
            # x`; the dots are kept so ".ui" and "ui" both reduce the same
            # way below.
            imported.add("." * node.level + (node.module or ""))

    forbidden = {"ui", "commands", "rich", "plotext"}
    for name in imported:
        parts = name.lstrip(".").split(".")
        assert not forbidden.intersection(parts), (
            f"poller.py imports {name!r}; PollerSet must stay usable "
            "without a terminal."
        )


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


def test_the_loop_uses_the_sites_window_and_builds_one_client(
    run_poller, fake_clients
):
    site = make_site("mysite", interval=1, poll_window=7)

    poller = run_poller(site)
    assert fake_clients["mysite"].wait_for_polls(2, timeout=5)

    # The site's own poll_window on every call, never a module constant.
    assert fake_clients["mysite"].poll_windows[:2] == [7, 7]
    # Built lazily, inside the loop -- and then kept, not rebuilt per cycle.
    assert fake_clients.builds["mysite"] == 1
    assert poller.is_alive()


def test_a_failed_poll_is_recorded_in_status_and_in_poll_log(
    run_poller, fake_clients, db_path, monkeypatch
):
    """One failure, and then nothing: the retry is held off deliberately.

    `poll_log`'s primary key is (site, ts_utc) at one-second resolution, so a
    retry landing in the same second would replace the row this test is
    about. Restoring a real backoff keeps the failure alone in the table
    without making the test wait for it -- stopping the poller is what ends
    the wait.
    """
    monkeypatch.setattr(poller_module, "MAX_BACKOFF", 30)
    site = make_site("mysite", interval=1)
    fake_clients.configure("mysite", fail_polls=(1,))

    poller = run_poller(site)
    assert wait_until(lambda: poller.status.error_count == 1)

    snapshot = poller.status.snapshot()
    assert snapshot["error_count"] == 1
    assert snapshot["poll_count"] == 0
    assert snapshot["last_success"] is None
    assert "ServiceUnavailable" in snapshot["last_error"]
    # in_flight is cleared on the way out of a failed poll as well as a
    # successful one; a status stuck at True would show a permanent spinner.
    assert snapshot["in_flight"] is False
    assert poller.is_alive()

    poller.stop_event.set()
    poller.join(timeout=5)
    rows = poll_log(db_path, "mysite")
    assert len(rows) == 1
    fetched, inserted, error = rows[0]
    assert (fetched, inserted) == (0, 0)
    assert "ServiceUnavailable" in error


def test_the_loop_survives_two_failures_in_a_row_and_recovers(
    run_poller, fake_clients, ring
):
    """The 2nd and 3rd calls raise; the 4th must still happen."""
    site = make_site("mysite", interval=1)
    fake_clients.configure("mysite", fail_polls=(2, 3))

    poller = run_poller(site)
    assert fake_clients["mysite"].wait_for_polls(4, timeout=10)
    assert wait_until(lambda: poller.status.poll_count == 2)

    snapshot = poller.status.snapshot()
    assert poller.is_alive()
    assert snapshot["poll_count"] == 2
    assert snapshot["error_count"] == 2
    # Cleared by the recovery, which is what makes the status strip go back
    # to a healthy marker rather than staying red for the rest of the run.
    assert snapshot["last_error"] is None
    assert snapshot["last_success"] is not None
    assert snapshot["quota"] == "quota-object"

    # Every poller line carries its site, so the --verbose panel can show
    # several sites interleaved and still be read.
    assert any("[mysite] poll failed" in line for line in ring.records)


def test_stop_ends_the_thread_without_waiting_out_the_interval(
    run_poller, fake_clients
):
    """Five minutes between polls, and a quit that takes milliseconds."""
    site = make_site("mysite", interval=300)

    poller = run_poller(site)
    assert fake_clients["mysite"].wait_for_polls(1, timeout=5)

    started = time.monotonic()
    poller.stop_event.set()
    poller.join(timeout=5)
    elapsed = time.monotonic() - started

    assert not poller.is_alive()
    assert elapsed < 2, f"joining took {elapsed:.1f}s of a 300s interval"


def test_a_stagger_that_has_not_elapsed_does_not_delay_a_stop(
    run_poller, fake_clients
):
    """Site 12 of 12 is minutes away from its first poll when `q` is pressed."""
    site = make_site("demo", interval=300)

    poller = run_poller(site, first_delay=120)
    started = time.monotonic()
    poller.stop_event.set()
    poller.join(timeout=5)

    assert not poller.is_alive()
    assert time.monotonic() - started < 2
    assert fake_clients.builds.get("demo") is None


def test_a_database_that_cannot_be_opened_ends_the_thread_quietly(
    fake_clients, tmp_path
):
    """An exception escaping a thread would print into the Live region.

    threading.excepthook writes a traceback to stderr, which is the frame the
    main thread is repainting. So the store open -- the one statement outside
    the loop's own try -- is caught too, and the thread ends having recorded
    the reason where the UI can find it.
    """
    not_a_database = tmp_path / "notes.txt"
    not_a_database.write_text("this is not a database", encoding="utf-8")
    site = make_site("mysite")
    poller = Poller(
        site,
        not_a_database,
        PollStatus(),
        threading.Event(),
        client_factory=fake_clients.factory,
    )

    poller.start()
    poller.join(timeout=5)

    assert not poller.is_alive()
    assert poller.status.error_count == 1
    assert "ConfigError" in poller.status.last_error


# --------------------------------------------------------------------------
# The stagger
# --------------------------------------------------------------------------


def test_stagger_offsets_site_i_of_n_by_i_interval_over_n():
    delays = [stagger_delay(i, 300, 5) for i in range(5)]

    assert delays == [0.0, 60.0, 120.0, 180.0, 240.0]


def test_a_single_site_is_never_delayed():
    assert stagger_delay(0, 300, 1) == 0.0
    # The first site of any set polls as soon as the set starts: the offset
    # is measured from there, not from one interval later.
    assert stagger_delay(0, 300, 12) == 0.0


def test_each_site_is_offset_by_a_fraction_of_its_own_interval():
    """Mixed intervals have no single cycle length to divide.

    A site polling every 60 s must not be pushed a quarter of another site's
    300 s cycle -- that would delay its first poll by more than its own
    interval.
    """
    assert stagger_delay(1, 60, 4) == 15.0
    assert stagger_delay(1, 300, 4) == 75.0


def test_started_pollers_fire_in_stagger_order(make_set, fake_clients):
    """Three sites, one second apart in theory; in order, in practice."""
    sites = [make_site(name, interval=1) for name in THREE_SITES]
    pollers = make_set(sites)

    pollers.start()
    firsts = []
    for name in THREE_SITES:
        assert fake_clients[name].wait_for_polls(1, timeout=5)
        firsts.append(fake_clients[name].poll_times[0])

    # 0, 1/3 and 2/3 of a second. The assertion is on ordering and a
    # generous floor rather than on the exact offsets: the sleep is a
    # scheduling hint, and Windows rounds it to its timer granularity.
    assert firsts[1] - firsts[0] >= 0.2
    assert firsts[2] - firsts[1] >= 0.2
    assert firsts[2] - firsts[0] < 3


def test_disabled_sites_get_no_thread_and_no_status(make_set, fake_clients):
    sites = [
        make_site("mysite"),
        make_site("clientb", enabled=False),
    ]
    pollers = make_set(sites)

    pollers.start()

    assert [site.name for site in pollers.sites] == ["mysite"]
    assert list(pollers.statuses) == ["mysite"]
    assert [thread.site.name for thread in pollers.threads] == ["mysite"]
    assert "clientb" not in fake_clients.clients


def test_start_twice_refuses(make_set):
    pollers = make_set([make_site("mysite")])
    pollers.start()

    with pytest.raises(RuntimeError):
        pollers.start()


# --------------------------------------------------------------------------
# prime_all
# --------------------------------------------------------------------------


def test_prime_all_polls_every_site_concurrently(make_set, fake_clients):
    """Proved with a barrier, not with a stopwatch.

    Each site's poll waits for the other two to arrive. Primed in series,
    the first one to arrive waits out the timeout, the barrier breaks, and
    all three come back as failures -- so `ok` on all three is the assertion
    that the pool really is a pool.
    """
    barrier = threading.Barrier(3, timeout=5)
    sites = [make_site(name) for name in THREE_SITES]
    for name in THREE_SITES:
        fake_clients.configure(name, on_poll=barrier.wait)
    pollers = make_set(sites)

    results = pollers.prime_all()

    assert sorted(results) == sorted(THREE_SITES)
    assert all(result.ok for result in results.values())


def test_prime_all_writes_rows_site_meta_and_a_poll_log_entry(
    make_set, db_path, fake_clients
):
    site = make_site("mysite", timezone=None)
    fake_clients.configure(
        "mysite", tz_name="Asia/Tokyo", display_name="My Site"
    )
    pollers = make_set([site])

    results = pollers.prime_all()

    assert results["mysite"].ok
    assert results["mysite"].rows == 2
    # The property is kept on the set as well, so the caller does not have to
    # unpack the results to know a site's timezone.
    assert pollers.properties["mysite"].tz_name == "Asia/Tokyo"

    store = RealtimeStore(db_path)
    try:
        meta = store.load_site("mysite")
        assert meta is not None
        assert meta.display_name == "My Site"
        assert meta.tz_name == "Asia/Tokyo"
        # A range that covers every possible key rather than a real day: the
        # keys are fixed-width UTC strings, so plain string bounds order
        # correctly and this test is about what was written, not about where
        # the site's midnight falls.
        totals = store.day_totals("mysite", ["generate_lead"], "0000", "9999")
        assert totals.page_views == 3
        assert totals.conversions == {"generate_lead": 1}
    finally:
        store.close()

    assert poll_log(db_path, "mysite") == [(2, 2, None)]
    status = pollers.statuses["mysite"].snapshot()
    assert status["poll_count"] == 1
    assert status["last_error"] is None


def test_prime_all_passes_the_sites_timezone_override(make_set, fake_clients):
    """A per-site `timezone` skips the Admin API for that site alone."""
    sites = [
        make_site("mysite", timezone="Asia/Tokyo"),
        make_site("clientb"),
    ]
    pollers = make_set(sites)

    pollers.prime_all()

    assert fake_clients["mysite"].tz_overrides == ["Asia/Tokyo"]
    assert fake_clients["clientb"].tz_overrides == [None]


def test_prime_all_carries_the_admin_api_warning(make_set, fake_clients):
    """The Admin API refused, the site works, and the caller has to say so."""
    fake_clients.configure(
        "mysite",
        tz_name="UTC",
        warning="site 'mysite': cannot read the property's timezone.",
    )
    pollers = make_set([make_site("mysite")])

    results = pollers.prime_all()

    assert results["mysite"].ok
    assert "cannot read the property's timezone" in results["mysite"].warning


def test_one_failing_site_of_three_does_not_stop_the_other_two(
    make_set, db_path, fake_clients
):
    """The whole reason `prime_all` returns results instead of raising."""
    sites = [make_site(name, property_id="123456789") for name in THREE_SITES]
    fake_clients.configure(
        "clientb",
        poll_error=gexc.PermissionDenied("caller does not have permission"),
        fail_polls=(1,),
    )
    pollers = make_set(sites)

    results = pollers.prime_all()

    assert results["mysite"].ok
    assert results["demo"].ok
    assert not results["clientb"].ok

    # The explanation is per site and names it, because "403" on its own does
    # not say which of three keys or which of three property IDs to go and
    # fix.
    message = results["clientb"].error
    assert "clientb" in message
    assert "123456789" in message
    assert "Viewer" in message
    assert "Property access management" in message
    assert "Data API" in message

    # The two that worked wrote rows; the one that failed is marked and left
    # with a thread of its own to keep trying.
    assert poll_log(db_path, "mysite") == [(2, 2, None)]
    assert pollers.statuses["clientb"].snapshot()["error_count"] == 1
    assert pollers.statuses["clientb"].snapshot()["last_success"] is None


def test_a_site_whose_key_will_not_load_is_reported_by_name(
    make_set, fake_clients
):
    """`ga4.GA4Client` raises ConfigError from its constructor for this.

    Its message already names the site and the file, so `diagnose` passes it
    through untouched rather than wrapping it in a second sentence that would
    say the site twice.
    """
    sites = [make_site(name) for name in THREE_SITES]
    fake_clients.fail_to_build(
        "demo",
        ConfigError(
            "the service account key for site 'demo' could not be loaded:\n"
            "  secrets/demo.json"
        ),
    )
    pollers = make_set(sites)

    results = pollers.prime_all()

    assert results["mysite"].ok
    assert not results["demo"].ok
    assert results["demo"].error.startswith("the service account key")
    assert results["demo"].error.count("'demo'") == 1


def test_prime_all_aborts_only_when_every_site_fails(make_set, fake_clients):
    sites = [make_site(name) for name in THREE_SITES]
    for name in THREE_SITES:
        fake_clients.configure(
            name,
            fail_polls=(1,),
            poll_error=gexc.PermissionDenied("nope"),
        )
    pollers = make_set(sites)

    with pytest.raises(ConfigError) as excinfo:
        pollers.prime_all()

    message = str(excinfo.value)
    # Every one of them, not just the first: with three broken keys the user
    # wants all three named in one pass rather than one per run.
    for name in THREE_SITES:
        assert name in message
    assert "nothing to show" in message


def test_a_stale_database_is_refused_once_before_any_site_is_polled(
    make_set, db_path, fake_clients
):
    """The version guard belongs to the file, not to each site.

    Left to the priming threads it would come back as three identical
    failures and then, being three of three, as an abort repeating the same
    paragraph three times.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        # A pre-C-0001 file: realtime_minute with no `site` column, and
        # user_version still 0.
        conn.execute("CREATE TABLE realtime_minute (minute_ts_utc TEXT)")
        conn.commit()
    finally:
        conn.close()
    sites = [make_site(name) for name in THREE_SITES]
    pollers = make_set(sites)

    with pytest.raises(ConfigError) as excinfo:
        pollers.prime_all()

    message = str(excinfo.value)
    assert str(db_path) in message
    assert message.count(str(db_path)) == 1
    assert fake_clients.clients == {}


def test_prime_all_with_no_enabled_sites_does_nothing(make_set):
    """Only reachable from a caller that built a set by hand.

    `config.py` refuses a file in which every site is disabled, so this is
    about the class not raising an IndexError when someone reuses it in a
    future headless collector.
    """
    pollers = make_set([make_site("mysite", enabled=False)])

    assert pollers.prime_all() == {}


# --------------------------------------------------------------------------
# diagnose
# --------------------------------------------------------------------------


def test_diagnose_names_the_site_for_an_unexpected_failure():
    site = make_site("clientb", property_id="987654321")

    message = diagnose(site, ValueError("something odd"))

    assert "clientb" in message
    assert "ValueError" in message
    assert "987654321" in message
