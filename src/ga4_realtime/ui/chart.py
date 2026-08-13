"""The cumulative page-view plot, and the arithmetic behind its axes.

plotext draws the picture; everything else in this module exists because
plotext's own defaults are wrong for this particular chart. Left alone it
fits both axes to the data, labels a counted quantity in decimals, and picks
tick spacings that read 09:07 and 12:27. `nice_step`, `nice_time_step` and
`integer_ticks` are each one of those defaults being overruled.

`minutes_past_midnight`, `window_series` and `window_marks` are here for a
duller reason: they lived inside `build_chart` (one as a closure, two as
inline loop bodies), and what they decide -- where the window opens, what
level the curve enters it at, which conversions are inside it -- cannot be
read back off a grid of terminal cells afterwards. Testing them through a
rendered plot would be testing plotext.
"""

from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

import plotext as plt
from rich.text import Text

from .theme import _ASCII_FRAME


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


def minutes_past_midnight(moment: datetime, tz: ZoneInfo) -> int:
    """Where a UTC instant falls in one site's own day, in minutes.

    x is minutes past local midnight, which keeps the axis numeric and lets
    the tick labels be formatted explicitly rather than left to plotext's date
    parsing.

    The zone is an argument rather than a module-level default because two
    sites in one process have two different local midnights: the same instant
    is 09:00 for one and 21:00 for the other, and the axis has to be computed
    in the focused site's zone rather than in the machine's.
    """
    local = moment.astimezone(tz)
    return local.hour * 60 + local.minute


def window_series(
    points: Sequence[tuple[datetime, int]],
    tz: ZoneInfo,
    *,
    left: int,
    right: int,
) -> tuple[list[int], list[int]]:
    """Clip a cumulative series to the window, carrying the level into it.

    The series is cumulative since local midnight, so a point before `left` is
    not noise to drop -- it is the running total the window opens at. Dropping
    it outright would start the line mid-panel at the first in-window point
    and lose the flat run in from the left edge. Only the *last* such point is
    carried: the earlier ones are already summed into it.

    Points after `right` are dropped rather than clamped. There is no honest
    place to draw a future minute on an axis that ends now.
    """
    xs: list[int] = []
    ys: list[int] = []
    carried: int | None = None
    for moment, value in points:
        x = minutes_past_midnight(moment, tz)
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
    return xs, ys


def window_marks(
    moments: Sequence[datetime],
    tz: ZoneInfo,
    *,
    left: int,
    right: int,
) -> list[int]:
    """The rug's x positions: inside the window, or gone.

    Conversions outside the window are dropped rather than drawn at its edge,
    where they would claim a conversion happened at a time it did not. The rug
    is read as a timeline, so that is the one place on the chart where a
    marker is answering a factual question.
    """
    return [
        x
        for x in (minutes_past_midnight(moment, tz) for moment in moments)
        if left <= x <= right
    ]


def build_chart(
    points: Sequence[tuple[datetime, int]],
    tz: ZoneInfo,
    width: int,
    height: int,
    *,
    ascii_mode: bool,
    no_color: bool,
    color: str,
    conversion_color: str,
    conversion_minutes: Sequence[datetime] | None = None,
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

    `conversion_minutes` are marked as a rug of stars along the baseline
    rather than on the curve itself: a terminal cell is coarse enough that a
    star placed at the running total lands a row off the line as often as on
    it, and two conversions a few minutes apart overlap. On the baseline they
    are always legible. Which events count as conversions is per-site
    configuration, so the caller resolves it and passes the minutes in.
    """
    if width < 20 or height < 5:
        return Text("(terminal too small)", style="dim")
    if not points:
        return Text("\n" * (height - 1) + "  no data yet", style="dim")

    plt.clear_figure()
    plt.plotsize(width, height)
    plt.theme("clear" if (ascii_mode or no_color) else "pro")

    # The trailing window, in minutes past local midnight. `left` is negative
    # until the day is itself window_minutes old -- at 02:10 with a 6h window
    # it is -230, which reads as 20:10 yesterday. That region is drawn empty
    # rather than clipped away: the series is day-scoped so there is genuinely
    # nothing there, and keeping the full width is what stops the axis from
    # rescaling through the early hours.
    right = minutes_past_midnight(now, tz)
    left = right - window_minutes

    xs, ys = window_series(points, tz, left=left, right=right)
    if not xs:
        return Text("\n" * (height - 1) + "  no data in window", style="dim")

    # In --ascii the line is drawn with dots rather than stars, so the
    # conversion markers below stay distinguishable from the curve itself.
    marker = "." if ascii_mode else "braille"
    if no_color:
        plt.plot(xs, ys, marker=marker)
    else:
        plt.plot(xs, ys, marker=marker, color=color)

    # After plot(), so the stars paint over the line where the two meet.
    if conversion_minutes:
        marks = window_marks(conversion_minutes, tz, left=left, right=right)
        if marks:
            baseline = [0] * len(marks)
            if no_color:
                plt.scatter(marks, baseline, marker="*")
            else:
                plt.scatter(
                    marks, baseline, marker="*", color=conversion_color
                )

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
