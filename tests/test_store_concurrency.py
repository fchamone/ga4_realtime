"""N writers, one WAL file: the contention multi-site introduces.

A single property meant a single poller thread, so there was exactly one
writer and nothing here could go wrong. N properties means N poller threads,
each with its own connection, and WAL allows any number of readers but only
one writer at a time. What keeps that from raising is the busy handler
`sqlite3.connect(timeout=10)` installs inside `RealtimeStore`: a collision
becomes a few milliseconds of waiting instead of
`sqlite3.OperationalError: database is locked`.

That is a claim about a library default, and the failure it guards against is
the kind nobody reproduces on demand -- an occasional lost poll, hours into a
run, on a machine nobody is watching. It would be blamed on the API, because
an empty minute usually does mean the API returned nothing. So the claim is
asserted rather than assumed.

Every writer here uses the *same* minute keys, event, device and country and
differs only in the site it writes as. Identical keys put all N threads on the
same pages of the clustered index in the same instant, which is the worst case
the busy handler has to absorb; and because each site's page-view figure is
seeded to a different number, the totals afterwards prove site isolation held
under contention as well -- rows that leaked between sites would not add up.

The database is created on the main thread before any writer starts, and that
line is load-bearing rather than tidy setup -- it was added after the first
draft of this file failed. `PRAGMA journal_mode=WAL` on a file that is not yet
in WAL mode has to take an exclusive lock, and SQLite does **not** run the
busy handler for a journal-mode change, so `timeout=10` does not cover it:
N connections issuing that pragma at once against a brand-new file fail
immediately with `database is locked`, which `RealtimeStore` then wraps in the
ConfigError meant for a file that is not a database. Measured while writing
this, 5 to 7 of 8 simultaneous first opens failed that way on every run.
Once the file *is* in WAL mode the pragma is a no-op, and the same eight
threads serialise cleanly with the slowest statement taking tens of
milliseconds against a ten-second budget -- which is the claim §7.3 makes and
what the tests below prove.

So the first open belongs on one thread, before any poller starts. That is a
constraint on whoever starts the threads, not on the store, and it is not
asserted here: a threaded reproduction of it is a race that passed five runs
out of six on this machine, and a flaky expected-failure marker teaches the
next reader less than this paragraph does.
"""

import sqlite3
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from ga4_realtime.store import RealtimeStore

# More writers than any realistic config: the spec's examples run two to five
# sites. Contention is the whole point, so the number is chosen to exceed the
# real one rather than to match it, and eight threads cost milliseconds.
WRITERS = 8

# Each writer commits CYCLES times, one `upsert_minutes` plus one `log_poll`
# per cycle -- exactly what one poll of one site does. A single commit each
# would leave the collision to one instant that a lucky scheduler could
# serialise on its own, and the test would then pass without proving anything.
CYCLES = 12

# Fan-out rows per cycle. The real poller writes one row per
# (minute, event, device, country) across a ten-minute window; six is enough
# that each commit is a real `executemany` rather than a single-row insert.
ROWS_PER_CYCLE = 6

# Only ever reached once a writer has already died: the survivors wait this
# out instead of blocking the suite forever, and the BrokenBarrierError they
# then return is reported beside the failure that caused it.
BARRIER_TIMEOUT = 30.0

# Literal keys rather than clock-derived ones, for the reason `test_store.py`
# gives: the keys are fixed-width strings whose ordering is their whole point,
# and deriving them from `utcnow()` makes the assertions depend on the minute
# the suite happened to run in.
DAY = "2026-08-05"
START = f"{DAY}T00:00:00Z"
END = "2026-08-06T00:00:00Z"

# Every writer covers this many distinct minutes, all of them inside the range
# above, and every writer covers exactly the same ones.
MINUTES_PER_WRITER = CYCLES * ROWS_PER_CYCLE


def minute_key(cycle: int, index: int) -> str:
    """A distinct minute per (cycle, row), inside two hours of one day."""
    minute = cycle * ROWS_PER_CYCLE + index
    return f"{DAY}T{minute // 60:02d}:{minute % 60:02d}:00Z"


@dataclass(frozen=True)
class WriterOutcome:
    """What one writer thread has to say for itself when it is done."""

    site: str
    rows_written: int
    error: BaseException | None


