"""The persisted format: what it accepts, what it refuses, what it totals.

Two of these tests are named in the spec's success criteria and are the reason
this module exists at all. `test_upsert_corrects_never_accumulates` is the
whole of invariant #1 -- the API returns the authoritative total for a minute
on every poll, so a second poll of the same minute must leave the totals
exactly where they were. `test_stale_database_raises_config_error` is the
guard that keeps `no such column: site` from ever being the first thing a user
sees after upgrading.

Minute keys are written as literals rather than derived from the clock.
Nothing here is about "now": the keys are fixed-width strings whose ordering
is their whole point, and a test that computes them from `utcnow()` proves
less while failing at midnight.
"""

import sqlite3

import pytest

from ga4_realtime import store as store_module
from ga4_realtime.errors import ConfigError
from ga4_realtime.store import SCHEMA_VERSION, DayTotals, RealtimeStore
from ga4_realtime.timeutil import iso_minute

DAY = "2026-08-05"
START = f"{DAY}T00:00:00Z"
END = "2026-08-06T00:00:00Z"

LEAD = "generate_lead"
PURCHASE = "purchase"


def at(hour: int, minute: int = 0) -> str:
    """A minute key inside the day the range constants cover."""
    return f"{DAY}T{hour:02d}:{minute:02d}:00Z"


# The pre-C-0001 table, reproduced exactly as it was: no `site` column, and a
# primary key of four parts rather than five. This is what sits on the
# author's disk today, and what the guard has to recognise.
LEGACY_SCHEMA = """
CREATE TABLE realtime_minute (
    minute_ts_utc     TEXT    NOT NULL,
    event_name        TEXT    NOT NULL,
    device            TEXT    NOT NULL,
    country           TEXT    NOT NULL,
    screen_page_views INTEGER NOT NULL,
    event_count       INTEGER NOT NULL,
    first_seen_utc    TEXT    NOT NULL,
    last_seen_utc     TEXT    NOT NULL,
    PRIMARY KEY (minute_ts_utc, event_name, device, country)
) WITHOUT ROWID;

CREATE TABLE realtime_users (
    minute_ts_utc TEXT PRIMARY KEY,
    active_users  INTEGER NOT NULL
);
"""


# --------------------------------------------------------------------------
# Invariant #1: the UPSERT corrects, it never accumulates
# --------------------------------------------------------------------------


def test_upsert_corrects_never_accumulates(store, make_row):
    """Polling the same minute twice must leave the totals unchanged.

    The failure this guards is silent and compounding: `x = x + excluded.x`
    doubles the minute on the second poll, and the 10-minute poll window means
    every minute is read six times at the default interval.
    """
    rows = [make_row(at(10), views=5, events=7)]

    store.upsert_minutes("mysite", rows)
    store.upsert_minutes("mysite", rows)
    store.upsert_minutes("mysite", rows)

    totals = store.day_totals("mysite", [LEAD], START, END)
    assert totals.page_views == 5
    assert totals.events == 7
    assert totals.minutes_recorded == 1


def test_upsert_takes_the_latest_value_as_authoritative(store, make_row):
    """A later poll reporting a bigger number replaces, rather than adds."""
    store.upsert_minutes("mysite", [make_row(at(10), views=5, events=7)])
    store.upsert_minutes("mysite", [make_row(at(10), views=9, events=11)])

    totals = store.day_totals("mysite", [LEAD], START, END)
    assert (totals.page_views, totals.events) == (9, 11)


def test_upsert_reports_inserts_and_corrections_separately(store, make_row):
    first = store.upsert_minutes(
        "mysite", [make_row(at(10)), make_row(at(11))]
    )
    second = store.upsert_minutes(
        "mysite", [make_row(at(11)), make_row(at(12))]
    )

    assert first == (2, 0)
    assert second == (1, 1)


def test_upsert_of_nothing_writes_nothing(store):
    assert store.upsert_minutes("mysite", []) == (0, 0)
    assert store.day_totals("mysite", [], START, END).minutes_recorded == 0


