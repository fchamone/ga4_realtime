#!.venv\Scripts\python.exe
# ^ Deliberately not the portable "/usr/bin/env python3". The Windows launcher
# resolves an unrecognised shebang as an executable path relative to *this
# file's* directory, which is what lets `py tools\ga4_realtime.py` find the
# virtualenv from any working directory without activating it or spelling out
# the full interpreter path. The dependencies live only in that venv, so the
# portable form starts the system Python and dies in the import guard below.
"""Record the GA4 Realtime window and render it as a live terminal dashboard.

GA4's Realtime report only reaches back 30 minutes, and the standard reports
lag 4-8 hours behind. Whatever happens while nobody is watching the screen is
simply lost. This script polls that window on a schedule, accumulates the
result into a local SQLite file, and draws a "today so far" view from the
accumulated history -- numbers the GA4 UI will not show for hours yet.

One process, two jobs: a background thread polls and writes, the main thread
renders from the same database.

    tools/.venv/Scripts/python.exe tools/ga4_realtime.py
    tools/.venv/Scripts/python.exe tools/ga4_realtime.py --interval 60
    tools/.venv/Scripts/python.exe tools/ga4_realtime.py --window 3
    tools/.venv/Scripts/python.exe tools/ga4_realtime.py report
    tools/.venv/Scripts/python.exe tools/ga4_realtime.py export --format csv

Configuration comes from tools/.env -- see tools/.env.example.
"""

import argparse
import csv
import io
import json
import logging
import os
import random
import shutil
import signal
import sqlite3
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Third-party imports are grouped behind one guard so a fresh clone that
# forgot the install step gets the command to run instead of a bare traceback
# naming whichever package happened to be imported first.
try:
    import plotext as plt
    from dotenv import load_dotenv
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
    from rich import box
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError as exc:  # pragma: no cover - environment problem, not logic
    # Two unrelated faults land here and the cure differs, so say which it is.
    # Almost always the venv is complete and the wrong interpreter was used:
    # bare `python` is a Microsoft Store stub, and a `py` that ignored the
    # shebang above resolves to the system install, which has none of these
    # packages. Absolute paths, because the relative form is only correct when
    # cwd happens to be the repo root -- and cwd is tools/ as often as not.
    _here = Path(__file__).resolve()
    _venv = _here.parent / ".venv" / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    if _venv.is_file() and Path(sys.executable) != _venv:
        sys.exit(
            f"missing dependency: {exc.name}\n"
            f"This ran under {sys.executable},\n"
            "which is not the virtualenv the dependencies are installed in.\n"
            "Run it with that interpreter instead:\n"
            f"  {_venv} {_here}"
        )
    sys.exit(
        f"missing dependency: {exc.name}\n"
        "install it with:\n"
        f"  {_venv} -m pip install -r {_here.parent / 'requirements.txt'}"
    )


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# Every path resolves from __file__ rather than the working directory. A
# cwd-relative default would build one database when run from the repo root
# and a different one when run from tools/, fragmenting the very history this
# tool exists to accumulate.
TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
OUT_DIR = TOOLS_DIR / "out"
DEFAULT_DB = OUT_DIR / "ga4_realtime.db"
LOG_PATH = OUT_DIR / "ga4_realtime.log"

# The polled window is deliberately wider than the default interval so that
# late-arriving events and skipped cycles are re-read and corrected rather
# than lost. Widening it costs nothing but a slightly larger response.
POLL_WINDOW_MINUTES = 10
DEFAULT_INTERVAL = 300
DEFAULT_REFRESH = 10
MAX_BACKOFF = 300

# Hours of history the chart shows. Unrelated to POLL_WINDOW_MINUTES above:
# that one is how far back each request reads, this one is how much of the
# accumulated day is on screen at once.
DEFAULT_WINDOW_HOURS = 6

# The Realtime API has no pagePath dimension; its only page dimension is
# unifiedScreenName, which on web returns the page *title*. On a one-page site
# that is nearly useless, so the breakdown is by eventName instead -- which
# also puts generate_lead, the conversion this site optimises for, on screen.
FANOUT_DIMENSIONS = ("minutesAgo", "eventName", "deviceCategory", "country")
# activeUsers is absent on purpose, and the API enforces it: pairing that
# user-scoped metric with the event-scoped eventName dimension is rejected
# outright with "Selected dimensions and metrics cannot be queried together."
# It was never usable at this grain anyway: one visitor firing page_view,
# scroll and session_start appears in three rows of the same minute, so the
# figure could not be summed across rows any more than across minutes.
FANOUT_METRICS = ("screenPageViews", "eventCount")

LEAD_EVENT = "generate_lead"

log = logging.getLogger("ga4_realtime")


class ConfigError(Exception):
    """A misconfiguration the user can fix, reported without a traceback."""


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def floor_minute(moment: datetime) -> datetime:
    """Truncate to the start of the minute.

    Load-bearing. The minute key is derived by subtracting minutesAgo from
    this value, so it must not carry seconds: without the truncation two polls
    of the same real minute produce keys a few seconds apart, the UPSERT's
    ON CONFLICT never matches, and every poll silently inserts duplicates
    instead of correcting the existing row.
    """
    return moment.replace(second=0, microsecond=0)


def iso_minute(moment: datetime) -> str:
    """Format a UTC instant as the storage key.

    Fixed width and always UTC, so plain string comparison orders and ranges
    correctly in SQL and no date functions are needed.
    """
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z")


def iso_second(moment: datetime) -> str:
    """Second-resolution stamp for the audit columns.

    Deliberately not iso_minute: first_seen/last_seen exist to show when a row
    was last corrected, and minute resolution would hide every correction made
    inside the same minute -- exactly the case worth watching.
    """
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_minute(key: str) -> datetime:
    return datetime.strptime(key, "%Y-%m-%dT%H:%M:00Z").replace(
        tzinfo=timezone.utc
    )