def _write_as(
    path: Path,
    site: str,
    views: int,
    barrier: threading.Barrier,
    make_row,
) -> WriterOutcome:
    """One poller-shaped thread: its own store, its own site, real commits.

    The store is constructed *inside* the thread, because that is the rule the
    tool itself obeys: sqlite3 connections are not shareable between threads,
    so a store handed in from the outside would be testing something the
    pollers never do. The file is already in WAL mode by the time this runs
    (`_create_wal_database`), so what races here is the schema check and the
    `PRAGMA user_version` write each open performs -- both ordinary writes,
    both covered by the busy handler, and both worth racing rather than
    skipping past.

    Exceptions are returned rather than raised. Raising would reach the main
    thread through `Future.result()` with nothing to say about which site it
    came from, and the first one would hide every other writer's outcome; the
    assertions want all N.
    """
    rows_written = 0
    try:
        store = RealtimeStore(path)
    except Exception as exc:  # noqa: BLE001
        # Blind on purpose, both here and below, and for the same reason
        # `Poller.run` is: a thread that dies with an exception this file did
        # not think to name is precisely the result worth reporting. Narrowing
        # to sqlite3.Error would let anything else escape into `Future.result`
        # unlabelled, after the other writers had waited out the barrier.
        #
        # Aborting the barrier keeps that wait from happening at all: the
        # survivors get a BrokenBarrierError immediately and the run ends in
        # seconds with every outcome collected.
        barrier.abort()
        return WriterOutcome(site, 0, exc)

    try:
        # All writers stop here so their first commits land together. Without
        # it they start at whatever pace the scheduler allows, and the test
        # could pass by never having overlapped at all.
        barrier.wait(timeout=BARRIER_TIMEOUT)
        for cycle in range(CYCLES):
            rows = [
                make_row(minute_key(cycle, index), views=views, events=views)
                for index in range(ROWS_PER_CYCLE)
            ]
            inserted, updated = store.upsert_minutes(site, rows)
            store.log_poll(site, len(rows), inserted, updated)
            rows_written += len(rows)
    except Exception as exc:  # noqa: BLE001 - see the constructor above
        return WriterOutcome(site, rows_written, exc)
    finally:
        store.close()
    return WriterOutcome(site, rows_written, None)


def _site_name(index: int) -> str:
    return f"site{index:02d}"


def _expected_page_views(index: int) -> int:
    """Site `index` writes `index + 1` views in every one of its rows."""
    return (index + 1) * MINUTES_PER_WRITER


def _submit_writers(
    pool: ThreadPoolExecutor,
    store_path: Path,
    barrier: threading.Barrier,
    make_row,
) -> list[Future]:
    return [
        pool.submit(
            _write_as,
            store_path,
            _site_name(index),
            index + 1,
            barrier,
            make_row,
        )
        for index in range(WRITERS)
    ]


def _describe(outcomes: list[WriterOutcome]) -> str:
    return "; ".join(
        f"{o.site}: {o.error!r}" for o in outcomes if o.error is not None
    )


def _create_wal_database(path: Path) -> None:
    """Put the file into WAL mode from one thread, before any writer opens it.

    See the module docstring: switching a fresh file to WAL needs an exclusive
    lock that the busy handler does not cover, so N simultaneous *first* opens
    is a different failure from the one these tests are about. Done through
    `RealtimeStore` rather than a raw `sqlite3.connect` so the file the writers
    meet is the one the tool would have made, schema and `user_version`
    included.
    """
    RealtimeStore(path).close()


