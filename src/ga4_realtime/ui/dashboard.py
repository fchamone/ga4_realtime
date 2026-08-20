"""One frame of the live view, and the budget that decides what fits in it.

**The rendered frame must never exceed the terminal.** A `Live(screen=False)`
region scrolls whatever overflows and tears the display on every refresh from
then on, which does not look like a layout bug -- it looks like the tool is
broken. So height is allocated from a budget rather than assumed, and when the
budget runs short the order of sacrifice is fixed: the log panel goes first,
then table rows, then the chart entirely. The header and the footer are never
sacrificed, because a frame that cannot say which site it is about or how
stale the numbers are is not worth the rows it saved.

`width` and `height` are arguments rather than a `shutil.get_terminal_size()`
call in here. The size is what the entire budget is computed against, and a
value the caller cannot state is a value the test sweep cannot vary.
"""

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text

from ..config import SiteConfig, UiConfig
from ..logging_setup import RingBufferHandler
from ..store import RealtimeStore, SiteMeta
from ..timeutil import day_bounds_utc, floor_minute, utcnow
from .chart import build_chart
from .panels import (
    FOOTER_HEIGHT,
    TABLE_CHROME,
    build_events_table,
    build_footer,
    build_header,
    build_status_strip,
    header_height,
    site_identity,
)
from .theme import frame_box

# The rows the chart is protected down to: a table longer than this leaves
# room for is trimmed instead, because a plot with no room for both an axis
# and a curve is worse than no plot at all. Below five rows of panel there is
# not even that, and the chart is dropped entirely rather than drawn as a
# sliver -- which is the last of the three sacrifices.
MIN_CHART = 8
# One row of data plus the table's own furniture: the smallest table that
# still says something.
MIN_TABLE = 1 + TABLE_CHROME

# What a site that has not been polled yet looks like. `statuses` is missing
# the entry rather than holding an empty one, because that is what a PollerSet
# looks like between construction and the first poll returning -- and a
# KeyError there would take down the first frame, which is the one the user is
# watching most closely.
_NEVER_POLLED: dict[str, Any] = {
    "last_success": None,
    "last_error": None,
    "in_flight": False,
    "poll_count": 0,
    "error_count": 0,
    "quota": None,
}


class StatusLike(Protocol):
    """What the dashboard needs from a poller's status: one snapshot.

    A structural type rather than an import of `poller.PollStatus`. The
    collector is deliberately free of any UI dependency so a future headless
    `collect` can reuse it whole, and importing it from here would close that
    circle from the other side. What a frame reads is a consistent snapshot
    taken under the status's own lock, so that is what is asked for.
    """

    def snapshot(self) -> dict[str, Any]: ...


def _focused(sites: Sequence[SiteConfig], focus: str) -> SiteConfig:
    """The site the frame is about, or the first one.

    The caller cycles `focus` through this same list, so a name that is not in
    it is a programming error -- but a render is the worst possible place to
    raise, since the exception costs the whole display rather than one frame.
    Showing the first site is wrong in a way the header immediately admits to.
    """
    for site in sites:
        if site.name == focus:
            return site
    return sites[0]


def _snapshot(statuses: Mapping[str, StatusLike], site: str) -> dict[str, Any]:
    status = statuses.get(site)
    return dict(_NEVER_POLLED) if status is None else status.snapshot()


def site_counters(
    store: RealtimeStore,
    sites: Sequence[SiteConfig],
    metas: Mapping[str, SiteMeta],
) -> dict[str, int | None]:
    """Each site's count of its own counter event, for its own day so far.

    Every site's *own* day, and that is the whole reason this is a loop rather
    than one query: the sites in a config can sit in any number of timezones,
    so "today" starts at a different UTC instant for each of them. A single
    grouped read would have to pick one site's midnight and quietly report the
    others against it -- a number that is wrong by up to a day and looks
    entirely plausible.

    The zone comes through `site_identity` rather than off `site.timezone`
    directly, so a site's counted day and the day its own header names resolve
    through one function and cannot drift apart -- including the fallbacks,
    where the config is silent and the cached property timezone answers.

    None for a site with no `counter_event`, which the strip renders as no
    counter at all. Distinct from 0, which is a site that is being counted and
    has had no traffic today.
    """
    counters: dict[str, int | None] = {}
    for site in sites:
        if site.counter_event is None:
            counters[site.name] = None
            continue
        identity = site_identity(site, metas.get(site.name))
        today = utcnow().astimezone(identity.tz).date()
        start, end = day_bounds_utc(today, identity.tz)
        counters[site.name] = store.event_total(
            site.name, site.counter_event, start, end
        )
    return counters