def test_upsert_counts_are_not_confused_by_another_site(store, make_row):
    """The existence probe is bounded by site, not only by minute range.

    Without the site predicate the second site's first write reads the first
    site's identical key, calls it "existing", and reports a correction that
    never happened -- plausibly enough that nobody would notice.
    """
    store.upsert_minutes("mysite", [make_row(at(10))])

    assert store.upsert_minutes("clientb", [make_row(at(10))]) == (1, 0)


# --------------------------------------------------------------------------
# Site isolation
# --------------------------------------------------------------------------


def test_two_sites_sharing_a_key_both_persist(store, make_row):
    """The same (minute, event, device, country) tuple, twice, one file."""
    store.upsert_minutes("mysite", [make_row(at(10), views=3, events=3)])
    store.upsert_minutes("clientb", [make_row(at(10), views=8, events=8)])

    mine = store.day_totals("mysite", [LEAD], START, END)
    theirs = store.day_totals("clientb", [LEAD], START, END)

    assert mine.page_views == 3
    assert theirs.page_views == 8


def test_reads_are_scoped_to_one_site(store, make_row):
    store.upsert_minutes("mysite", [make_row(at(10), "signup", events=4)])
    store.upsert_minutes("clientb", [make_row(at(10), "signup", events=6)])

    assert store.top_events("mysite", START, END) == [("signup", 4)]
    assert store.top_events("clientb", START, END) == [("signup", 6)]
    assert len(store.pageview_series("mysite", START, END)) == 1


def test_poll_log_holds_two_sites_in_the_same_second(store, monkeypatch):
    """Two pollers finishing together must both leave a row.

    With `ts_utc` alone as the primary key -- which is what the single-site
    schema had -- the second INSERT OR REPLACE overwrites the first, and the
    row most likely to vanish is the one carrying an error worth reading.
    """
    monkeypatch.setattr(
        store_module, "iso_second", lambda _moment: "2026-08-05T10:00:00Z"
    )

    store.log_poll("mysite", 10, 10, 0)
    store.log_poll("clientb", 4, 0, 4, error="PermissionDenied")

    rows = store._conn.execute(
        "SELECT site, rows_fetched, error FROM poll_log ORDER BY site"
    ).fetchall()
    assert rows == [
        ("clientb", 4, "PermissionDenied"),
        ("mysite", 10, None),
    ]


def test_poll_log_collapses_one_site_polling_twice_a_second(
    store, monkeypatch
):
    """The only collision the key still allows, and it loses nothing."""
    monkeypatch.setattr(
        store_module, "iso_second", lambda _moment: "2026-08-05T10:00:00Z"
    )

    store.log_poll("mysite", 1, 1, 0)
    store.log_poll("mysite", 2, 2, 0)

    rows = store._conn.execute("SELECT rows_fetched FROM poll_log").fetchall()
    assert rows == [(2,)]


# --------------------------------------------------------------------------
# The schema itself
# --------------------------------------------------------------------------


def _columns(store, table):
    return store._conn.execute(f"PRAGMA table_info({table})").fetchall()


def test_site_leads_every_primary_key(store):
    """Position 1 in the PK, not appended to it.

    `WITHOUT ROWID` makes the primary key the clustered index, so this is a
    statement about where a site's rows physically live, not about
    uniqueness -- appending `site` would satisfy the same constraints and
    scatter every site across the table.
    """
    for table in ("realtime_minute", "poll_log"):
        pk_order = {row[1]: row[5] for row in _columns(store, table)}
        assert pk_order["site"] == 1, table


def test_realtime_minute_is_clustered_by_that_key(store):
    sql = store._conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'realtime_minute'"
    ).fetchone()[0]
    assert "WITHOUT ROWID" in sql


def test_no_user_metric_anywhere_in_the_schema(store):
    """No `active_users` column and no `realtime_users` table, in any table.

    activeUsers cannot be summed across minutes or across the rows of one
    minute, and there is deliberately no column holding it, so there is
    nothing to sum by mistake. The orphaned table the pre-split databases
    carried does not come across either: D3 starts fresh, so there is no
    history for a drop to destroy.
    """
    tables = [
        row[0]
        for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    ]
    assert "realtime_users" not in tables
    for table in tables:
        names = [row[1] for row in _columns(store, table)]
        assert "active_users" not in names, table


