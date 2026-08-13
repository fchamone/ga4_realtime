"""The chart's arithmetic, written before the module it tests exists.

**This file is red on purpose.** `ga4_realtime.ui.chart` does not exist yet;
it lands in T-013, which moves `nice_step`, `nice_time_step`, `integer_ticks`
and `build_chart` out of the pre-split single file. Spec §14.4 puts the tests
first so the behaviour is pinned *while* the code changes house, rather than
described afterwards by whatever survived the move.

Every import of the module under test is therefore deferred into the test
body. A module-level `from ga4_realtime.ui.chart import ...` would collapse
the whole file into one collection error, and `pytest --collect-only` listing
the cases is half of what writing it first is for.

Three functions the single file kept inline are asked for by name here, because
the properties the spec names cannot be observed through a rendered plot:

    minutes_past_midnight(moment, tz) -> int
    window_series(points, tz, *, left, right) -> tuple[list[int], list[int]]
    window_marks(moments, tz, *, left, right) -> list[int]

`window_series` is the clip-and-carry the chart's x axis depends on, and
`window_marks` is the conversion rug. Both were closures over `build_chart`'s
locals; reading their answers off a grid of terminal cells would test plotext
rather than this project.
"""

import inspect
from datetime import UTC, datetime
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest

# São Paulo is UTC-3 and, since 2019, observes no DST -- so a local wall clock
# in these tests converts to UTC by one fixed offset and the arithmetic under
# test is never accidentally about a transition. Tokyo is here to prove the
# same instant lands at a different minute-past-midnight when the site's own
# timezone says so.
TZ = ZoneInfo("America/Sao_Paulo")
TOKYO = ZoneInfo("Asia/Tokyo")

# A fixed day, never "today": every value below is a minute past *local*
# midnight, and a test that derived them from the clock would prove less and
# fail at midnight.
DAY = (2026, 8, 5)

# The tick spacings `nice_time_step` is allowed to return. Every one of them
# divides 1440, which is what "divides the clock" means: labels land on a
# round minute or a round hour instead of drifting to 09:07, 12:27, 15:47.
CLOCK_STEPS = (1, 2, 5, 10, 15, 20, 30, 60, 120, 180, 240, 360, 720, 1440)


def at(hour: int, minute: int = 0, *, tz: ZoneInfo = TZ) -> datetime:
    """A local wall-clock moment on the fixed day, as an aware UTC instant.

    Stored keys are UTC and only display is local, so the points a chart is
    handed always arrive in UTC. Writing them as a local time and converting
    is what makes the expected x values readable.
    """
    return datetime(*DAY, hour, minute, tzinfo=tz).astimezone(UTC)


def chart_module():
    """Import the module under test, deferred so collection still works."""
    from ga4_realtime.ui import chart

    return chart


def draw(
    chart,
    points,
    *,
    width: int = 80,
    height: int = 12,
    now: datetime | None = None,
    window_minutes: int = 360,
    conversion_minutes=None,
    ascii_mode: bool = False,
    no_color: bool = False,
):
    """Call `build_chart` with everything but the argument under test fixed.

    The target signature, spelled out once here so the calls below stay about
    what they are asserting::

        build_chart(
            points, tz, width, height,
            *,
            ascii_mode, no_color, color, conversion_color,
            conversion_minutes=None, now, window_minutes,
        ) -> rich.text.Text

    `color` and `conversion_color` are both passed in because they are per-site
    and `[ui]` configuration now; `LEAD_EVENT` and the hardcoded green are
    gone.
    """
    return chart.build_chart(
        points,
        TZ,
        width,
        height,
        ascii_mode=ascii_mode,
        no_color=no_color,
        color="cyan",
        conversion_color="green",
        conversion_minutes=conversion_minutes,
        now=at(10) if now is None else now,
        window_minutes=window_minutes,
    )