def test_concurrent_writers_all_commit(store_path, make_row):
    """N threads, N sites, one file: every commit lands and nothing raises.

    `max_workers` is exactly WRITERS so the barrier can be reached: a pool
    that ran them in two batches would deadlock, which would be a bug in this
    test rather than in the store, and is worth naming here so nobody trims
    the pool later.
    """
    _create_wal_database(store_path)
    barrier = threading.Barrier(WRITERS)

    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        futures = _submit_writers(pool, store_path, barrier, make_row)
        outcomes = [future.result() for future in futures]

    # Called out separately from the general failure below because this is the
    # one the test exists for. Anything else going wrong is a broken test;
    # `database is locked` is the busy handler not doing its job.
    busy = [
        o for o in outcomes if isinstance(o.error, sqlite3.OperationalError)
    ]
    assert not busy, (
        "the busy handler did not absorb the collision: " + _describe(busy)
    )
    assert not any(o.error for o in outcomes), _describe(outcomes)
    assert [o.rows_written for o in outcomes] == [MINUTES_PER_WRITER] * WRITERS

    # Read back through a store opened after every writer has closed, so what
    # is checked is what survived the commits rather than what one connection
    # still had in flight.
    reader = RealtimeStore(store_path)
    try:
        for index in range(WRITERS):
            site = _site_name(index)
            totals = reader.day_totals(site, [], START, END)
            assert totals.minutes_recorded == MINUTES_PER_WRITER, site
            assert totals.page_views == _expected_page_views(index), site
            assert totals.events == _expected_page_views(index), site
    finally:
        reader.close()

    # Read directly: the store exposes no `poll_log` read, deliberately --
    # nothing in the tool reads that table back, it is a forensic record for
    # whoever is looking at a database after the fact. This test is that
    # reader.
    conn = sqlite3.connect(store_path)
    try:
        total_rows = conn.execute(
            "SELECT COUNT(*) FROM realtime_minute"
        ).fetchone()[0]
        logged = {row[0] for row in conn.execute("SELECT site FROM poll_log")}
    finally:
        conn.close()

    assert total_rows == WRITERS * MINUTES_PER_WRITER

    # Every site logged, and none was silently replaced by another finishing
    # in the same second -- which is what `site` in poll_log's primary key is
    # there to prevent, now proved against real threads rather than two
    # sequential calls. The number of rows *per* site is deliberately not
    # asserted: the whole run fits inside a second or two, and INSERT OR
    # REPLACE on (site, ts_utc) collapses one site's same-second polls on
    # purpose, keeping the later answer.
    assert logged == {_site_name(index) for index in range(WRITERS)}


def test_reader_sees_a_growing_database_while_writers_run(
    store_path, make_row
):
    """A render must never wait on a poll, nor fail because of one.

    WAL's reason for being in this tool is that the renderer reads while the
    pollers write. The reader here holds one connection open across the whole
    burst -- the dashboard's shape, minus the ten seconds it waits between
    frames.

    A read takes no write lock, so the busy handler is not what saves this
    one: WAL is. Under a rollback journal the same reads would queue behind
    every commit rather than pass through it, which with eight writers
    committing continuously is the difference between a dashboard that redraws
    and one that stutters. That is a latency claim and this test does not time
    it; what it does assert is that the reads keep succeeding and never go
    backwards while the writes land.
    """
    site = _site_name(0)
    # WRITERS + 1: this thread waits at the barrier too, so the reads start
    # with the writes rather than after them.
    barrier = threading.Barrier(WRITERS + 1)
    # Opening the reader first is what the dashboard does -- and, per the
    # module docstring, it is also what leaves the file in WAL mode before the
    # writers arrive. There is no separate `_create_wal_database` call here
    # because this one already is that call.
    reader = RealtimeStore(store_path)
    seen: list[int] = []

    try:
        with ThreadPoolExecutor(max_workers=WRITERS) as pool:
            futures = _submit_writers(pool, store_path, barrier, make_row)
            try:
                barrier.wait(timeout=BARRIER_TIMEOUT)
            except threading.BrokenBarrierError:
                # A writer died before reaching the barrier. Fall through to
                # the outcomes below, which name the site and the error --
                # failing here instead would report only the symptom.
                pass
            while True:
                seen.append(reader.day_totals(site, [], START, END).page_views)
                if all(future.done() for future in futures):
                    break

        # Outside the `with`, so the pool has joined every writer thread and
        # every commit is durable before this read is taken.
        #
        # It cannot be folded back into the loop above. That loop reads and
        # *then* tests `done`, so a writer finishing in the gap between the
        # two strands the final read before its commits; the loop's exit
        # condition carries no correctness weight, it only stops the reading.
        # Eight writers against eight cores spin the loop often enough to hide
        # that -- but on a 4-vCPU CI runner the main thread was starved for
        # the whole burst, took its one read at barrier release and saw
        # nothing: `assert 0 == 72`. Reproducible on any machine by pinning
        # the suite to a single core (`start /affinity 1` on Windows), which
        # fails roughly one run in three.
        seen.append(reader.day_totals(site, [], START, END).page_views)
        outcomes = [future.result() for future in futures]
    finally:
        reader.close()

    assert not any(o.error for o in outcomes), _describe(outcomes)
    # Never decreasing: a read that went backwards would mean it had seen an
    # uncommitted or half-applied write.
    assert seen == sorted(seen)
    # The final read was taken after the pool joined every writer, so it is
    # the finished total however the scheduling fell out.
    assert seen[-1] == _expected_page_views(0)