def test_a_fresh_database_is_stamped_with_the_schema_version(store):
    version = store._conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION


def test_an_existing_database_reopens(store_path, store, make_row):
    store.upsert_minutes("mysite", [make_row(at(10), views=2)])

    reopened = RealtimeStore(store_path)
    try:
        totals = reopened.day_totals("mysite", [LEAD], START, END)
    finally:
        reopened.close()
    assert totals.page_views == 2


# --------------------------------------------------------------------------
# The stale-database guard
# --------------------------------------------------------------------------


def test_stale_database_raises_config_error(tmp_path):
    """A pre-C-0001 file is refused in words, before any query runs.

    `CREATE TABLE IF NOT EXISTS` is not a migration: it finds the table
    already there, adds no `site` column and succeeds, and the failure
    surfaces later as `sqlite3.OperationalError: no such column: site` from
    whichever query happened to run first.
    """
    legacy = tmp_path / "ga4_realtime.db"
    conn = sqlite3.connect(str(legacy))
    conn.executescript(LEGACY_SCHEMA)
    conn.commit()
    conn.close()

    with pytest.raises(ConfigError) as caught:
        RealtimeStore(legacy)

    message = str(caught.value)
    assert str(legacy) in message
    assert "site" in message
    assert not isinstance(caught.value, sqlite3.Error)


def test_the_guard_does_not_touch_the_file_it_refuses(tmp_path):
    """It runs before the schema script, so nothing is created or stamped."""
    legacy = tmp_path / "ga4_realtime.db"
    conn = sqlite3.connect(str(legacy))
    conn.executescript(LEGACY_SCHEMA)
    conn.commit()
    conn.close()

    with pytest.raises(ConfigError):
        RealtimeStore(legacy)

    conn = sqlite3.connect(str(legacy))
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        ]
    finally:
        conn.close()
    assert version == 0
    assert "site_meta" not in tables


def test_a_newer_schema_version_is_refused_just_as_plainly(store_path):
    """Refused, not silently downgraded: the columns could mean anything."""
    RealtimeStore(store_path).close()
    conn = sqlite3.connect(str(store_path))
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()

    with pytest.raises(ConfigError) as caught:
        RealtimeStore(store_path)

    message = str(caught.value)
    assert str(store_path) in message
    assert "99" in message
    assert str(SCHEMA_VERSION) in message
    assert not isinstance(caught.value, sqlite3.Error)


def test_a_file_that_is_not_a_database_is_named_as_such(tmp_path):
    not_a_db = tmp_path / "notes.txt"
    not_a_db.write_text("this is not a database\n", encoding="utf-8")

    with pytest.raises(ConfigError) as caught:
        RealtimeStore(not_a_db)

    assert str(not_a_db) in str(caught.value)
    assert not isinstance(caught.value, sqlite3.Error)


def test_a_path_that_cannot_be_opened_at_all_is_named(tmp_path):
    """--db pointed at a directory fails at connect(), before any statement."""
    directory = tmp_path / "not-a-file"
    directory.mkdir()

    with pytest.raises(ConfigError) as caught:
        RealtimeStore(directory)

    assert str(directory) in str(caught.value)
    assert not isinstance(caught.value, sqlite3.Error)


def test_opening_creates_the_parent_directory(store_path, store):
    assert store_path.parent.is_dir()
    assert store.path == store_path


# --------------------------------------------------------------------------
# Conversions
# --------------------------------------------------------------------------


def test_conversion_minutes_deduplicates_across_the_fan_out(store, make_row):
    """One minute is one mark, however many rows produced it.

    Rows fan out over device and country, and a site may count several events
    as conversions, so a single minute can hold many matching rows -- which
    would otherwise put several marks on the same cell of the chart's rug.
    """
    store.upsert_minutes(
        "mysite",
        [
            make_row(at(10), LEAD, device="desktop"),
            make_row(at(10), LEAD, device="mobile"),
            make_row(at(10), PURCHASE, device="desktop"),
            make_row(at(10), PURCHASE, country="Japan"),
            make_row(at(11), PURCHASE),
        ],
    )

    minutes = store.conversion_minutes("mysite", [LEAD, PURCHASE], START, END)

    assert [iso_minute(m) for m in minutes] == [at(10), at(11)]