def is_one_two_five(value: int) -> bool:
    """True for 1, 2 or 5 times a power of ten, and nothing else."""
    if value <= 0:
        return False
    while value % 10 == 0:
        value //= 10
    return value in (1, 2, 5)


# --------------------------------------------------------------------------
# nice_step -- 1, 2 or 5 times a power of ten
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "minimum", [1, 2, 3, 4, 6, 7, 9, 11, 21, 37, 51, 99, 101, 1234, 9999]
)
def test_nice_step_returns_only_one_two_or_five_times_a_power_of_ten(
    minimum,
):
    chart = chart_module()

    step = chart.nice_step(minimum)

    assert is_one_two_five(step), f"{step} is not 1/2/5 x 10^n"
    assert step >= minimum


def test_nice_step_never_returns_below_the_minimum_it_was_asked_for():
    """A step under the minimum crowds the axis it was sized to fit."""
    chart = chart_module()

    for minimum in range(1, 300):
        assert chart.nice_step(minimum) >= minimum


def test_nice_step_is_monotonic():
    chart = chart_module()

    steps = [chart.nice_step(minimum) for minimum in range(1, 300)]

    assert steps == sorted(steps)


def test_nice_step_rounds_the_docstrings_own_example():
    """A chart topping out at 35 over five slots steps by 10, not by 7.

    The rejected reading is 0/7/14/21/28/35, which is what the arithmetic
    produces on its own and what nobody can read at a glance.
    """
    chart = chart_module()

    assert chart.nice_step(7) == 10


# --------------------------------------------------------------------------
# nice_time_step -- only spacings that divide the clock
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "minimum", [1, 3, 4, 7, 12, 17, 25, 45, 61, 100, 200, 400, 700, 900]
)
def test_nice_time_step_returns_only_clock_dividing_values(minimum):
    chart = chart_module()

    step = chart.nice_time_step(minimum)

    assert step in CLOCK_STEPS
    assert 1440 % step == 0, f"{step} does not divide the day"


def test_nice_time_step_covers_the_minimum_within_half_a_day():
    chart = chart_module()

    for minimum in range(1, 721):
        assert chart.nice_time_step(minimum) >= minimum


def test_nice_time_step_refuses_the_decimal_ladders_answer():
    """200 minutes labels the axis 09:07, 12:27, 15:47 -- hence this function.

    `nice_step` is right for counted values and wrong for clock time, and 200
    is the value that shows the difference.
    """
    chart = chart_module()

    step = chart.nice_time_step(200)

    assert step != 200
    assert step == 240


def test_nice_time_step_is_monotonic():
    chart = chart_module()

    steps = [chart.nice_time_step(minimum) for minimum in range(1, 800)]

    assert steps == sorted(steps)


# --------------------------------------------------------------------------
# integer_ticks -- whole numbers, from zero, reaching the peak
# --------------------------------------------------------------------------


@pytest.mark.parametrize("top", [0, 1, 2, 3, 5, 6, 7, 35, 99, 1000])
@pytest.mark.parametrize("height", [5, 8, 12, 20, 40])
def test_integer_ticks_are_whole_numbers_from_zero_reaching_the_top(
    top, height
):
    """Page views are counted, not measured.

    Left to itself plotext labels a chart that peaked at 6 with 5.50 and 6.00,
    which claims half a page view happened.
    """
    chart = chart_module()

    ticks = chart.integer_ticks(top, height)

    assert ticks, "an axis with no ticks has no labels"
    assert all(isinstance(t, int) and not isinstance(t, bool) for t in ticks)
    assert ticks[0] == 0, "the y axis is anchored at zero, not at the data"
    assert ticks[-1] >= top, "the top of the axis must reach the peak"
    assert ticks == sorted(set(ticks))


