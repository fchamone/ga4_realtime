"""SQLite storage: the schema, the correcting UPSERT, and the day reads.

The persisted format lives here, and it is the one part of the tool a user
cannot change by editing a text file -- rows already written outlive every
later release. That is why the schema is designed once, with `site` leading
every primary key rather than appended to it, and why the version it was
written at is stamped into the file itself.

Nothing here imports `ga4.py`, `config.py` or anything else in the package
except the two leaves (`errors`, `timeutil`), and the direction is deliberate:
the API client and the poller are built *on top of* the store, so a name
borrowed downwards would close a cycle. Sites arrive as plain strings and rows
arrive as anything shaped like `MinuteRowLike`.
"""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from ga4_realtime.errors import ConfigError
from ga4_realtime.timeutil import iso_second, parse_minute, utcnow

# The schema version, stamped into `PRAGMA user_version` when a file is
# created and checked on every open. `user_version` is the version store
# because it needs no table of its own: every SQLite file already has the
# field, it reads 0 in one that nobody has stamped, and that single fact is
# what tells a pre-C-0001 database apart from an empty one. It also gives the
# next schema change somewhere to stand.
#
# Kept as a Python constant rather than written into the SQL below, so the
# guard that reads the version and the statement that writes it cannot drift.
SCHEMA_VERSION = 1

SCHEMA = """
-- `site` LEADS the primary key; it is not appended to it. WITHOUT ROWID makes
-- the primary key the clustered index, and every query this tool makes is
-- site-scoped, so leading with `site` gives one site's rows physical locality
-- and turns "this site's day" into a single range scan. Appending `site`
-- instead would scatter each site across the whole table and make every day
-- read touch every other site's pages.
--
-- There is no active_users column, and there must never be one. activeUsers
-- cannot be summed across minutes (the same visitor recurs, with no
-- identifier to deduplicate on) nor across the rows of one minute (the
-- fan-out puts one visitor in three rows), and GA4 rejects pairing it with
-- eventName outright. No column means nothing to sum by mistake.
--
-- Pre-split databases also carried an orphaned realtime_users table, kept
-- because dropping it would have destroyed collected history. It does not
-- come across: D3 starts fresh, so there is no history in a new file for a
-- drop to destroy and no reason to create the table in the first place.
CREATE TABLE IF NOT EXISTS realtime_minute (
    site              TEXT    NOT NULL,
    minute_ts_utc     TEXT    NOT NULL,
    event_name        TEXT    NOT NULL,
    device            TEXT    NOT NULL,
    country           TEXT    NOT NULL,
    screen_page_views INTEGER NOT NULL,
    event_count       INTEGER NOT NULL,
    first_seen_utc    TEXT    NOT NULL,
    last_seen_utc     TEXT    NOT NULL,
    PRIMARY KEY (site, minute_ts_utc, event_name, device, country)
) WITHOUT ROWID;

-- `site` is in this primary key too, and that is a bug fix rather than
-- symmetry. `ts_utc TEXT PRIMARY KEY` was safe while one poller existed; with
-- one thread per site, two sites completing a poll in the same second collide
-- on the key and INSERT OR REPLACE silently discards one of the two rows --
-- the poll that vanishes being, of course, most likely the one that recorded
-- an error worth reading.
CREATE TABLE IF NOT EXISTS poll_log (
    site          TEXT    NOT NULL,
    ts_utc        TEXT    NOT NULL,
    rows_fetched  INTEGER NOT NULL,
    rows_inserted INTEGER NOT NULL,
    rows_updated  INTEGER NOT NULL,
    error         TEXT,
    PRIMARY KEY (site, ts_utc)
);

-- Property name and timezone per site, cached by the live view. It is what
-- lets `report` and `sites` read a stored day with the right day boundaries
-- given --db alone: no config file, no credentials, no network.
--
-- Typed columns, where the single-property version had a key/value `meta`
-- table. The fields are known now and they are the same four for every site,
-- so key/value would only buy a composite (site, key) primary key and a
-- dictionary lookup at every use in exchange for the type checker knowing
-- nothing.
CREATE TABLE IF NOT EXISTS site_meta (
    site         TEXT PRIMARY KEY,
    property_id  TEXT NOT NULL,
    display_name TEXT NOT NULL,
    tz_name      TEXT NOT NULL,
    updated_utc  TEXT NOT NULL
);
"""