def day_bounds_utc(day: date, tz: ZoneInfo) -> tuple[str, str]:
    """Half-open [start, end) of a property-local day, as UTC storage keys.

    Storage is UTC and only display is local, so every day-scoped query has to
    cross the boundary here rather than in SQL.
    """
    start_local = datetime(day.year, day.month, day.day, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return iso_minute(start_local), iso_minute(end_local)


def humanize_delta(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def require(name: str) -> str:
    """Return an environment variable or explain exactly what is missing."""
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set.\n"
            f"Add it to {TOOLS_DIR / '.env'} -- copy tools/.env.example if "
            "that file does not exist yet."
        )
    return value


def validate_property_id(raw: str) -> str:
    """Reject the measurement ID by name.

    Passing G-XXXXXXXXXX here yields a PermissionDenied from the API that
    never mentions the real cause, and it is the single most common way to
    lose an afternoon to this setup.
    """
    value = raw.strip()
    if value.upper().startswith("G-"):
        raise ConfigError(
            f"GA4_PROPERTY_ID is set to {value!r}, which is a *measurement* "
            "ID.\n"
            "That one belongs to gtag.js in public/index.html; the API needs "
            "the numeric property ID.\n"
            "Find it in GA4 -> Admin -> Property settings -> Property ID."
        )
    if not value.isdigit():
        raise ConfigError(
            f"GA4_PROPERTY_ID is set to {value!r}; it must be all digits.\n"
            "Find it in GA4 -> Admin -> Property settings -> Property ID."
        )
    return value


def credentials_path() -> Path:
    """Resolve the service-account key and re-export it as an absolute path.

    A relative path in .env is resolved against tools/, never the working
    directory. It is written back into the environment because the Google
    client libraries read GOOGLE_APPLICATION_CREDENTIALS themselves and would
    otherwise re-resolve the relative form against the wrong directory.
    """
    raw = require("GOOGLE_APPLICATION_CREDENTIALS")
    path = Path(raw)
    if not path.is_absolute():
        path = TOOLS_DIR / path
    path = path.resolve()
    if not path.is_file():
        raise ConfigError(
            f"service account key not found at {path}\n"
            "Create it in Google Cloud Console -> IAM -> Service accounts -> "
            "Keys -> Add key -> JSON, then save it there.\n"
            "See the GA4 access section of tools/README.md."
        )
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(path)
    return path


def load_config() -> str:
    """Load tools/.env and return the validated numeric property ID.

    The property ID is checked before the key file so that the measurement-ID
    mix-up is reported even when the credentials are not sorted out yet; it is
    the mistake that costs the most time to diagnose from the API's own error.
    """
    load_dotenv(TOOLS_DIR / ".env")
    property_id = validate_property_id(require("GA4_PROPERTY_ID"))
    credentials_path()
    return property_id


# --------------------------------------------------------------------------
# GA4 client
# --------------------------------------------------------------------------


class PropertyInfo(NamedTuple):
    id: str
    display_name: str
    tz_name: str
    tz: ZoneInfo


@dataclass(frozen=True)
class MinuteRow:
    """One fan-out row: a single minute, event, device and country."""

    minute_ts_utc: str
    event_name: str
    device: str
    country: str
    # Only additive metrics live at this grain. There is deliberately no
    # active_users column: it cannot be summed across minutes *or* across the
    # rows of one minute, so storing it here would only invite a wrong SUM.
    screen_page_views: int
    event_count: int

    def key(self) -> tuple[str, str, str, str]:
        return (self.minute_ts_utc, self.event_name, self.device, self.country)


class GA4Client:
    """Everything that talks to Google. Owned by the polling thread."""

    def __init__(self, property_id: str) -> None:
        self.property_id = property_id
        self.resource = f"properties/{property_id}"
        try:
            self._data = BetaAnalyticsDataClient()
        except auth_exc.DefaultCredentialsError as exc:
            # A truncated or wrong-format key file fails here rather than at
            # the first request, and the library's own message does not say
            # which file it was pointed at.
            raise ConfigError(
                f"the service account key could not be loaded.\n"
                f"  {os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')}\n"
                "It must be the JSON key downloaded from Google Cloud Console "
                "-> IAM -> Service accounts -> Keys, used as-is.\n"
                f"Details: {exc}"
            ) from None
        self.latest_quota = None

    # -- Admin API ---------------------------------------------------------

    def fetch_property(self, tz_override: str | None = None) -> PropertyInfo:
        """Look up display name and timezone, degrading to UTC if refused.

        The Admin API is a separate API from the Data API and needs to be
        enabled separately, so a service account that can read reports
        perfectly well may still be refused here. That should cost the header
        a nice label, not the whole dashboard.
        """
        display_name = f"property {self.property_id}"
        tz_name = tz_override or "UTC"
        if tz_override is None:
            try:
                admin = AnalyticsAdminServiceClient()
                prop = admin.get_property(name=self.resource)
                display_name = prop.display_name or display_name
                tz_name = prop.time_zone or "UTC"
            except gexc.PermissionDenied:
                log.warning(
                    "Admin API refused property %s; falling back to UTC",
                    self.property_id,
                )
                print(
                    "warning: cannot read the property's timezone.\n"
                    "  Enable the Google Analytics Admin API in the same GCP "
                    "project, and confirm the service account has Viewer in "
                    "GA4 -> Admin -> Property access management.\n"
                    "  Falling back to UTC for day boundaries; override with "
                    "--timezone.",
                    file=sys.stderr,
                )
            except gexc.GoogleAPICallError as exc:
                log.warning("Admin API call failed: %s", exc)
                print(
                    f"warning: Admin API lookup failed ({exc.__class__.__name__});"
                    " falling back to UTC.",
                    file=sys.stderr,
                )

        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            # Windows ships no IANA database, so this is what a missing tzdata
            # package looks like. Say so rather than leaving the user to guess.
            raise ConfigError(
                f"timezone {tz_name!r} could not be resolved.\n"
                "Python on Windows has no system timezone database; install "
                "the data package:\n"
                "  tools/.venv/Scripts/python.exe -m pip install tzdata"
            ) from None

        return PropertyInfo(self.property_id, display_name, tz_name, tz)

    # -- Data API ----------------------------------------------------------

    def _minute_range(self) -> MinuteRange:
        # One range, not ten. The Data API accepts at most two minute ranges
        # per request; per-minute attribution comes from the minutesAgo
        # dimension instead, which is exact and costs nothing.
        return MinuteRange(
            start_minutes_ago=POLL_WINDOW_MINUTES - 1, end_minutes_ago=0
        )

    def poll_realtime(self) -> list[MinuteRow]:
        request = RunRealtimeReportRequest(
            property=self.resource,
            dimensions=[Dimension(name=n) for n in FANOUT_DIMENSIONS],
            metrics=[Metric(name=n) for n in FANOUT_METRICS],
            minute_ranges=[self._minute_range()],
            return_property_quota=True,
        )
        # Anchor before the call, not after: minutesAgo is relative to when
        # the server processed the request, so the closest local clock reading
        # is the one taken just before sending. A request that straddles a
        # minute boundary can still misplace a row by one minute, but the
        # 10-minute window re-reads and corrects it on the next poll.
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


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS realtime_minute (
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

-- A realtime_users table lived here once, holding the per-minute activeUsers
-- gauge. Databases created before it was removed still carry it, orphaned and
-- harmless; nothing reads or writes it now. Do not add a migration to drop it,
-- which would destroy collected history to reclaim a few kilobytes.

CREATE TABLE IF NOT EXISTS poll_log (
    ts_utc        TEXT PRIMARY KEY,
    rows_fetched  INTEGER NOT NULL,
    rows_inserted INTEGER NOT NULL,
    rows_updated  INTEGER NOT NULL,
    error         TEXT
);

-- Property name and timezone, cached by the live view. It is what lets
-- `report` and `export` read a stored day with the right day boundaries
-- without credentials, a network round trip, or the Admin API.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Every metric column is overwritten from excluded, never added to. The API
# returns the authoritative total for a minute on each poll, so re-polling a
# minute must CORRECT the stored row. Writing `x = x + excluded.x` here would
# double every count on the second poll of the same minute, and the error
# would compound silently for as long as the process ran.
UPSERT_MINUTE = """
INSERT INTO realtime_minute
    (minute_ts_utc, event_name, device, country,
     screen_page_views, event_count,
     first_seen_utc, last_seen_utc)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(minute_ts_utc, event_name, device, country) DO UPDATE SET
    screen_page_views = excluded.screen_page_views,
    event_count       = excluded.event_count,
    last_seen_utc     = excluded.last_seen_utc
"""


@dataclass
class DayTotals:
    page_views: int = 0
    events: int = 0
    leads: int = 0
    minutes_recorded: int = 0
    first_minute: str | None = None
    last_minute: str | None = None


class RealtimeStore:
    """SQLite access.

    sqlite3 connections are not shareable between threads, so each thread
    constructs its own RealtimeStore against the same file. WAL mode is what
    makes that safe: one writer and any number of readers proceed
    concurrently, so a render never waits on a poll.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.commit()
        finally:
            self._conn.close()

    # -- writes ------------------------------------------------------------

    def upsert_minutes(self, rows: list[MinuteRow]) -> tuple[int, int]:
        """Write the fan-out rows; return (inserted, updated).

        The two counts are worked out by reading the existing keys first,
        because changes() reports 1 for an upsert whichever branch it took and
        so cannot tell an insert from a correction.
        """
        if not rows:
            return (0, 0)
        keys = [r.minute_ts_utc for r in rows]
        cur = self._conn.execute(
            "SELECT minute_ts_utc, event_name, device, country "
            "FROM realtime_minute WHERE minute_ts_utc BETWEEN ? AND ?",
            (min(keys), max(keys)),
        )
        existing = {tuple(r) for r in cur.fetchall()}
        updated = sum(1 for r in rows if r.key() in existing)

        now = iso_second(utcnow())
        self._conn.executemany(
            UPSERT_MINUTE,
            [
                (
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
        fetched: int,
        inserted: int,
        updated: int,
        error: str | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO poll_log "
            "(ts_utc, rows_fetched, rows_inserted, rows_updated, error) "
            "VALUES (?, ?, ?, ?, ?)",
            (iso_second(utcnow()), fetched, inserted, updated, error),
        )
        self._conn.commit()

    # -- reads -------------------------------------------------------------

    def pageview_series(
        self, start: str, end: str
    ) -> list[tuple[datetime, int]]:
        """Cumulative page views across a day, one point per recorded minute."""
        cur = self._conn.execute(
            "SELECT minute_ts_utc, SUM(screen_page_views) "
            "FROM realtime_minute WHERE minute_ts_utc >= ? AND minute_ts_utc < ? "
            "GROUP BY minute_ts_utc ORDER BY minute_ts_utc",
            (start, end),
        )
        series: list[tuple[datetime, int]] = []
        running = 0
        for key, views in cur:
            running += int(views or 0)
            series.append((parse_minute(key), running))
        return series

    def lead_minutes(self, start: str, end: str) -> list[datetime]:
        """Minutes in which generate_lead fired at least once.

        The GROUP BY is load-bearing rather than tidy: rows are fanned out by
        device and country, so a single lead minute can hold several rows and
        would otherwise mark the same instant more than once.
        """
        cur = self._conn.execute(
            "SELECT minute_ts_utc FROM realtime_minute "
            "WHERE event_name = ? AND event_count > 0 "
            "AND minute_ts_utc >= ? AND minute_ts_utc < ? "
            "GROUP BY minute_ts_utc ORDER BY minute_ts_utc",
            (LEAD_EVENT, start, end),
        )
        return [parse_minute(key) for (key,) in cur]

    def top_events(
        self, start: str, end: str, limit: int = 10
    ) -> list[tuple[str, int]]:
        cur = self._conn.execute(
            "SELECT event_name, SUM(event_count) AS n FROM realtime_minute "
            "WHERE minute_ts_utc >= ? AND minute_ts_utc < ? "
            "GROUP BY event_name ORDER BY n DESC, event_name LIMIT ?",
            (start, end, limit),
        )
        return [(name, int(n or 0)) for name, n in cur]

    def day_totals(self, start: str, end: str) -> DayTotals:
        totals = DayTotals()
        row = self._conn.execute(
            "SELECT COALESCE(SUM(screen_page_views), 0), "
            "       COALESCE(SUM(event_count), 0), "
            "       COALESCE(SUM(CASE WHEN event_name = ? "
            "                    THEN event_count END), 0), "
            "       COUNT(DISTINCT minute_ts_utc), "
            "       MIN(minute_ts_utc), MAX(minute_ts_utc) "
            "FROM realtime_minute "
            "WHERE minute_ts_utc >= ? AND minute_ts_utc < ?",
            (LEAD_EVENT, start, end),
        ).fetchone()
        (
            totals.page_views,
            totals.events,
            totals.leads,
            totals.minutes_recorded,
            totals.first_minute,
            totals.last_minute,
        ) = row
        return totals

    def export_rows(self, start: str, end: str) -> list[dict]:
        cur = self._conn.execute(
            "SELECT minute_ts_utc, event_name, device, country, "
            "       screen_page_views, event_count, first_seen_utc, "
            "       last_seen_utc "
            "FROM realtime_minute "
            "WHERE minute_ts_utc >= ? AND minute_ts_utc < ? "
            "ORDER BY minute_ts_utc, event_name, device, country",
            (start, end),
        )
        columns = [c[0] for c in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]

    # -- cached property metadata -----------------------------------------

    def save_property(self, prop: PropertyInfo) -> None:
        self._conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [
                ("property_id", prop.id),
                ("display_name", prop.display_name),
                ("tz_name", prop.tz_name),
            ],
        )
        self._conn.commit()

    def load_property(self) -> PropertyInfo | None:
        rows = dict(self._conn.execute("SELECT key, value FROM meta"))
        if not {"property_id", "tz_name"} <= rows.keys():
            return None
        try:
            tz = ZoneInfo(rows["tz_name"])
        except ZoneInfoNotFoundError:
            return None
        return PropertyInfo(
            rows["property_id"],
            rows.get("display_name", f"property {rows['property_id']}"),
            rows["tz_name"],
            tz,
        )

    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.path) + suffix)
            if candidate.exists():
                total += candidate.stat().st_size
        return total


# --------------------------------------------------------------------------
# Poller
# --------------------------------------------------------------------------


@dataclass
class PollStatus:
    """Poller state read by the UI. Every field access goes through the lock."""

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


class Poller(threading.Thread):
    """Background polling loop. Must never die on an API error."""

    def __init__(
        self,
        client: GA4Client,
        db_path: Path,
        interval: int,
        status: PollStatus,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name="ga4-poller", daemon=True)
        self.client = client
        self.db_path = db_path
        self.interval = interval
        self.status = status
        self.stop_event = stop_event

    def run(self) -> None:
        store = RealtimeStore(self.db_path)  # opened in the owning thread
        failures = 0
        try:
            while not self.stop_event.is_set():
                delay = self.interval
                try:
                    self.poll_once(store)
                    failures = 0
                except Exception as exc:  # noqa: BLE001 - the loop must survive
                    failures += 1
                    # Exponential backoff with jitter. The jitter matters less
                    # here than in a fleet, but it keeps a restarting process
                    # from lining its retries up with the previous run's.
                    delay = min(MAX_BACKOFF, 2**failures) + random.uniform(0, 1)
                    message = f"{exc.__class__.__name__}: {exc}"
                    log.error("poll failed (%d in a row): %s", failures, message)
                    with self.status.lock:
                        self.status.last_error = message
                        self.status.error_count += 1
                        self.status.in_flight = False
                    try:
                        store.log_poll(0, 0, 0, message)
                    except sqlite3.Error:
                        log.exception("could not write poll_log")
                # Interruptible: a stop request must not wait out the interval.
                self.stop_event.wait(delay)
        finally:
            store.close()
            log.info("poller stopped")

    def poll_once(self, store: RealtimeStore) -> None:
        with self.status.lock:
            self.status.in_flight = True
        try:
            rows = self.client.poll_realtime()
            inserted, updated = store.upsert_minutes(rows)
            store.log_poll(len(rows), inserted, updated, None)
            log.info(
                "poll ok: %d rows (%d new, %d corrected)",
                len(rows), inserted, updated,
            )
            with self.status.lock:
                self.status.last_success = utcnow()
                self.status.last_error = None
                self.status.poll_count += 1
                self.status.quota = self.client.latest_quota
        finally:
            with self.status.lock:
                self.status.in_flight = False


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------


class RingBufferHandler(logging.Handler):
    """Keeps the last N records in memory for the --verbose panel."""

    def __init__(self, capacity: int = 200) -> None:
        super().__init__()
        self.records: deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(
            f"{datetime.fromtimestamp(record.created):%H:%M:%S} "
            f"{record.levelname[:4]:<4} {record.getMessage()}"
        )


def setup_logging(verbose: bool) -> RingBufferHandler:
    """Log to a rotating file only -- stdout belongs to the live view."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.propagate = False
    for handler in list(log.handlers):
        log.removeHandler(handler)

    file_handler = RotatingFileHandler(
        LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    )
    log.addHandler(file_handler)

    ring = RingBufferHandler()
    log.addHandler(ring)
    return ring


# --------------------------------------------------------------------------
# Terminal input
# --------------------------------------------------------------------------


class KeyReader:
    """Non-blocking single-key reads, without curses.

    On Windows msvcrt does this with no terminal mode changes at all. On POSIX
    the terminal has to be put into cbreak mode, and restoring it lives in
    __exit__ so that an exception mid-render cannot leave the user's shell
    with echo switched off.
    """

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled and sys.stdin is not None and sys.stdin.isatty()
        self._fd = None
        self._saved = None
        self._win = os.name == "nt"

    def __enter__(self) -> "KeyReader":
        if not self.enabled:
            return self
        if self._win:
            import msvcrt  # noqa: F401 - imported for availability check

            return self
        try:
            import termios
            import tty

            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        except Exception:  # noqa: BLE001 - not a tty we can drive
            self.enabled = False
        return self

    def __exit__(self, *exc_info) -> None:
        if self._saved is not None and self._fd is not None:
            import termios

            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)

    def poll(self) -> str | None:
        if not self.enabled:
            return None
        if self._win:
            import msvcrt

            if msvcrt.kbhit():
                char = msvcrt.getwch()
                # Arrow and function keys arrive as a two-character sequence;
                # swallow the payload so it is not read as a command.
                if char in ("\x00", "\xe0"):
                    msvcrt.getwch()
                    return None
                return char
            return None

        import select

        if select.select([sys.stdin], [], [], 0)[0]:
            return sys.stdin.read(1)
        return None


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------


# plotext draws its axes with box-drawing characters whatever the theme, and
# those are no more encodable on a legacy code page than the braille markers
# are. --ascii has to flatten them too, or it only half works.
_ASCII_FRAME = str.maketrans(
    {
        "─": "-", "━": "-", "│": "|", "┃": "|",
        "┌": "+", "┐": "+", "└": "+", "┘": "+",
        "├": "+", "┤": "+", "┬": "+", "┴": "+", "┼": "+",
        "╌": "-", "╎": "|",
    }
)


def frame_box(ascii_mode: bool):
    """Border style for every panel and table, so --ascii covers them too."""
    return box.ASCII if ascii_mode else box.ROUNDED


def stdout_supports_blocks() -> bool:
    """Can stdout encode the braille and block glyphs the plots use?

    A legacy Windows code page cannot, and the failure mode is a
    UnicodeEncodeError thrown from inside the Live refresh -- a crash in the
    middle of the dashboard rather than an obviously wrong character. Checking
    up front turns that into an automatic fall back to --ascii.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "█⣿".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def nice_step(minimum: int) -> int:
    """Round a step up to 1, 2 or 5 times a power of ten.

    Without this the axis is spaced by whatever the arithmetic produced -- a
    chart topping out at 35 across five slots steps by 7 and reads
    0/7/14/21/28/35. The same chart stepped by 10 reads at a glance.
    """
    unit = 1
    while True:
        for factor in (1, 2, 5):
            if unit * factor >= minimum:
                return unit * factor
        unit *= 10


def nice_time_step(minimum: int) -> int:
    """Round a tick spacing up to a value that divides the clock.

    nice_step()'s decimal ladder is right for counted values and wrong for
    clock time: it will happily choose 200 minutes, which labels the axis
    09:07, 12:27, 15:47. These are the spacings that keep every label on a
    round minute or hour.
    """
    for step in (1, 2, 5, 10, 15, 20, 30, 60, 120, 180, 240, 360, 720):
        if step >= minimum:
            return step
    return 1440


def integer_ticks(top: int, height: int) -> list[int]:
    """Y-axis ticks at whole numbers, from zero to at least `top`.

    Page views are counted, not measured. Left to itself plotext fits the axis
    to the data and labels a chart that peaked at 6 with 5.50 and 6.00, which
    claims half a page view happened. The tick count comes from the panel
    height so a short chart does not crowd its own labels.
    """
    top = max(top, 1)
    slots = max(2, min(6, (height - 1) // 2))
    step = nice_step(-(-top // slots))  # ceil division, no float round-trip
    return list(range(0, top + step, step))


def build_chart(
    points: list[tuple[datetime, int]],
    tz: ZoneInfo,
    width: int,
    height: int,
    ascii_mode: bool,
    no_color: bool,
    color: str,
    leads: list[datetime] | None = None,
    *,
    now: datetime,
    window_minutes: int,
) -> Text:
    """Render a plotext line chart into a rich renderable.

    plt.build() returns the plot as a string instead of printing it, which is
    what lets it live inside a Live region; printing would fight the Live
    refresh. clear_figure() first because plotext keeps global figure state
    and would otherwise draw every frame on top of the last.

    The x axis is a trailing window ending at `now`, not a fit to the data.
    Left to itself plotext fits the axis to whatever minutes happen to be
    recorded, so a poller started a moment ago stretches ten minutes across
    the whole panel and the axis rescales on every poll. A fixed window slides
    forward with the clock instead, and a fresh start draws a small curve at
    the right edge -- which is the honest picture of what has been collected.

    `now` and `window_minutes` are keyword-only and have no defaults: a default
    here would quietly drift from the --window flag the caller was given.

    `leads` are marked as a rug of stars along the baseline rather than on the
    curve itself: a terminal cell is coarse enough that a star placed at the
    running total lands a row off the line as often as on it, and two leads a
    few minutes apart overlap. On the baseline they are always legible.
    """
    if width < 20 or height < 5:
        return Text("(terminal too small)", style="dim")
    if not points:
        return Text("\n" * (height - 1) + "  no data yet", style="dim")

    plt.clear_figure()
    plt.plotsize(width, height)
    plt.theme("clear" if (ascii_mode or no_color) else "pro")

    # x is minutes past local midnight, which keeps the axis numeric and lets
    # the tick labels be formatted explicitly rather than left to plotext's
    # date parsing.
    def minutes_past_midnight(moment: datetime) -> int:
        local = moment.astimezone(tz)
        return local.hour * 60 + local.minute

    # The trailing window, in the same units. `left` is negative until the day
    # is itself window_minutes old -- at 02:10 with a 6h window it is -230,
    # which reads as 20:10 yesterday. That region is drawn empty
    # rather than clipped away: the series is day-scoped so there is genuinely
    # nothing there, and keeping the full width is what stops the axis from
    # rescaling through the early hours.
    right = minutes_past_midnight(now)
    left = right - window_minutes

    # Clipped to the window, but the level entering it is carried across. The
    # series is cumulative since midnight, so a point before `left` is not
    # noise to drop -- it is the running total the window opens at. Dropping it
    # outright would start the line mid-panel at the first in-window point and
    # lose the flat run in from the left edge.
    xs, ys = [], []
    carried: int | None = None
    for moment, value in points:
        x = minutes_past_midnight(moment)
        if x < left:
            carried = value
            continue
        if x > right:
            continue
        xs.append(x)
        ys.append(value)
    if carried is not None:
        xs.insert(0, left)
        ys.insert(0, carried)
    if not xs:
        return Text("\n" * (height - 1) + "  no data in window", style="dim")

    # In --ascii the line is drawn with dots rather than stars, so the lead
    # markers below stay distinguishable from the curve itself.
    marker = "." if ascii_mode else "braille"
    if no_color:
        plt.plot(xs, ys, marker=marker)
    else:
        plt.plot(xs, ys, marker=marker, color=color)

    # After plot(), so the stars paint over the line where the two meet.
    # Leads outside the window are dropped rather than drawn at its edge,
    # where they would claim a conversion happened at a time it did not.
    if leads:
        lead_xs = [
            x for x in map(minutes_past_midnight, leads) if left <= x <= right
        ]
        if lead_xs:
            baseline = [0] * len(lead_xs)
            if no_color:
                plt.scatter(lead_xs, baseline, marker="*")
            else:
                plt.scatter(lead_xs, baseline, marker="*", color="green")

    # Anchor the y axis at zero and label it in whole numbers. plotext
    # otherwise fits the axis to the data, which turns a count sitting flat at
    # 5 into a dramatic-looking climb between 5.50 and 6.00.
    y_ticks = integer_ticks(max(ys), height)
    plt.yticks(y_ticks, [str(t) for t in y_ticks])
    plt.ylim(0, y_ticks[-1])

    # Hold the axis to the window. Checked against the installed plotext: a
    # user-set limit overrides the one derived from the data (only None entries
    # are replaced), so the axis stays put even when the data fills a sliver.
    plt.xlim(left, right)

    # Ticks land on round clock boundaries inside the window rather than on
    # the first recorded minute, so the labels read 09:00 and not 09:07. Both
    # -(-a // b) are ceil division, kept in integers to avoid a float
    # round-trip putting a tick a minute off the hour.
    slots = max(1, min(6, width // 12))
    step = nice_time_step(-(-window_minutes // slots))
    first = -(-left // step) * step  # first boundary at or after the left edge
    x_ticks = list(range(first, right + 1, step)) or [right]
    # % 1440 is what lets a negative tick read as yesterday evening: Python's
    # modulo is floored, so -120 becomes 1320, which is 22:00.
    plt.xticks(
        x_ticks,
        [f"{(t % 1440) // 60:02d}:{(t % 1440) % 60:02d}" for t in x_ticks],
    )
    plt.xlabel("")

    # build() ends with a newline, which would cost one line of the panel's
    # fixed height and push the x-axis labels out of view. no_wrap/crop then
    # guarantees the plot occupies exactly the rows budgeted for it even if
    # it comes back a column wider than asked.
    rendered = plt.build().rstrip("\n")
    if ascii_mode:
        rendered = rendered.translate(_ASCII_FRAME)
    return Text.from_ansi(rendered, no_wrap=True, overflow="crop")


def build_header(
    prop: PropertyInfo,
    snapshot: dict,
    interval: int,
    no_color: bool,
    ascii_mode: bool = False,
) -> Panel:
    now_local = utcnow().astimezone(prop.tz)
    last = snapshot["last_success"]
    if last is None:
        age_text = Text("never", style="" if no_color else "bold red")
    else:
        age = (utcnow() - last).total_seconds()
        # Two missed intervals is the point at which this stopped being a
        # slow poll and started being a broken one.
        stale = age > interval * 2
        style = "" if no_color else ("bold red" if stale else "green")
        age_text = Text(f"{humanize_delta(age)} ago", style=style)

    spinner = "*" if snapshot["in_flight"] else " "
    # no_wrap is what keeps this panel exactly as tall as render_dashboard
    # budgeted for it. A wrapped header pushes the whole frame past the
    # bottom of the terminal, and inside a non-screen Live that tears.
    line = Text(
        no_wrap=True, overflow="crop" if ascii_mode else "ellipsis"
    )
    line.append(f"{prop.display_name} ", style="" if no_color else "bold")
    line.append(f"({prop.id})  ", style="" if no_color else "dim")
    line.append(f"{prop.tz_name}  ")
    line.append(f"{now_local:%H:%M:%S}  ")
    line.append("last poll ")
    line.append_text(age_text)
    line.append(f"  polls {snapshot['poll_count']}")
    if snapshot["error_count"]:
        line.append(
            f"  errors {snapshot['error_count']}",
            style="" if no_color else "red",
        )
    line.append(f"  {spinner}")

    if snapshot["last_error"]:
        line.append("\n")
        line.append(
            f"last error: {snapshot['last_error'][:160]}",
            style="" if no_color else "red",
        )
    return Panel(
        line,
        padding=(0, 1),
        box=frame_box(ascii_mode),
        height=4 if snapshot["last_error"] else 3,
    )


def build_events_table(
    events: list[tuple[str, int]], width: int, ascii_mode: bool, no_color: bool
) -> Table:
    table = Table(
        expand=True,
        padding=(0, 1),
        show_edge=True,
        box=frame_box(ascii_mode),
        title="Top events today",
    )
    table.add_column("event", ratio=3, no_wrap=True)
    table.add_column("count", justify="right", width=8)
    table.add_column("", ratio=4, no_wrap=True)

    if not events:
        table.add_row("no data yet", "", "")
        return table

    bar_char = "#" if ascii_mode else "█"
    bar_width = max(4, int(width * 0.35))
    top = max(count for _, count in events) or 1
    for name, count in events:
        filled = max(1, round(count / top * bar_width)) if count else 0
        style = ""
        if not no_color:
            style = "bold cyan" if name == LEAD_EVENT else "cyan"
        table.add_row(name, f"{count:,}", Text(bar_char * filled, style=style))
    return table


def build_footer(
    totals: DayTotals,
    quota: object | None,
    db_path: Path,
    db_bytes: int,
    no_color: bool,
    ascii_mode: bool = False,
    *,
    started: float,
) -> Panel:
    quota_text = "quota n/a"
    if quota is not None:
        hourly = getattr(quota, "tokens_per_hour", None)
        daily = getattr(quota, "tokens_per_day", None)
        parts = []
        if hourly is not None:
            parts.append(f"hour {hourly.remaining}/{hourly.consumed + hourly.remaining}")
        if daily is not None:
            parts.append(f"day {daily.remaining}")
        if parts:
            quota_text = "tokens " + "  ".join(parts)

    # Shown relative to the repo when it lives there, because the absolute
    # Windows path is long enough on its own to wrap the footer.
    try:
        shown_path = db_path.relative_to(REPO_ROOT)
    except ValueError:
        shown_path = db_path

    line = Text(
        no_wrap=True, overflow="crop" if ascii_mode else "ellipsis"
    )
    line.append(f"page views {totals.page_views:,}   ")
    line.append(f"events {totals.events:,}   ")
    line.append(
        f"{LEAD_EVENT} {totals.leads:,}",
        style="" if no_color else "bold green",
    )
    # Deliberately no user count of any kind. Realtime data cannot produce a
    # daily unique-user figure -- the same visitor recurs in consecutive minute
    # windows with no identifier to deduplicate on -- so there is none to show.
    line.append("\n")
    # Beside "min recorded" on purpose, because that counter alone is
    # ambiguous: it only advances on minutes GA4 returned rows for, so a low
    # number means either a quiet site or a process started a moment ago. The
    # uptime is what separates the two. `started` is a monotonic origin rather
    # than a wall-clock stamp because what is shown is a duration -- an NTP
    # correction or a DST change mid-run would make the wall-clock form jump an
    # hour or count backwards.
    uptime = humanize_delta(time.monotonic() - started)
    line.append(
        f"{quota_text}   {shown_path} {db_bytes / 1024:.0f} KiB   "
        f"{totals.minutes_recorded} min recorded   up {uptime}   q to quit",
        style="" if no_color else "dim",
    )
    return Panel(line, padding=(0, 1), box=frame_box(ascii_mode), height=4)


def render_dashboard(
    store: RealtimeStore,
    prop: PropertyInfo,
    status: PollStatus,
    args: argparse.Namespace,
    ring: RingBufferHandler,
    no_color: bool,
    *,
    started: float,
) -> Group:
    size = shutil.get_terminal_size(fallback=(100, 30))
    width, height = size.columns, size.lines
    # Day bounds are recomputed on every render, so midnight in the property's
    # timezone rolls the view over with no restart and no special case.
    today = utcnow().astimezone(prop.tz).date()
    start, end = day_bounds_utc(today, prop.tz)

    snapshot = status.snapshot()
    totals = store.day_totals(start, end)
    events = store.top_events(start, end, limit=10)
    series = store.pageview_series(start, end)
    leads = store.lead_minutes(start, end)

    # Carry the running total out to the current minute so the line reaches the
    # right edge and visibly grows through the day. This is a fact rather than
    # an interpolation: while the poller is live a minute that produced no rows
    # genuinely had no page views, so the cumulative total really is flat.
    # The one reading it gets wrong is a stalled poller, where flat means "not
    # collected" -- which is what the header's reddening "last poll ... ago"
    # exists to say.
    now_minute = floor_minute(utcnow())
    if series and now_minute > series[-1][0]:
        series.append((now_minute, series[-1][1]))

    header = build_header(prop, snapshot, args.interval, no_color, args.ascii)
    header_h = 4 if snapshot["last_error"] else 3
    footer_h = 4

    # Height is allocated from a budget rather than assumed, because the
    # frame must never exceed the terminal: a Live region with screen=False
    # scrolls the overflow and tears the display on every refresh. Order of
    # sacrifice as the terminal shrinks: log panel, table rows, chart entirely.
    MIN_CHART = 8
    TABLE_CHROME = 5  # title, top border, header row, header rule, bottom
    MIN_TABLE = 1 + TABLE_CHROME
    available = height - header_h - footer_h

    # The log panel only appears if it still leaves room for a table.
    log_h = 0
    if args.verbose and available >= MIN_TABLE + 3:
        log_h = min(8, max(3, available // 4), available - MIN_TABLE)
    available -= log_h

    show_table = available >= MIN_TABLE
    table_rows, table_h = 0, 0
    if show_table:
        table_rows = min(10, max(1, len(events)))
        table_h = table_rows + TABLE_CHROME
        if available - table_h < MIN_CHART:
            table_h = min(available, max(MIN_TABLE, available - MIN_CHART))
        table_rows = max(1, table_h - TABLE_CHROME)
        available -= table_h

    chart_h = available
    # width - 3, not - 2: two columns go to the panel border and rich needs
    # the last one free, otherwise the plot's own right-hand frame is cropped.
    inner_w = max(20, width - 3)

    parts = [header]
    if chart_h >= 5:
        parts.append(
            Panel(
                build_chart(
                    series, prop.tz, inner_w, chart_h - 2,
                    args.ascii, no_color, "cyan", leads,
                    now=now_minute, window_minutes=args.window * 60,
                ),
                # The title is where the star gets explained; a plotext legend
                # would spend plot rows saying the same thing. It still says
                # "today" because the values remain cumulative from local
                # midnight -- only the axis is windowed, so the curve can
                # enter the window at a height rather than at zero.
                title=(
                    f"Cumulative page views today, last {args.window}h"
                    f"   * = {LEAD_EVENT}"
                ),
                padding=(0, 0),
                box=frame_box(args.ascii),
                height=chart_h,
            )
        )
    if show_table:
        parts.append(
            build_events_table(
                events[:table_rows], width, args.ascii, no_color
            )
        )
    parts.append(
        build_footer(
            totals, snapshot["quota"], store.path, store.size_bytes(),
            no_color, args.ascii, started=started,
        )
    )
    if log_h:
        tail = list(ring.records)[-max(1, log_h - 2):]
        parts.insert(
            len(parts) - 1,
            Panel(
                Text("\n".join(tail) or "(no log yet)",
                     style="" if no_color else "dim"),
                title="log",
                height=log_h,
                padding=(0, 1),
                box=frame_box(args.ascii),
            ),
        )
    return Group(*parts)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def open_store_for_reading(db_path: Path) -> RealtimeStore:
    if not db_path.exists():
        raise ConfigError(
            f"no database at {db_path}\n"
            "Run the live dashboard first so it can collect some data:\n"
            "  tools/.venv/Scripts/python.exe tools/ga4_realtime.py"
        )
    return RealtimeStore(db_path)


def resolve_day(raw: str | None, tz: ZoneInfo) -> date:
    if raw is None:
        return utcnow().astimezone(tz).date()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise ConfigError(f"--date must be YYYY-MM-DD, got {raw!r}") from None


def print_report(store: RealtimeStore, prop: PropertyInfo, day: date) -> None:
    start, end = day_bounds_utc(day, prop.tz)
    totals = store.day_totals(start, end)
    events = store.top_events(start, end, limit=10)

    print(f"GA4 realtime - {prop.display_name} ({prop.id})")
    print(f"{prop.tz_name}   {day.isoformat()}")
    print()
    print(f"  page views       {totals.page_views:>8,}")
    print(f"  events           {totals.events:>8,}")
    print(f"  {LEAD_EVENT:<15}  {totals.leads:>8,}")
    print()
    if events:
        print("  top events")
        width = max(len(name) for name, _ in events)
        for name, count in events:
            print(f"    {name:<{width}}  {count:>8,}")
        print()
    if totals.first_minute and totals.last_minute:
        first = parse_minute(totals.first_minute).astimezone(prop.tz)
        last = parse_minute(totals.last_minute).astimezone(prop.tz)
        print(
            f"  coverage: {totals.minutes_recorded} minutes recorded, "
            f"{first:%H:%M} to {last:%H:%M}"
        )
    else:
        print("  coverage: nothing recorded for this day")


def resolve_property(args: argparse.Namespace, store: RealtimeStore) -> PropertyInfo:
    """Property details for a read-only command.

    Prefers what the live view cached in the database, so reading back a
    stored day needs neither credentials nor a network round trip. Only falls
    back to the API when the cache is empty.
    """
    cached = store.load_property()
    if cached is not None:
        if args.timezone:
            try:
                return cached._replace(
                    tz_name=args.timezone, tz=ZoneInfo(args.timezone)
                )
            except ZoneInfoNotFoundError:
                raise ConfigError(
                    f"unknown timezone {args.timezone!r}"
                ) from None
        return cached
    return GA4Client(load_config()).fetch_property(args.timezone)


def cmd_report(args: argparse.Namespace) -> int:
    store = open_store_for_reading(args.db)
    try:
        prop = resolve_property(args, store)
        print_report(store, prop, resolve_day(args.date, prop.tz))
    finally:
        store.close()
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    store = open_store_for_reading(args.db)
    try:
        prop = resolve_property(args, store)
        day = resolve_day(args.date, prop.tz)
        start, end = day_bounds_utc(day, prop.tz)
        rows = store.export_rows(start, end)
        if args.format == "json":
            payload = json.dumps(
                {
                    "property_id": prop.id,
                    "property_name": prop.display_name,
                    "timezone": prop.tz_name,
                    "date": day.isoformat(),
                    "rows": rows,
                },
                indent=2,
            )
        else:
            buffer = io.StringIO()
            columns = [
                "minute_ts_utc", "event_name", "device", "country",
                "screen_page_views", "event_count",
                "first_seen_utc", "last_seen_utc",
            ]
            writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            payload = buffer.getvalue()

        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(payload, encoding="utf-8")
            print(f"wrote {len(rows)} rows to {args.out}", file=sys.stderr)
        else:
            sys.stdout.write(payload)
    finally:
        store.close()
    return 0


def cmd_live(args: argparse.Namespace, ring: RingBufferHandler) -> int:
    # Anchored ahead of the credential load and the priming poll rather than at
    # the render loop, because both can take a visible moment and the user has
    # been watching the program since here.
    started = time.monotonic()
    client = GA4Client(load_config())
    prop = client.fetch_property(args.timezone)
    no_color = bool(os.environ.get("NO_COLOR"))
    if not args.ascii and not stdout_supports_blocks():
        args.ascii = True
        log.info(
            "stdout encoding %r cannot represent block glyphs; using --ascii",
            getattr(sys.stdout, "encoding", None),
        )

    # A priming poll before anything else: it validates Data API access while
    # errors can still be reported plainly, and it means the first frame has
    # something in it instead of an empty chart.
    status = PollStatus()
    stop_event = threading.Event()
    primer = RealtimeStore(args.db)
    try:
        primer.save_property(prop)
        rows = client.poll_realtime()
        inserted, updated = primer.upsert_minutes(rows)
        primer.log_poll(len(rows), inserted, updated, None)
        status.last_success = utcnow()
        status.poll_count = 1
        status.quota = client.latest_quota
    except gexc.PermissionDenied as exc:
        raise ConfigError(
            f"the service account cannot read property {prop.id}.\n"
            "Add its e-mail (the client_email in the JSON key) as Viewer in\n"
            "GA4 -> Admin -> Property access management, and confirm the\n"
            "Google Analytics Data API is enabled in the GCP project.\n"
            f"API said: {exc.message}"
        ) from None
    finally:
        primer.close()

    if not sys.stdout.isatty():
        # Piped or redirected: the Live region would emit control sequences
        # into whatever is reading. Print the summary instead and stop.
        store = RealtimeStore(args.db)
        try:
            print_report(store, prop, utcnow().astimezone(prop.tz).date())
        finally:
            store.close()
        return 0

    poller = Poller(client, args.db, args.interval, status, stop_event)
    poller.start()

    # SIGWINCH does not exist on Windows and merely naming it raises
    # AttributeError. Where it does exist it prompts an immediate redraw;
    # where it does not, the per-render get_terminal_size() call below is
    # already enough to re-fit the plots on its own.
    if hasattr(signal, "SIGWINCH"):
        signal.signal(signal.SIGWINCH, lambda *_: None)

    console = Console(no_color=no_color, soft_wrap=False)
    store = RealtimeStore(args.db)
    try:
        with KeyReader() as keys, Live(
            console=console, screen=False, auto_refresh=False, transient=False
        ) as live:
            while not stop_event.is_set():
                live.update(
                    render_dashboard(
                        store, prop, status, args, ring, no_color,
                        started=started,
                    ),
                    refresh=True,
                )
                deadline = time.monotonic() + args.refresh
                while time.monotonic() < deadline and not stop_event.is_set():
                    key = keys.poll()
                    if key and key.lower() == "q":
                        stop_event.set()
                        break
                    if stop_event.wait(0.1):
                        break
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        poller.join(timeout=15)
        store.close()
        console.print("stopped. data is in " + str(args.db))
    return 0


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ga4_realtime.py",
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Global flags are declared before add_subparsers() so that they also work
    # on the bare invocation, which is the live dashboard.
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB,
        help=f"SQLite file (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--interval", type=int, default=DEFAULT_INTERVAL,
        help=f"seconds between polls (default: {DEFAULT_INTERVAL})",
    )
    parser.add_argument(
        "--refresh", type=int, default=DEFAULT_REFRESH,
        help=f"seconds between redraws (default: {DEFAULT_REFRESH})",
    )
    parser.add_argument(
        "--window", type=int, default=DEFAULT_WINDOW_HOURS,
        help="hours of history shown in the chart "
             f"(default: {DEFAULT_WINDOW_HOURS})",
    )
    parser.add_argument(
        "--timezone", default=None,
        help="override the property timezone, e.g. America/Sao_Paulo",
    )
    parser.add_argument(
        "--ascii", action="store_true",
        help="plain ASCII plots and bars, for terminals without braille",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="add a log panel to the view"
    )

    subparsers = parser.add_subparsers(dest="command")

    report = subparsers.add_parser("report", help="print a daily summary")
    report.add_argument("--date", help="YYYY-MM-DD (default: today)")

    export = subparsers.add_parser("export", help="dump stored rows")
    export.add_argument("--date", help="YYYY-MM-DD (default: today)")
    export.add_argument(
        "--format", choices=("csv", "json"), default="csv",
        help="output format (default: csv)",
    )
    export.add_argument(
        "--out", type=Path, default=None,
        help="write to a file instead of stdout",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.interval < 1 or args.refresh < 1:
        print("--interval and --refresh must be at least 1", file=sys.stderr)
        return 2
    # Capped at a day because past that the axis labels wrap onto clock times
    # already on it, and the same tick reads as two different moments.
    if not 1 <= args.window <= 24:
        print("--window must be between 1 and 24 hours", file=sys.stderr)
        return 2

    ring = setup_logging(args.verbose)
    try:
        # Only the live view needs the API up front. report and export read
        # the local database and resolve the property from its cache.
        if args.command == "report":
            return cmd_report(args)
        if args.command == "export":
            return cmd_export(args)
        return cmd_live(args, ring)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