def test_conversion_minutes_ignores_events_not_configured(store, make_row):
    store.upsert_minutes(
        "mysite",
        [make_row(at(10), LEAD), make_row(at(11), PURCHASE)],
    )

    minutes = store.conversion_minutes("mysite", [LEAD], START, END)

    assert [iso_minute(m) for m in minutes] == [at(10)]


def test_conversion_minutes_ignores_a_zero_count(store, make_row):
    store.upsert_minutes("mysite", [make_row(at(10), LEAD, events=0)])

    assert store.conversion_minutes("mysite", [LEAD], START, END) == []


def test_conversion_minutes_is_scoped_to_the_site(store, make_row):
    store.upsert_minutes("clientb", [make_row(at(10), LEAD)])

    assert store.conversion_minutes("mysite", [LEAD], START, END) == []


def test_conversion_minutes_with_nothing_configured(store, make_row):
    """An empty list is a real input, and `IN ()` is a SQLite syntax error."""
    store.upsert_minutes("mysite", [make_row(at(10), LEAD)])

    assert store.conversion_minutes("mysite", [], START, END) == []


def test_conversion_minutes_range_is_half_open(store, make_row):
    store.upsert_minutes(
        "mysite",
        [make_row(START, LEAD), make_row(END, LEAD)],
    )

    minutes = store.conversion_minutes("mysite", [LEAD], START, END)

    assert [iso_minute(m) for m in minutes] == [START]


# --------------------------------------------------------------------------
# Day totals
# --------------------------------------------------------------------------


def test_day_totals_on_an_empty_range_returns_zeros(store):
    """Zeros, not None -- the footer formats these without checking."""
    totals = store.day_totals("mysite", [LEAD, PURCHASE], START, END)

    assert totals.page_views == 0
    assert totals.events == 0
    assert totals.minutes_recorded == 0
    assert totals.conversions == {LEAD: 0, PURCHASE: 0}
    assert totals.conversions_total == 0
    # The two edges stay None: "no rows" has no first minute, and a string
    # standing in for one would be a date nobody recorded.
    assert totals.first_minute is None
    assert totals.last_minute is None


def test_day_totals_counts_every_configured_conversion_by_name(
    store, make_row
):
    store.upsert_minutes(
        "mysite",
        [
            make_row(at(10), "page_view", views=4, events=4),
            make_row(at(10), LEAD, views=0, events=2),
            make_row(at(11), LEAD, views=0, events=1),
        ],
    )

    totals = store.day_totals("mysite", [LEAD, PURCHASE], START, END)

    assert totals.page_views == 4
    assert totals.events == 7
    # PURCHASE is configured and did not fire: reported as 0, not missing.
    assert totals.conversions == {LEAD: 3, PURCHASE: 0}
    assert totals.conversions_total == 3
    assert totals.minutes_recorded == 2
    assert (totals.first_minute, totals.last_minute) == (at(10), at(11))


def test_day_totals_with_no_conversions_configured(store, make_row):
    store.upsert_minutes("mysite", [make_row(at(10), LEAD, events=2)])

    totals = store.day_totals("mysite", [], START, END)

    assert totals.events == 2
    assert totals.conversions == {}
    assert totals.conversions_total == 0


def test_day_totals_range_is_half_open(store, make_row):
    store.upsert_minutes(
        "mysite",
        [
            make_row(START, views=1, events=1),
            make_row(END, views=100, events=100),
        ],
    )

    totals = store.day_totals("mysite", [], START, END)

    assert totals.page_views == 1


def test_day_totals_defaults_are_all_zero():
    """The dataclass alone, without a database behind it."""
    assert DayTotals().conversions == {}
    assert DayTotals().page_views == 0


# --------------------------------------------------------------------------
# Series and tables
# --------------------------------------------------------------------------