def render_dashboard(
    store: RealtimeStore,
    sites: Sequence[SiteConfig],
    statuses: Mapping[str, StatusLike],
    ui: UiConfig,
    ring: RingBufferHandler | None,
    *,
    focus: str,
    width: int,
    height: int,
    verbose: bool,
    no_color: bool,
    started: float,
) -> Group:
    """Build one frame, for one site, inside one terminal.

    Everything day-scoped on screen -- the footer's totals, the events table,
    the chart's cumulative series -- belongs to the *focused* site's local day,
    and the chart is drawn in that site's timezone and its own colour. Two
    sites in different timezones roll over at different moments, so Tab can
    move between a site at 23:40 and one already at 04:40 the next day. That is
    correct, and the header's timezone and clock are what make it legible.

    Every *other* site reaches the frame three times, and only three times: as
    one glyph in the header's status strip, as the count beside that glyph,
    and as its own lines in the --verbose log panel. All three exist for the
    same reason -- a site nobody has pressed Tab to must not be able to fail
    quietly -- and the count is the one that catches the failure the marker
    cannot see, where polls keep succeeding and bring back nothing.
    """
    site = _focused(sites, focus)

    # One read for every site's cached property row, not one read per site.
    # `list_sites` is a single statement and therefore a single snapshot, so
    # the day the header names and the days the strip's counts are measured
    # over all come from one instant; twelve separate `load_site` calls could
    # straddle a poll landing mid-frame. Same reasoning as `snapshots` below,
    # and the same idiom `commands/sites.py` already uses.
    metas = {meta.site: meta for meta in store.list_sites()}
    identity = site_identity(site, metas.get(site.name))

    # Day bounds are recomputed on every render, so midnight in the focused
    # site's timezone rolls the view over with no restart and no special case.
    today = utcnow().astimezone(identity.tz).date()
    start, end = day_bounds_utc(today, identity.tz)

    # One snapshot per site, taken once for the whole frame. Each is a lock
    # acquisition on a live poller's status, and a frame that read the same
    # site twice could show a marker in the strip and an age in the header
    # that describe two different instants.
    snapshots = {
        other.name: _snapshot(statuses, other.name) for other in sites
    }
    snapshot = snapshots[site.name]
    totals = store.day_totals(site.name, site.conversions, start, end)
    events = store.top_events(site.name, start, end, limit=ui.top_events)
    series = store.pageview_series(site.name, start, end)
    conversion_minutes = store.conversion_minutes(
        site.name, site.conversions, start, end
    )

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

    header = build_header(
        identity,
        snapshot,
        site.interval,
        no_color,
        ui.ascii,
        strip=build_status_strip(
            sites,
            snapshots,
            counters=site_counters(store, sites, metas),
            # `site.name` and not `focus`: an unknown focus falls back to the
            # first site, and the strip has to emphasise the site whose
            # numbers are actually on screen rather than the one that was
            # asked for.
            focus=site.name,
            width=width,
            no_color=no_color,
            ascii_mode=ui.ascii,
        ),
    )
    header_h = header_height(snapshot)

    # Height is allocated from a budget rather than assumed, because the
    # frame must never exceed the terminal: a Live region with screen=False
    # scrolls the overflow and tears the display on every refresh. Order of
    # sacrifice as the terminal shrinks: log panel, table rows, chart entirely.
    available = height - header_h - FOOTER_HEIGHT

    # The log panel only appears if it still leaves room for a table.
    log_h = 0
    if verbose and ring is not None and available >= MIN_TABLE + 3:
        log_h = min(8, max(3, available // 4), available - MIN_TABLE)
    available -= log_h

    show_table = available >= MIN_TABLE
    table_rows, table_h = 0, 0
    if show_table:
        table_rows = min(ui.top_events, max(1, len(events)))
        table_h = table_rows + TABLE_CHROME
        if available - table_h < MIN_CHART:
            table_h = min(available, max(MIN_TABLE, available - MIN_CHART))
        table_rows = max(1, table_h - TABLE_CHROME)
        available -= table_h

    chart_h = available
    # width - 3, not - 2: two columns go to the panel border and rich needs
    # the last one free, otherwise the plot's own right-hand frame is cropped.
    inner_w = max(20, width - 3)

    parts: list[RenderableType] = [header]
    # Five rows is a bordered panel with one line inside it. Between five and
    # seven that line is build_chart's own "(terminal too small)", which is a
    # better use of the rows than the blank ones dropping the panel would
    # leave behind -- nothing else claims them, since the chart is last.
    if chart_h >= 5:
        parts.append(
            Panel(
                build_chart(
                    series,
                    identity.tz,
                    inner_w,
                    chart_h - 2,
                    ascii_mode=ui.ascii,
                    no_color=no_color,
                    color=site.color,
                    conversion_color=ui.conversion_color,
                    conversion_minutes=conversion_minutes,
                    now=now_minute,
                    window_minutes=ui.window * 60,
                ),
                # The title is where the star gets explained; a plotext legend
                # would spend plot rows saying the same thing. It still says
                # "today" because the values remain cumulative from local
                # midnight -- only the axis is windowed, so the curve can
                # enter the window at a height rather than at zero. Rich
                # truncates a title too long for the width, so naming every
                # configured conversion costs nothing on a narrow terminal.
                title=(
                    f"Cumulative page views today, last {ui.window}h"
                    f"   * = {', '.join(site.conversions)}"
                ),
                padding=(0, 0),
                box=frame_box(ui.ascii),
                height=chart_h,
            )
        )
    if show_table:
        parts.append(
            build_events_table(
                events[:table_rows],
                width,
                site.conversions,
                ui.ascii,
                no_color,
                color=site.color,
                conversion_color=ui.conversion_color,
            )
        )
    parts.append(
        build_footer(
            totals,
            snapshot["quota"],
            store.path,
            store.size_bytes(),
            no_color,
            ui.ascii,
            started=started,
            conversion_color=ui.conversion_color,
        )
    )
    if log_h and ring is not None:
        # Every site's lines, interleaved, each carrying the site prefix the
        # ring buffer already rendered into it. A panel showing only the
        # focused site would hide exactly the site worth looking at.
        tail = list(ring.records)[-max(1, log_h - 2) :]
        parts.insert(
            len(parts) - 1,
            Panel(
                # no_wrap for the same reason as every other panel: one record
                # per row, so the tail that is shown is the tail that was
                # counted against the budget.
                Text(
                    "\n".join(tail) or "(no log yet)",
                    style="" if no_color else "dim",
                    no_wrap=True,
                    overflow="crop",
                ),
                title="log",
                height=log_h,
                padding=(0, 1),
                box=frame_box(ui.ascii),
            ),
        )
    return Group(*parts)