@pytest.mark.parametrize("top", [1, 6, 35, 99, 1000])
@pytest.mark.parametrize("height", [5, 8, 12, 20, 40])
def test_integer_ticks_are_evenly_spaced_by_a_nice_step(top, height):
    chart = chart_module()

    ticks = chart.integer_ticks(top, height)

    steps = {b - a for a, b in pairwise(ticks)}
    assert len(steps) == 1, f"uneven spacing: {ticks}"
    assert is_one_two_five(steps.pop())


def test_integer_ticks_survive_a_day_with_no_data():
    """`top` is 0 before the first poll lands, and an axis is still drawn."""
    chart = chart_module()

    ticks = chart.integer_ticks(0, 12)

    assert ticks[0] == 0
    assert ticks[-1] >= 1


def test_integer_ticks_thin_out_on_a_short_panel():
    """A short chart must not crowd its own labels."""
    chart = chart_module()

    short = chart.integer_ticks(1000, 5)
    tall = chart.integer_ticks(1000, 40)

    assert len(short) <= len(tall)


# --------------------------------------------------------------------------
# minutes_past_midnight -- the x axis is in the site's own timezone
# --------------------------------------------------------------------------


def test_minutes_past_midnight_counts_from_local_midnight():
    chart = chart_module()

    assert chart.minutes_past_midnight(at(0, 0), TZ) == 0
    assert chart.minutes_past_midnight(at(9, 7), TZ) == 547
    assert chart.minutes_past_midnight(at(23, 59), TZ) == 1439


def test_minutes_past_midnight_is_relative_to_the_zone_it_is_given():
    """The same instant is a different time of day for a different site.

    Two sites in one process roll over at different moments, so the axis has
    to be computed in the focused site's zone rather than in the machine's.
    """
    chart = chart_module()

    instant = at(9, 0)

    assert chart.minutes_past_midnight(instant, TZ) == 540
    assert chart.minutes_past_midnight(instant, TOKYO) == 1260


# --------------------------------------------------------------------------
# window_series -- the window clips, but carries the level entering it
# --------------------------------------------------------------------------


def test_window_series_keeps_points_inside_the_window_unchanged():
    chart = chart_module()

    points = [(at(9, 0), 20), (at(9, 30), 30), (at(10, 0), 40)]

    xs, ys = chart.window_series(points, TZ, left=510, right=600)

    assert xs == [540, 570, 600]
    assert ys == [20, 30, 40]


def test_window_series_carries_the_level_entering_the_window():
    """A point before the window is the running total the window opens at.

    The series is cumulative since local midnight, so an earlier point is not
    noise to drop. Dropping it starts the line mid-panel at the first in-window
    point and loses the flat run in from the left edge.
    """
    chart = chart_module()

    points = [(at(6, 0), 7), (at(9, 0), 9)]

    xs, ys = chart.window_series(points, TZ, left=510, right=600)

    assert xs == [510, 540]
    assert ys == [7, 9], "the carried level is the total, not zero"


def test_window_series_carries_the_last_level_before_the_window():
    chart = chart_module()

    points = [(at(6, 0), 3), (at(7, 0), 5), (at(8, 0), 7), (at(9, 0), 9)]

    xs, ys = chart.window_series(points, TZ, left=510, right=600)

    assert xs == [510, 540]
    assert ys == [7, 9], "the entry level is the latest point, not the first"


def test_window_series_draws_a_flat_line_when_everything_predates_it():
    """Nothing recorded in the last six hours still means a total to show."""
    chart = chart_module()

    xs, ys = chart.window_series([(at(6, 0), 4)], TZ, left=510, right=600)

    assert (xs, ys) == ([510], [4])


def test_window_series_drops_points_after_the_right_edge():
    chart = chart_module()

    points = [(at(9, 0), 9), (at(11, 0), 11)]

    xs, ys = chart.window_series(points, TZ, left=510, right=600)

    assert xs == [540]
    assert ys == [9]


def test_window_series_on_no_points_is_empty():
    chart = chart_module()

    assert chart.window_series([], TZ, left=510, right=600) == ([], [])


