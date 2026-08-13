"""The storage key format and the day boundaries built on it.

These are persisted-format tests, not unit tests for their own sake. The
minute key is written into every row of `realtime_minute`; if its width or its
ordering ever changed, every range query in the tool would go quietly wrong
against data already on disk rather than fail.
"""

import random
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ga4_realtime.timeutil import (
    day_bounds_utc,
    floor_minute,
    humanize_delta,
    iso_minute,
    iso_second,
    parse_minute,
    utcnow,
)

# America/New_York observes DST and both of its 2026 transitions fall on a
# date this test can name outright: spring forward on 8 March, back on
# 1 November. A zone without DST would make the two interesting cases
# indistinguishable from the ordinary one.
DST_ZONE = ZoneInfo("America/New_York")
SPRING_FORWARD = date(2026, 3, 8)
FALL_BACK = date(2026, 11, 1)


# --------------------------------------------------------------------------
# utcnow
# --------------------------------------------------------------------------


def test_utcnow_is_aware_and_utc():
    now = utcnow()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


# --------------------------------------------------------------------------
# floor_minute -- the truncation the UPSERT depends on
# --------------------------------------------------------------------------


def test_floor_minute_drops_seconds_and_microseconds():
    moment = datetime(2026, 8, 13, 14, 37, 42, 123456, tzinfo=UTC)
    assert floor_minute(moment) == datetime(2026, 8, 13, 14, 37, tzinfo=UTC)


def test_floor_minute_keeps_everything_above_the_minute():
    moment = datetime(2026, 8, 13, 14, 37, 59, 999999, tzinfo=UTC)
    floored = floor_minute(moment)
    assert (floored.year, floored.month, floored.day) == (2026, 8, 13)
    assert (floored.hour, floored.minute) == (14, 37)


def test_floor_minute_is_idempotent():
    moment = datetime(2026, 8, 13, 14, 37, 42, 123456, tzinfo=UTC)
    assert floor_minute(floor_minute(moment)) == floor_minute(moment)


def test_floor_minute_preserves_tzinfo():
    tz = ZoneInfo("America/Sao_Paulo")
    moment = datetime(2026, 8, 13, 14, 37, 42, tzinfo=tz)
    assert floor_minute(moment).tzinfo is tz


def test_two_polls_of_one_minute_produce_the_same_key():
    """The failure floor_minute exists to prevent, stated as a test.

    Two polls seconds apart inside one real minute have to yield one key. If
    they did not, ON CONFLICT would never match and every poll would insert a
    duplicate instead of correcting the row already there.
    """
    first = datetime(2026, 8, 13, 14, 37, 2, tzinfo=UTC)
    second = datetime(2026, 8, 13, 14, 37, 58, 900000, tzinfo=UTC)
    assert iso_minute(floor_minute(first)) == iso_minute(floor_minute(second))


# --------------------------------------------------------------------------
# iso_minute -- fixed width, UTC, sortable as text
# --------------------------------------------------------------------------


def test_iso_minute_format():
    moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert iso_minute(moment) == "2026-01-02T03:04:00Z"


def test_iso_minute_is_fixed_width():
    moments = [
        datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 12, 31, 23, 59, tzinfo=UTC),
        datetime(2026, 9, 9, 9, 9, tzinfo=UTC),
        datetime(2000, 10, 10, 10, 10, tzinfo=UTC),
    ]
    assert {len(iso_minute(m)) for m in moments} == {20}


def test_iso_minute_converts_to_utc():
    tz = ZoneInfo("America/Sao_Paulo")  # UTC-3, no DST since 2019
    local = datetime(2026, 8, 13, 21, 30, tzinfo=tz)
    assert iso_minute(local) == "2026-08-14T00:30:00Z"


def test_iso_minute_sorts_lexicographically_as_it_does_chronologically():
    """The property that lets SQL range over the keys as plain strings.

    Spans a year boundary on purpose: that is where a format without
    zero-padding, or with a variable-width year, would come apart.
    """
    start = datetime(2025, 12, 31, 23, 55, tzinfo=UTC)
    moments = [start + timedelta(minutes=i) for i in range(20)]
    moments += [start + timedelta(days=d) for d in (-400, -1, 1, 400)]

    shuffled = list(moments)
    random.Random(0).shuffle(shuffled)

    assert sorted(iso_minute(m) for m in shuffled) == [
        iso_minute(m) for m in sorted(moments)
    ]


def test_iso_minute_crosses_the_year_boundary_in_order():
    before = iso_minute(datetime(2025, 12, 31, 23, 59, tzinfo=UTC))
    after = iso_minute(datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
    assert before < after
    assert before == "2025-12-31T23:59:00Z"
    assert after == "2026-01-01T00:00:00Z"


# --------------------------------------------------------------------------
# iso_second and parse_minute
# --------------------------------------------------------------------------


def test_iso_second_keeps_the_seconds_iso_minute_throws_away():
    moment = datetime(2026, 8, 13, 14, 37, 42, tzinfo=UTC)
    assert iso_second(moment) == "2026-08-13T14:37:42Z"
    assert iso_minute(moment) == "2026-08-13T14:37:00Z"


def test_iso_second_is_fixed_width_too():
    assert len(iso_second(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))) == 20


def test_parse_minute_round_trips():
    moment = datetime(2026, 8, 13, 14, 37, tzinfo=UTC)
    assert parse_minute(iso_minute(moment)) == moment
    assert parse_minute(iso_minute(moment)).tzinfo is UTC


def test_parse_minute_rejects_a_second_resolution_stamp():
    with pytest.raises(ValueError):
        parse_minute("2026-08-13T14:37:42Z")


# --------------------------------------------------------------------------
# day_bounds_utc -- including the two days of the year that are not 24 hours
# --------------------------------------------------------------------------


def _span(day: date, tz: ZoneInfo) -> timedelta:
    start, end = day_bounds_utc(day, tz)
    return parse_minute(end) - parse_minute(start)


def test_day_bounds_are_half_open_and_meet_the_next_day():
    day = date(2026, 8, 13)
    _, end = day_bounds_utc(day, DST_ZONE)
    next_start, _ = day_bounds_utc(day + timedelta(days=1), DST_ZONE)
    assert end == next_start


def test_ordinary_day_is_24_hours():
    assert _span(date(2026, 8, 13), DST_ZONE) == timedelta(hours=24)


def test_utc_day_starts_at_midnight_utc():
    start, end = day_bounds_utc(date(2026, 8, 13), ZoneInfo("UTC"))
    assert start == "2026-08-13T00:00:00Z"
    assert end == "2026-08-14T00:00:00Z"


def test_local_day_is_offset_from_the_utc_day():
    """The reason the crossing happens here and not in SQL.

    A Sao Paulo day begins at 03:00 UTC, so a query written against UTC
    midnights would attribute three hours of every evening to the wrong day.
    """
    tz = ZoneInfo("America/Sao_Paulo")
    start, end = day_bounds_utc(date(2026, 8, 13), tz)
    assert start == "2026-08-13T03:00:00Z"
    assert end == "2026-08-14T03:00:00Z"


def test_spring_forward_day_is_23_hours():
    assert _span(SPRING_FORWARD, DST_ZONE) == timedelta(hours=23)


def test_fall_back_day_is_25_hours():
    assert _span(FALL_BACK, DST_ZONE) == timedelta(hours=25)


def test_dst_days_still_meet_their_neighbours():
    """The short and the long day tile the calendar with no gap or overlap.

    Worth asserting separately from the spans: a bound computed from a fixed
    UTC offset instead of the zone would give the right *length* for the
    ordinary days on either side while leaving an hour unattributed here.
    """
    for day in (SPRING_FORWARD, FALL_BACK):
        _, end = day_bounds_utc(day, DST_ZONE)
        next_start, _ = day_bounds_utc(day + timedelta(days=1), DST_ZONE)
        assert end == next_start


# --------------------------------------------------------------------------
# humanize_delta -- checked either side of both unit boundaries
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (1, "1s"),
        (59, "59s"),
        (60, "1m00s"),
        (61, "1m01s"),
        (3599, "59m59s"),
        (3600, "1h00m"),
        (3660, "1h01m"),
        (86399, "23h59m"),
        (86400, "24h00m"),
    ],
)
def test_humanize_delta_at_and_around_the_unit_boundaries(seconds, expected):
    assert humanize_delta(seconds) == expected


def test_humanize_delta_truncates_a_float():
    assert humanize_delta(59.9) == "59s"
    assert humanize_delta(60.9) == "1m00s"


# --------------------------------------------------------------------------
# The network guard from conftest.py
# --------------------------------------------------------------------------
# Its own test lives here because this is the only test module in the task
# that installed the guard. It moves to whichever file first exercises the
# real client if that ever reads better -- but a guard nothing asserts is a
# guard that can be deleted by accident.


def test_data_api_client_cannot_be_constructed_in_a_test():
    from google.analytics.data_v1beta import BetaAnalyticsDataClient

    with pytest.raises(RuntimeError, match="No test may reach"):
        BetaAnalyticsDataClient()


def test_admin_api_client_cannot_be_constructed_in_a_test():
    from google.analytics.admin import AnalyticsAdminServiceClient

    with pytest.raises(RuntimeError, match="No test may reach"):
        AnalyticsAdminServiceClient()


def test_the_guard_names_the_client_it_blocked():
    from google.analytics.admin import AnalyticsAdminServiceClient

    with pytest.raises(RuntimeError) as caught:
        AnalyticsAdminServiceClient(credentials=object())
    assert "AnalyticsAdminServiceClient" in str(caught.value)