# Every metric column is overwritten from excluded, never added to. The API
# returns the authoritative total for a minute on each poll, so re-polling a
# minute must CORRECT the stored row. Writing `x = x + excluded.x` here would
# double every count on the second poll of the same minute, and the error
# would compound silently for as long as the process ran. floor_minute() in
# timeutil is load-bearing for the same reason: seconds left in the key make
# ON CONFLICT never match, and every poll becomes a duplicate insert instead.
UPSERT_MINUTE = """
INSERT INTO realtime_minute
    (site, minute_ts_utc, event_name, device, country,
     screen_page_views, event_count,
     first_seen_utc, last_seen_utc)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(site, minute_ts_utc, event_name, device, country) DO UPDATE SET
    screen_page_views = excluded.screen_page_views,
    event_count       = excluded.event_count,
    last_seen_utc     = excluded.last_seen_utc
"""


def _not_a_database(path: Path, exc: Exception) -> ConfigError:
    """The message for a --db that does not point at a usable database.

    Kept in one place because there are two chances to find out: connect()
    fails on a directory or an unwritable path, and the first statement fails
    on a file whose header is not SQLite's. A user reading either needs the
    same sentence, and a raw sqlite3 exception reaching them is a bug.
    """
    return ConfigError(
        f"the file at\n  {path}\n"
        f"could not be opened as a SQLite database ({exc}).\n"
        "Point --db at a database this tool wrote, or at a path that does "
        "not exist yet."
    )


class MinuteRowLike(Protocol):
    """One fan-out row: a single minute, event, device and country.

    No `site` field: the site is what the caller is writing *as*, passed once
    to `upsert_minutes` rather than repeated on every row it was never read
    from. Only the primary key carries it.

    A structural type rather than an import. The concrete `MinuteRow` belongs
    to `ga4.py`, which is built on top of this module, so naming it here would
    close a cycle -- and declaring a second dataclass would give the persisted
    format two definitions to drift apart. What the store needs is six values
    and a key, so that is what it asks for.
    """

    minute_ts_utc: str
    event_name: str
    device: str
    country: str
    # Only additive metrics live at this grain; see the schema comment above
    # for why no user metric of any kind is one of them.
    screen_page_views: int
    event_count: int

    def key(self) -> tuple[str, str, str, str]: ...


@dataclass
class DayTotals:
    """One site's day, as the footer and `report` need it.

    `conversions` replaces the single `leads` count that assumed one hardcoded
    event: it maps every *configured* conversion name to its count, and
    `conversions_total` is their sum, kept as a field rather than recomputed
    at each use so the footer and the report cannot disagree about it.

    The dict always holds one entry per configured conversion, zero included.
    A missing key would let a caller drop an event the user explicitly asked
    to count, and "not shown" and "did not happen" are different answers.
    """

    page_views: int = 0
    events: int = 0
    conversions: dict[str, int] = field(default_factory=dict)
    conversions_total: int = 0
    minutes_recorded: int = 0
    first_minute: str | None = None
    last_minute: str | None = None


@dataclass(frozen=True)
class SiteMeta:
    """One row of `site_meta`: what the live view cached about a property.

    `tz_name` stays text and is not resolved to a ZoneInfo here. Resolving it
    can fail -- Windows ships no IANA database at all -- and the message that
    failure deserves names the config file to edit and the tzdata install,
    which is not something the storage layer knows. Storage stays data.
    """

    site: str
    property_id: str
    display_name: str
    tz_name: str
    updated_utc: str


class RealtimeStore:
    """SQLite access. One instance per thread, all against the same file.

    sqlite3 connections are not shareable between threads, so each poller
    thread constructs its own RealtimeStore and the renderer constructs one
    more. WAL mode is what makes that safe: many readers and one writer
    proceed concurrently, so a render never waits on a poll.

    Multi-site turns one writer into N. WAL still allows only one writer at a
    time, and `timeout=10` is what installs the busy handler that makes two
    simultaneous writes serialise for a few milliseconds instead of raising
    SQLITE_BUSY. Each write is one short executemany plus a commit, so the
    wait stays in that range at the scale this tool targets.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(str(self.path), timeout=10)
        except sqlite3.Error as exc:
            raise _not_a_database(self.path, exc) from None
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._prepare_schema()
        except sqlite3.DatabaseError as exc:
            # Reached before the version guard ever runs: the very first
            # PRAGMA reads the file header, so a text file or a truncated
            # database fails here rather than at a query.
            self._conn.close()
            raise _not_a_database(self.path, exc) from None
        except Exception:
            # A store that refused to open must not leave the file open. The
            # caller is about to print a ConfigError and exit, and on Windows
            # a live handle is enough to stop them moving the file aside --
            # which is precisely what the message tells them to do.
            self._conn.close()
            raise

    # -- schema and the version guard --------------------------------------

    def _prepare_schema(self) -> None:
        """Create or verify the schema, refusing anything else in words.

        The guard exists because `CREATE TABLE IF NOT EXISTS` is not a
        migration: pointed at a database written before `site` existed, it
        finds realtime_minute already there, adds no column, succeeds -- and
        every query the tool then makes dies on `no such column: site`, a page
        into a stack trace. Reading the version first turns that into a
        sentence naming the file.
        """
        version = self._read_user_version()
        if version == 0 and self._has_table("realtime_minute"):
            raise ConfigError(
                f"the database at\n  {self.path}\n"
                "was written before this tool supported more than one site: "
                "its realtime_minute table has no `site` column, and the "
                "schema is created with CREATE TABLE IF NOT EXISTS, which "
                "will not add one. Every query would fail with "
                "`no such column: site`.\n"
                "Move that file aside and a fresh database will be created "
                "in its place, or point --db at another file. Nothing here "
                "reads or converts the old rows."
            )
        if version not in (0, SCHEMA_VERSION):
            raise ConfigError(
                f"the database at\n  {self.path}\n"
                f"was written with schema version {version}, and this build "
                f"only understands version {SCHEMA_VERSION}.\n"
                "It was most likely written by a newer release of "
                "ga4-realtime. Upgrade the tool, or point --db at another "
                "file."
            )
        self._conn.executescript(SCHEMA)
        # Not a parameter: sqlite3 does not bind values into PRAGMA
        # statements. The value is this module's own int constant, so the
        # formatting cannot carry anything a user typed.
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION:d}")
        self._conn.commit()

    def _read_user_version(self) -> int:
        row = self._conn.execute("PRAGMA user_version").fetchone()
        return int(row[0])

    def _has_table(self, name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        return row is not None

    def close(self) -> None:
        try:
            self._conn.commit()
        finally:
            self._conn.close()

    # -- writes ------------------------------------------------------------

    def upsert_minutes(
        self, site: str, rows: Sequence[MinuteRowLike]
    ) -> tuple[int, int]:
        """Write the fan-out rows for one site; return (inserted, updated).

        The two counts are worked out by reading the existing keys first,
        because changes() reports 1 for an upsert whichever branch it took and
        so cannot tell an insert from a correction.
        """
        if not rows:
            return (0, 0)

        # Bounded by the site as well as the minute range. Without the site
        # predicate this reads every site's rows in the window and reports
        # another site's key as "existing", which would misattribute the
        # insert/update split -- silently, since both counts stay plausible.
        keys = [r.minute_ts_utc for r in rows]
        cur = self._conn.execute(
            "SELECT site, minute_ts_utc, event_name, device, country "
            "FROM realtime_minute "
            "WHERE site = ? AND minute_ts_utc BETWEEN ? AND ?",
            (site, min(keys), max(keys)),
        )
        existing = {tuple(r) for r in cur.fetchall()}
        updated = sum(1 for r in rows if (site, *r.key()) in existing)

        now = iso_second(utcnow())
        self._conn.executemany(
            UPSERT_MINUTE,
            [
                (
                    site,
                    r.minute_ts_utc,
                    r.event_name,
                    r.device,
                    r.country,
                    r.screen_page_views,
                    r.event_count,
                    now,
                    now,
                )
                for r in rows
            ],
        )
        self._conn.commit()
        return (len(rows) - updated, updated)

    def log_poll(
        self,
        site: str,
        fetched: int,
        inserted: int,
        updated: int,
        error: str | None = None,
    ) -> None:
        # OR REPLACE still, but with `site` in the key the only collision left
        # is one site logging twice inside one second -- two polls of the same
        # window, whose second answer is the one worth keeping anyway. Before
        # `site` joined the key it also collapsed two *different* sites
        # finishing together, losing a row nobody could know was missing.
        self._conn.execute(
            "INSERT OR REPLACE INTO poll_log "
            "(site, ts_utc, rows_fetched, rows_inserted, rows_updated, error)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (site, iso_second(utcnow()), fetched, inserted, updated, error),
        )
        self._conn.commit()

    # -- reads -------------------------------------------------------------

    def pageview_series(
        self, site: str, start: str, end: str
    ) -> list[tuple[datetime, int]]:
        """Cumulative page views, one point per minute the site recorded."""
        cur = self._conn.execute(
            "SELECT minute_ts_utc, SUM(screen_page_views) "
            "FROM realtime_minute "
            "WHERE site = ? AND minute_ts_utc >= ? AND minute_ts_utc < ? "
            "GROUP BY minute_ts_utc ORDER BY minute_ts_utc",
            (site, start, end),
        )
        series: list[tuple[datetime, int]] = []
        running = 0
        for key, views in cur:
            running += int(views or 0)
            series.append((parse_minute(key), running))
        return series

    def conversion_minutes(
        self,
        site: str,
        conversions: Sequence[str],
        start: str,
        end: str,
    ) -> list[datetime]:
        """Minutes in which any configured conversion fired at least once.

        The GROUP BY is load-bearing rather than tidy: rows are fanned out by
        device and country, and a site may count several events as
        conversions, so one minute can hold many matching rows and would
        otherwise mark the same instant on the chart's rug repeatedly.
        """
        # `IN ()` is a syntax error in SQLite, and an empty list is a real
        # input: config rejects it, but `report` may be handed a site whose
        # conversions it does not know. No events means no minutes.
        if not conversions:
            return []
        placeholders = ", ".join("?" * len(conversions))
        cur = self._conn.execute(
            "SELECT minute_ts_utc FROM realtime_minute "
            f"WHERE site = ? AND event_name IN ({placeholders}) "
            "AND event_count > 0 "
            "AND minute_ts_utc >= ? AND minute_ts_utc < ? "
            "GROUP BY minute_ts_utc ORDER BY minute_ts_utc",
            (site, *conversions, start, end),
        )
        return [parse_minute(key) for (key,) in cur]

    def top_events(
        self, site: str, start: str, end: str, limit: int = 10
    ) -> list[tuple[str, int]]:
        # The event_name tie-break is not decoration: without it two events on
        # the same count swap places between refreshes, and a table that
        # reshuffles itself every ten seconds is unreadable.
        cur = self._conn.execute(
            "SELECT event_name, SUM(event_count) AS n FROM realtime_minute "
            "WHERE site = ? AND minute_ts_utc >= ? AND minute_ts_utc < ? "
            "GROUP BY event_name ORDER BY n DESC, event_name LIMIT ?",
            (site, start, end, limit),
        )
        return [(name, int(n or 0)) for name, n in cur]

    def event_total(self, site: str, event: str, start: str, end: str) -> int:
        """One event's count for one site over one half-open minute range.

        SUM and not COUNT: the fan-out puts one event in a row per device and
        country, so counting rows would count device categories. `event_count`
        and not `screen_page_views` because the caller names an arbitrary
        event -- for `page_view` the two agree, and for `purchase` the second
        is zero on every row.

        COALESCE rather than a None check at the call site: an event that
        never fired today is 0, and the status strip formats what comes back
        without testing it.
        """
        row = self._conn.execute(
            "SELECT COALESCE(SUM(event_count), 0) FROM realtime_minute "
            "WHERE site = ? AND event_name = ? "
            "AND minute_ts_utc >= ? AND minute_ts_utc < ?",
            (site, event, start, end),
        ).fetchone()
        return int(row[0])

    def day_totals(
        self,
        site: str,
        conversions: Sequence[str],
        start: str,
        end: str,
    ) -> DayTotals:
        """Totals for one site over one half-open range of minute keys.

        Two statements rather than one. The single `CASE WHEN event_name = ?`
        that served a single hardcoded conversion does not generalise to a
        list, and stacking one CASE per configured event would rebuild the SQL
        text for every call anyway -- so the conversions come back from a
        grouped read with an IN (...) predicate, which returns each event's
        count already labelled.
        """
        totals = DayTotals()
        row = self._conn.execute(
            "SELECT COALESCE(SUM(screen_page_views), 0), "
            "       COALESCE(SUM(event_count), 0), "
            "       COUNT(DISTINCT minute_ts_utc), "
            "       MIN(minute_ts_utc), MAX(minute_ts_utc) "
            "FROM realtime_minute "
            "WHERE site = ? AND minute_ts_utc >= ? AND minute_ts_utc < ?",
            (site, start, end),
        ).fetchone()
        (
            totals.page_views,
            totals.events,
            totals.minutes_recorded,
            totals.first_minute,
            totals.last_minute,
        ) = row

        # Seeded with every configured name at zero before the query, so an
        # event that did not fire today is reported as 0 rather than being
        # missing from the breakdown entirely.
        counts = {name: 0 for name in conversions}
        if conversions:
            placeholders = ", ".join("?" * len(conversions))
            cur = self._conn.execute(
                "SELECT event_name, SUM(event_count) FROM realtime_minute "
                f"WHERE site = ? AND event_name IN ({placeholders}) "
                "AND minute_ts_utc >= ? AND minute_ts_utc < ? "
                "GROUP BY event_name",
                (site, *conversions, start, end),
            )
            for name, n in cur:
                counts[name] = int(n or 0)
        totals.conversions = counts
        totals.conversions_total = sum(counts.values())
        return totals

    # -- cached site metadata ----------------------------------------------

    def save_site(
        self,
        site: str,
        *,
        property_id: str,
        display_name: str,
        tz_name: str,
    ) -> None:
        # Keyword-only because the three of them are strings of the same shape
        # and a transposition would be stored, not raised: a display name in
        # the tz_name column produces a database whose day boundaries are
        # wrong for as long as it is read from.
        self._conn.execute(
            "INSERT INTO site_meta "
            "(site, property_id, display_name, tz_name, updated_utc) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(site) DO UPDATE SET "
            "    property_id  = excluded.property_id, "
            "    display_name = excluded.display_name, "
            "    tz_name      = excluded.tz_name, "
            "    updated_utc  = excluded.updated_utc",
            (
                site,
                property_id,
                display_name,
                tz_name,
                iso_second(utcnow()),
            ),
        )
        self._conn.commit()

    def load_site(self, site: str) -> SiteMeta | None:
        row = self._conn.execute(
            "SELECT site, property_id, display_name, tz_name, updated_utc "
            "FROM site_meta WHERE site = ?",
            (site,),
        ).fetchone()
        return None if row is None else SiteMeta(*row)

    def list_sites(self) -> list[SiteMeta]:
        """Every site the database has heard of, in name order.

        The database, not the config: a site disabled or deleted from the
        config file still has rows here, and `sites` marks it orphaned rather
        than pretending the history is gone (D17). Name order rather than
        insertion order because there is no insertion order to speak of --
        site_meta is upserted every time the live view refreshes it.
        """
        cur = self._conn.execute(
            "SELECT site, property_id, display_name, tz_name, updated_utc "
            "FROM site_meta ORDER BY site"
        )
        return [SiteMeta(*row) for row in cur.fetchall()]

    def size_bytes(self) -> int:
        # The -wal and -shm sidecars count: in WAL mode a freshly written
        # database keeps most of a session's rows in the -wal file until a
        # checkpoint, so reporting the main file alone would show a footer
        # figure that barely moves while the tool is running.
        total = 0
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.path) + suffix)
            if candidate.exists():
                total += candidate.stat().st_size
        return total