def test_window_series_uses_the_zone_it_is_given():
    chart = chart_module()

    points = [(at(9, 0), 9)]

    xs, _ = chart.window_series(points, TOKYO, left=1200, right=1320)

    assert xs == [1260]


# --------------------------------------------------------------------------
# window_marks -- conversions outside the window are dropped, not clamped
# --------------------------------------------------------------------------


def test_window_marks_keeps_only_the_conversions_inside_the_window():
    """Clamping to the edge would claim a conversion at a time it did not
    happen -- and the rug is read as a timeline, so that is a lie the chart
    tells in the one place it is asked a factual question."""
    chart = chart_module()

    moments = [at(8, 0), at(9, 0), at(11, 0)]

    marks = chart.window_marks(moments, TZ, left=510, right=600)

    assert marks == [540]
    assert 510 not in marks, "an earlier conversion was clamped to the edge"
    assert 600 not in marks, "a later conversion was clamped to the edge"


def test_window_marks_keeps_the_edges_themselves():
    chart = chart_module()

    moments = [at(8, 30), at(10, 0)]

    marks = chart.window_marks(moments, TZ, left=510, right=600)

    assert marks == [510, 600]


def test_window_marks_on_no_conversions_is_empty():
    chart = chart_module()

    assert chart.window_marks([], TZ, left=510, right=600) == []


# --------------------------------------------------------------------------
# build_chart -- the messages, and the frame it is allowed to occupy
# --------------------------------------------------------------------------


def test_build_chart_says_so_when_the_terminal_is_too_small():
    chart = chart_module()

    assert "too small" in draw(chart, [], width=10, height=12).plain
    assert "too small" in draw(chart, [], width=80, height=4).plain


def test_build_chart_says_so_before_the_first_poll_lands():
    chart = chart_module()

    text = draw(chart, [])

    assert "no data yet" in text.plain


def test_build_chart_says_so_when_the_window_holds_nothing():
    """Distinct from "no data yet": there are points, just none in view.

    Every point here is *after* `now`, because a point before the window is
    carried into it rather than dropped -- so future points are the only way
    to empty the window.
    """
    chart = chart_module()

    points = [(at(11, 0), 5), (at(11, 30), 6)]

    text = draw(chart, points, now=at(10), window_minutes=360)

    assert "no data in window" in text.plain


@pytest.mark.parametrize(
    ("width", "height"), [(40, 8), (60, 10), (80, 16), (140, 24)]
)
def test_build_chart_never_draws_more_lines_than_it_was_given(width, height):
    """The height budget reaches into the chart too.

    `build()` ends with a newline that would cost a row of the panel and push
    the x-axis labels out of view, and plotext can come back a column wider
    than it was asked for -- hence the rstrip and the no_wrap/crop.
    """
    chart = chart_module()

    points = [(at(6, 0), 1), (at(8, 0), 12), (at(9, 30), 20), (at(10, 0), 25)]

    text = draw(
        chart,
        points,
        width=width,
        height=height,
        conversion_minutes=[at(7, 0), at(9, 0)],
    )

    assert len(text.plain.split("\n")) <= height


@pytest.mark.parametrize("ascii_mode", [False, True], ids=["unicode", "ascii"])
def test_build_chart_renders_in_both_themes(ascii_mode):
    chart = chart_module()

    points = [(at(8, 0), 3), (at(9, 0), 9), (at(10, 0), 14)]

    text = draw(chart, points, ascii_mode=ascii_mode)

    assert text.plain.strip(), "an empty plot is not a rendered plot"


def test_build_chart_keeps_now_and_window_keyword_only_with_no_defaults():
    """So they cannot drift from the --window flag the caller was given.

    A default for either would be a second source of truth for the axis, and
    the one that is wrong is the one nobody passes.
    """
    chart = chart_module()

    parameters = inspect.signature(chart.build_chart).parameters

    for name in ("now", "window_minutes"):
        parameter = parameters[name]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty
