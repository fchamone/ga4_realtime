"""The storage key format, and the day boundaries computed against it.

Two jobs, and they are the same job seen from either end. Every minute this
tool records is keyed by a fixed-width UTC string produced here, and every
day-scoped query crosses from a *property-local* day into a pair of those keys
here. Storage is UTC and only display is local, so the timezone arithmetic has
to happen somewhere; doing it in Python rather than in SQL is what lets the
keys stay plain sortable text.

The module depends on nothing else in the package -- not configuration, not
storage, not the API -- because the key format is a persisted format. Changing
it silently would strand every row already written, so it is worth being able
to test on its own.
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    """Aware "now" in UTC.

    Aware rather than naive on purpose: a naive datetime compares and
    subtracts happily against an aware one right up until it raises, and
    every instant in this tool is eventually converted to the property's
    timezone, which a naive value cannot survive.
    """
    return datetime.now(UTC)


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
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:00Z")


def iso_second(moment: datetime) -> str:
    """Second-resolution stamp for the audit columns.

    Deliberately not iso_minute: first_seen/last_seen exist to show when a row
    was last corrected, and minute resolution would hide every correction made
    inside the same minute -- exactly the case worth watching.
    """
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_minute(key: str) -> datetime:
    return datetime.strptime(key, "%Y-%m-%dT%H:%M:00Z").replace(tzinfo=UTC)


def day_bounds_utc(day: date, tz: ZoneInfo) -> tuple[str, str]:
    """Half-open [start, end) of a property-local day, as UTC storage keys.

    Storage is UTC and only display is local, so every day-scoped query has to
    cross the boundary here rather than in SQL.
    """
    start_local = datetime(day.year, day.month, day.day, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return iso_minute(start_local), iso_minute(end_local)


def humanize_delta(seconds: float) -> str:
    """Render an elapsed duration in the narrowest useful form.

    Two thresholds, both at a unit boundary: under a minute is seconds alone,
    under an hour is minutes and zero-padded seconds, and above that the
    seconds are dropped entirely -- an uptime of hours does not become more
    informative for knowing it is 41 seconds into the minute, and the extra
    characters cost header width the status strip needs more.
    """
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