def test_pageview_series_is_cumulative(store, make_row):
    store.upsert_minutes(
        "mysite",
        [
            make_row(at(10), views=2),
            make_row(at(10), device="mobile", views=3),
            make_row(at(11), views=4),
            make_row(at(12), views=0),
        ],
    )

    series = store.pageview_series("mysite", START, END)

    assert [(iso_minute(m), n) for m, n in series] == [
        (at(10), 5),
        (at(11), 9),
        (at(12), 9),
    ]


def test_top_events_orders_by_count_then_name(store, make_row):
    """The name tie-break is what stops the table reshuffling every refresh."""
    store.upsert_minutes(
        "mysite",
        [
            make_row(at(10), "beta", events=5),
            make_row(at(10), "alpha", events=5),
            make_row(at(10), "gamma", events=9),
        ],
    )

    assert store.top_events("mysite", START, END) == [
        ("gamma", 9),
        ("alpha", 5),
        ("beta", 5),
    ]


def test_top_events_sums_the_fan_out_and_honours_the_limit(store, make_row):
    store.upsert_minutes(
        "mysite",
        [
            make_row(at(10), "alpha", device="desktop", events=2),
            make_row(at(10), "alpha", device="mobile", events=3),
            make_row(at(11), "beta", events=1),
        ],
    )

    assert store.top_events("mysite", START, END, limit=1) == [("alpha", 5)]


# --------------------------------------------------------------------------
# site_meta
# --------------------------------------------------------------------------


def test_site_meta_round_trip(store):
    store.save_site(
        "mysite",
        property_id="123456789",
        display_name="mysite.example",
        tz_name="America/Sao_Paulo",
    )

    meta = store.load_site("mysite")

    assert meta is not None
    assert meta.site == "mysite"
    assert meta.property_id == "123456789"
    assert meta.display_name == "mysite.example"
    assert meta.tz_name == "America/Sao_Paulo"
    assert meta.updated_utc.endswith("Z")


def test_saving_a_site_twice_updates_it(store):
    store.save_site(
        "mysite",
        property_id="1",
        display_name="old",
        tz_name="UTC",
    )
    store.save_site(
        "mysite",
        property_id="2",
        display_name="new",
        tz_name="Asia/Tokyo",
    )

    meta = store.load_site("mysite")

    assert (meta.property_id, meta.display_name, meta.tz_name) == (
        "2",
        "new",
        "Asia/Tokyo",
    )
    assert len(store.list_sites()) == 1


def test_load_site_returns_none_for_a_site_never_seen(store):
    assert store.load_site("nobody") is None


def test_list_sites_is_in_name_order(seeded_store):
    listed = [meta.site for meta in seeded_store.store.list_sites()]

    assert listed == sorted(seeded_store.sites)


def test_size_bytes_counts_the_wal_sidecars(store, make_row):
    store.upsert_minutes("mysite", [make_row(at(10))])

    # The main file is 0 bytes until the first checkpoint in WAL mode, so a
    # size that only looked at `self.path` would report a database that never
    # grows while the tool is running.
    assert store.size_bytes() > store.path.stat().st_size


# --------------------------------------------------------------------------
# The seeded fixture the rest of the suite builds on
# --------------------------------------------------------------------------


def test_the_seed_lands_in_both_sites_and_stays_separate(seeded_store):
    start, end = seeded_store.span()
    store = seeded_store.store

    for site in seeded_store.sites:
        totals = store.day_totals(
            site, seeded_store.conversions(site), start, end
        )
        assert totals.page_views > 0
        assert totals.conversions_total > 0
        assert totals.minutes_recorded == 30


def test_the_seed_marks_three_conversion_minutes_per_site(seeded_store):
    """Three minutes, reached through six rows and twelve respectively."""
    start, end = seeded_store.span()

    for site in seeded_store.sites:
        minutes = seeded_store.store.conversion_minutes(
            site, seeded_store.conversions(site), start, end
        )
        assert len(minutes) == 3


def test_the_seed_is_readable_through_each_sites_own_day(seeded_store):
    """Which is a different UTC range per site, and both must resolve."""
    for site in seeded_store.sites:
        start, end = seeded_store.bounds(site)
        assert start < end
        totals = seeded_store.store.day_totals(site, [], start, end)
        assert totals.minutes_recorded >= 0
