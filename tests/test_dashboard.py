"""The one invariant a refactor is most likely to break, swept exhaustively.

**This file is red on purpose.** `ga4_realtime.ui.dashboard` does not exist
yet; it lands in T-013, which moves `render_dashboard` and its height budget
out of the pre-split single file. Spec §14.4 requires the sweep to exist
*first*, so the budget is protected while 700 lines of layout arithmetic
change house -- writing it afterwards protects nothing during the window where
it would actually break.

The invariant: **the rendered frame must never exceed the terminal.** A
`Live(screen=False)` region scrolls the overflow and tears the display on
every refresh, and the tear happens on someone's real terminal at some real
size, not on the one the author happened to develop at. So the frame is
measured the way the Live region will measure it -- rendered through a
`rich.Console` of exactly that width and height, counting the lines it emits
-- across every height from 15 to 60, a sampled set of widths, `--verbose` on
and off, `--ascii` on and off, both header heights, and 1, 3 and 12 sites in
the status strip.

The import of the module under test is deferred into each test body. A
module-level import would collapse 6,624 cases into one collection error, and
`pytest --collect-only` listing the sweep is half of what writing it first is
for.

The target signature, written down once here because T-013 has to match it::

    render_dashboard(
        store,          # RealtimeStore, opened on the rendering thread
        sites,          # Sequence[SiteConfig] -- the rotation, in file order
        statuses,       # Mapping[str, PollStatus] -- may omit an unpolled site
        ui,             # UiConfig
        ring,           # RingBufferHandler | None, the --verbose source
        *,
        focus,          # str, the site the frame is about
        width,          # int  \\ the terminal, passed in rather than read from
        height,         # int  /  shutil, so the sweep can ask for any size
        verbose,        # bool
        no_color,       # bool -- NO_COLOR, resolved by the caller
        started,        # float, a time.monotonic() origin for the uptime
    ) -> rich.console.Group

`width` and `height` are keyword-only arguments rather than a
`shutil.get_terminal_size()` call inside the function: the size is what the
whole budget is computed against, and a value the caller cannot state is a
value the test cannot vary.

The focused site's display name and timezone come from `site_meta`, read for
every site at once through `store.list_sites()` -- the same source `report`
reads, so the two cannot disagree about which day "today" is -- falling back
to the config's `label` and `timezone` for a site that has not been primed
yet. Every site and not only the focused one, because the status strip counts
each site over its own local day and needs each site's zone to find it.
"""

import inspect
import io
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from rich.console import Console

from ga4_realtime.config import SiteConfig, UiConfig
from ga4_realtime.logging_setup import RingBufferHandler
from ga4_realtime.store import DayTotals, RealtimeStore
from ga4_realtime.timeutil import (
    day_bounds_utc,
    floor_minute,
    iso_minute,
    utcnow,
)

# --------------------------------------------------------------------------
# The sites, and the pressure each of them puts on the frame
# --------------------------------------------------------------------------

# Twelve, because the status strip is the new pressure on the budget (D11) and
# twelve markers is a config nobody would call unreasonable. The names are
# valid slugs so the fixtures here look like something config.py would have
# produced.
SITE_NAMES = tuple(f"site{index:02d}" for index in range(1, 13))
FOCUS = SITE_NAMES[0]

# Only the focused site's rows are ever read, but three sites are seeded so a
# test can move the focus and still find data behind it.
SEEDED_SITES = SITE_NAMES[:3]

# Four hours of minutes: enough that the six-hour chart window has a curve in
# it rather than a dot, and enough that the events table has more rows than
# the shortest terminal in the sweep can show.
SEED_MINUTES = 240
DEVICES = ("desktop", "mobile")

# Real zones, and deliberately not all the same one: "today" is the focused
# site's local day, so a sweep that focused a UTC site would never notice a
# day boundary being computed in the wrong place. Buenos Aires is here for its
# name, which is long enough to crowd the identity line at width 40.
ZONE_NAMES = (
    "America/Sao_Paulo",
    "America/Argentina/Buenos_Aires",
    "Asia/Tokyo",
    "Europe/Lisbon",
    "UTC",
)

# A display name that does not fit, on the site the sweep focuses. The header
# is no_wrap for exactly this reason: a wrapped identity line pushes the whole
# frame past the bottom of the terminal, which is the tear the budget exists
# to prevent.
LONG_DISPLAY_NAME = "site01.example.com -- Marketing Site, Production Property"

# Likewise for a custom event name in the table, and for a log line in the
# --verbose panel. Both are the kind of value a stranger's property produces
# and the author's never did.
LONG_EVENT = "checkout_funnel_step_three_shipping_details_submitted_ok"
LONG_LOG_LINE = "poll failed: " + "diagnostic detail, " * 20

# The error that turns the 4-line header into the 5-line one. Long, because
# the header truncates it to a fixed budget and a short one would never prove
# that it does.
LONG_ERROR = (
    "403 PermissionDenied: the caller does not have permission to access "
    "property 123456789; add the service account as a Viewer in GA4 Admin "
    "-> Property access management, then run doctor again"
)

CONVERSIONS = ["generate_lead", "purchase"]

# What the strip counts beside each marker, and the shipped default. A test
# that wants a site counting something else says so; a test that wants a site
# counted for nothing passes None.
COUNTER_EVENT = "page_view"

# One colour per site, cycled. `color` is per-site precisely because the
# dashboard shows one site at a time and the chart's colour is what says which
# one without reading the header -- so a fixture where all twelve were cyan
# could not tell a colour that follows the focus from one that is hardcoded.
COLORS = ("cyan", "magenta", "yellow", "green")


def zone_for(site: str) -> str:
    return ZONE_NAMES[SITE_NAMES.index(site) % len(ZONE_NAMES)]


def display_name_for(site: str) -> str:
    return LONG_DISPLAY_NAME if site == FOCUS else f"{site}.example"


def property_id_for(site: str) -> str:
    return f"{100000000 + SITE_NAMES.index(site)}"


def count_for(site: str) -> int:
    """A distinct four-figure count per site, wide enough to need a comma.

    Distinct because a strip that read the wrong site's number would show the
    right one by coincidence if every site shared a count; four figures
    because the thousands separator is part of the format under test.
    """
    return 1_000 + SITE_NAMES.index(site) * 111


def color_for(site: str) -> str:
    return COLORS[SITE_NAMES.index(site) % len(COLORS)]


# --------------------------------------------------------------------------
# Stand-ins for the objects the live command will pass in
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeRow:
    """One fan-out row, accepted structurally by `store.MinuteRowLike`."""

    minute_ts_utc: str
    event_name: str
    device: str
    country: str
    screen_page_views: int
    event_count: int

    def key(self) -> tuple[str, str, str, str]:
        return (self.minute_ts_utc, self.event_name, self.device, self.country)


@dataclass(frozen=True)
class FakeTokens:
    consumed: int
    remaining: int


@dataclass(frozen=True)
class FakeQuota:
    """What the Realtime API returns about token spend, as the footer reads it.

    Read with getattr in the footer, so this only has to carry the two
    attributes it names.
    """

    tokens_per_hour: FakeTokens
    tokens_per_day: FakeTokens


@dataclass
class FakeStatus:
    """A `PollStatus` without the thread it belongs to.

    `poller.py` lands in a different task and this file must not depend on it:
    what the dashboard reads is the snapshot, so the snapshot is what is
    faked. The lock is absent for the same reason -- nothing here is being
    written while it is read.
    """

    last_success: datetime | None = None
    last_error: str | None = None
    in_flight: bool = False
    poll_count: int = 0
    error_count: int = 0
    quota: object | None = None

    def snapshot(self) -> dict:
        return {
            "last_success": self.last_success,
            "last_error": self.last_error,
            "in_flight": self.in_flight,
            "poll_count": self.poll_count,
            "error_count": self.error_count,
            "quota": self.quota,
        }


def make_site(
    name: str, *, counter_event: str | None = COUNTER_EVENT
) -> SiteConfig:
    """One site as `config.py` would have merged it.

    `credentials` points at nothing that exists: the UI never opens it, and a
    real key file in a rendering test would be a fixture nobody could explain.

    `counter_event` is a parameter because None is a state worth rendering --
    a site the strip counts nothing for -- and it is reached through the
    config rather than through any absence of data.
    """
    return SiteConfig(
        name=name,
        property_id=property_id_for(name),
        credentials=Path("secrets") / f"{name}.json",
        conversions=list(CONVERSIONS),
        interval=300,
        poll_window=10,
        timezone=zone_for(name),
        label=None,
        color=color_for(name),
        counter_event=counter_event,
        enabled=True,
    )


def make_sites(
    count: int, *, counter_event: str | None = COUNTER_EVENT
) -> list[SiteConfig]:
    """The first `count` sites, focused site first, in config order."""
    return [
        make_site(name, counter_event=counter_event)
        for name in SITE_NAMES[:count]
    ]


def make_statuses(
    sites: list[SiteConfig], *, last_error: str | None
) -> dict[str, FakeStatus]:
    """One status per site, covering all three strip markers at once.

    The focused site is always the healthy one; the others cycle through
    failed, stale and never-polled, so a 3- or 12-site frame exercises every
    marker the strip can draw rather than twelve copies of the same one.
    """
    now = utcnow()
    quota = FakeQuota(
        tokens_per_hour=FakeTokens(consumed=1200, remaining=248800),
        tokens_per_day=FakeTokens(consumed=9000, remaining=191000),
    )
    statuses: dict[str, FakeStatus] = {}
    for index, site in enumerate(sites):
        if site.name == FOCUS:
            statuses[site.name] = FakeStatus(
                last_success=now - timedelta(seconds=12),
                last_error=last_error,
                in_flight=True,
                poll_count=137,
                error_count=1 if last_error else 0,
                quota=quota,
            )
        elif index % 3 == 1:
            statuses[site.name] = FakeStatus(
                last_success=now - timedelta(minutes=4),
                last_error="503 ServiceUnavailable: the service is currently"
                " unavailable",
                poll_count=41,
                error_count=3,
            )
        elif index % 3 == 2:
            statuses[site.name] = FakeStatus(
                last_success=now - timedelta(hours=2),
                poll_count=9,
            )
        else:
            statuses[site.name] = FakeStatus()
    return statuses


def make_ui(*, ascii_mode: bool) -> UiConfig:
    return UiConfig(
        refresh=10,
        window=6,
        top_events=10,
        ascii=ascii_mode,
        color="cyan",
        conversion_color="green",
    )


def make_ring() -> RingBufferHandler:
    """A ring buffer holding what a few minutes of polling leaves behind.

    The last record belongs to a site that is *not* focused, on purpose: the
    --verbose panel shows every site's lines interleaved, because a panel that
    hid the failing site would defeat the status strip it sits below.
    """
    ring = RingBufferHandler()
    for index in range(30):
        ring.emit(
            logging.makeLogRecord(
                {
                    "msg": LONG_LOG_LINE if index == 5 else f"poll {index} ok",
                    "levelname": "INFO",
                    "site": SITE_NAMES[index % 3],
                }
            )
        )
    ring.emit(
        logging.makeLogRecord(
            {"msg": "poll ok", "levelname": "INFO", "site": SITE_NAMES[1]}
        )
    )
    return ring


# Built once. The panel only ever reads the buffer, and re-emitting thirty
# records for each of several thousand cases is time the sweep cannot spare.
RING = make_ring()


# --------------------------------------------------------------------------
# A database with something in it
# --------------------------------------------------------------------------


def seed_rows(site: str, anchor: datetime) -> list[FakeRow]:
    """One site's rows, counting backwards from `anchor`.

    A formula rather than a fixture file: nothing here asserts a particular
    number, only that a realistic amount of data renders inside its budget.
    """
    rows: list[FakeRow] = []
    for index in range(SEED_MINUTES):
        key = iso_minute(anchor - timedelta(minutes=index))
        for offset, device in enumerate(DEVICES):
            views = (index % 7) + 1 + offset
            rows.append(
                FakeRow(
                    minute_ts_utc=key,
                    event_name="page_view",
                    device=device,
                    country="Brazil",
                    screen_page_views=views,
                    event_count=views,
                )
            )
            # A long custom event name, so the events table has something to
            # crop at width 40 rather than only the six-character names the
            # author's own property happens to produce.
            if index % 5 == 0:
                rows.append(
                    FakeRow(
                        minute_ts_utc=key,
                        event_name=LONG_EVENT,
                        device=device,
                        country="Brazil",
                        screen_page_views=0,
                        event_count=2,
                    )
                )
            if index % 10:
                continue
            for event in CONVERSIONS:
                rows.append(
                    FakeRow(
                        minute_ts_utc=key,
                        event_name=event,
                        device=device,
                        country="Brazil",
                        screen_page_views=0,
                        event_count=1,
                    )
                )
    return rows


@dataclass(frozen=True)
class SweepStore:
    store: RealtimeStore
    anchor: datetime


@pytest.fixture(scope="module")
def sweep_store(tmp_path_factory):
    """Twelve sites' metadata, three sites' rows, seeded once for the sweep.

    Module-scoped deliberately. Every case in the sweep reads the same rows
    and writes none, and re-seeding four hours of minutes several thousand
    times would make the file's runtime about SQLite rather than about layout.

    The anchor is the current minute, not a fixed instant: "today" is what the
    dashboard asks the store for, and rows pinned to a past date would leave
    every case in the sweep rendering an empty frame -- which fits any
    terminal, and proves nothing.
    """
    path = tmp_path_factory.mktemp("dashboard") / "data" / "ga4_realtime.db"
    opened = RealtimeStore(path)
    anchor = floor_minute(utcnow())
    for site in SEEDED_SITES:
        opened.upsert_minutes(site, seed_rows(site, anchor))
    for site in SITE_NAMES:
        opened.save_site(
            site,
            property_id=property_id_for(site),
            display_name=display_name_for(site),
            tz_name=zone_for(site),
        )
    try:
        yield SweepStore(store=opened, anchor=anchor)
    finally:
        opened.close()


# --------------------------------------------------------------------------
# Measuring the frame the way the Live region will
# --------------------------------------------------------------------------


def measure(frame, width: int, height: int) -> list[str]:
    """Render `frame` through a console of exactly that size; return its lines.

    `color_system=None` and `legacy_windows=False` so the answer is the same
    on Linux and on Windows: what is being counted is lines, and an escape
    sequence or a legacy box-drawing fallback that differs per platform would
    make the count a property of the machine.
    """
    console = Console(
        width=width,
        height=height,
        file=io.StringIO(),
        force_terminal=False,
        color_system=None,
        legacy_windows=False,
    )
    with console.capture() as capture:
        console.print(frame)
    return capture.get().splitlines()


def dashboard_module():
    """Import the module under test, deferred so collection still works."""
    from ga4_realtime.ui import dashboard

    return dashboard


def panels_module():
    """Same deferral, for the panels the frame is assembled from."""
    from ga4_realtime.ui import panels

    return panels


def render(
    dashboard,
    sweep_store: SweepStore,
    *,
    width: int,
    height: int,
    verbose: bool = False,
    ascii_mode: bool = False,
    last_error: str | None = None,
    site_count: int = 3,
    focus: str = FOCUS,
    statuses: dict | None = None,
    ring: RingBufferHandler | None = RING,
    store=None,
):
    sites = make_sites(site_count)
    if statuses is None:
        statuses = make_statuses(sites, last_error=last_error)
    return dashboard.render_dashboard(
        sweep_store.store if store is None else store,
        sites,
        statuses,
        make_ui(ascii_mode=ascii_mode),
        ring,
        focus=focus,
        width=width,
        height=height,
        verbose=verbose,
        no_color=False,
        # An hour of uptime, so the footer's duration is a realistic width
        # rather than "0s".
        started=time.monotonic() - 3661,
    )


# --------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------

# Every integer height from 15 to 60, because the budget's failures are
# off-by-one and hide at particular heights -- the row where the table stops
# fitting, the row where the chart is dropped. Widths are sampled instead:
# nothing in the budget is computed from the width except the chart's inner
# width and the table's bar column, so the interesting widths are the extremes
# and a few in between.
HEIGHTS = tuple(range(15, 61))
WIDTHS = (40, 60, 80, 100, 140, 200)


@pytest.mark.parametrize("site_count", [1, 3, 12], ids=lambda n: f"sites{n}")
@pytest.mark.parametrize(
    "last_error",
    [None, LONG_ERROR],
    # The ids name the header height the error line produces once the status
    # strip lands (identity line, strip, error line, two borders). Before the
    # strip they are one line shorter; the budget has to hold either way,
    # which is why both are swept from the start.
    ids=["header4", "header5"],
)
@pytest.mark.parametrize("ascii_mode", [False, True], ids=["unicode", "ascii"])
@pytest.mark.parametrize("verbose", [False, True], ids=["quiet", "verbose"])
@pytest.mark.parametrize("height", HEIGHTS, ids=lambda h: f"h{h}")
@pytest.mark.parametrize("width", WIDTHS, ids=lambda w: f"w{w}")
def test_frame_never_exceeds_the_terminal(
    sweep_store, width, height, verbose, ascii_mode, last_error, site_count
):
    """The whole point of the file.

    A frame one line too tall does not look like a bug: inside a
    `Live(screen=False)` region it scrolls, and the display tears on every
    refresh from then on. The order of sacrifice as the terminal shrinks --
    log panel, then table rows, then the chart entirely -- is what keeps this
    true, and it is arithmetic, which is to say it is where the mistakes live.
    """
    dashboard = dashboard_module()

    frame = render(
        dashboard,
        sweep_store,
        width=width,
        height=height,
        verbose=verbose,
        ascii_mode=ascii_mode,
        last_error=last_error,
        site_count=site_count,
    )
    lines = measure(frame, width, height)

    assert lines, "the frame rendered nothing at all"
    assert len(lines) <= height, (
        f"frame is {len(lines)} lines in a {width}x{height} terminal"
    )


# --------------------------------------------------------------------------
# What the frame has to carry, at a size where everything fits
# --------------------------------------------------------------------------


@pytest.mark.parametrize("focus", [SITE_NAMES[0], SITE_NAMES[1]])
def test_frame_is_about_the_focused_site(sweep_store, focus):
    """The header identifies the site the numbers belong to.

    With one site on screen at a time, a frame that does not say which site it
    is showing is a frame that cannot be read after pressing Tab.
    """
    dashboard = dashboard_module()

    frame = render(dashboard, sweep_store, width=200, height=45, focus=focus)
    text = "\n".join(measure(frame, 200, 45))

    assert display_name_for(focus)[:14] in text


def test_render_writes_nothing_to_stdout(sweep_store, capsys):
    """stdout belongs to the Live region.

    A stray print from inside a render does not produce a wrong frame -- it
    produces a display that tears from that moment until the process exits.
    """
    dashboard = dashboard_module()

    frame = render(dashboard, sweep_store, width=100, height=30)
    measure(frame, 100, 30)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_render_survives_a_site_that_has_never_been_polled(sweep_store):
    """A site with no status yet is the state every site starts in.

    `statuses` is missing an entry rather than holding an empty one, because
    that is what a `PollerSet` looks like between construction and the first
    poll returning -- and a KeyError there would take the first frame down.
    """
    dashboard = dashboard_module()

    sites = make_sites(3)
    statuses = make_statuses(sites, last_error=None)
    del statuses[SITE_NAMES[2]]

    frame = render(
        dashboard, sweep_store, width=120, height=40, statuses=statuses
    )

    assert measure(frame, 120, 40)


def test_verbose_panel_shows_lines_from_sites_other_than_the_focused_one(
    sweep_store,
):
    """§7.6: the log panel interleaves every site, with its site prefix.

    A panel showing only the focused site's lines would hide exactly the site
    worth looking at -- the one whose marker in the strip has gone red.
    """
    dashboard = dashboard_module()

    frame = render(dashboard, sweep_store, width=200, height=50, verbose=True)
    text = "\n".join(measure(frame, 200, 50))

    assert f"[{SITE_NAMES[1]}]" in text


def test_frame_renders_without_a_ring_buffer(sweep_store):
    """`--verbose` off means there is nothing to show and nothing to read."""
    dashboard = dashboard_module()

    frame = render(dashboard, sweep_store, width=100, height=30, ring=None)

    assert measure(frame, 100, 30)


def test_render_keeps_the_terminal_size_keyword_only():
    """Same reason `now` and `window_minutes` are keyword-only on the chart.

    The size is what the entire budget is computed against. A positional pair
    of ints is two arguments that can be swapped silently, and a frame
    rendered 30 columns by 100 rows fits nothing.
    """
    dashboard = dashboard_module()

    parameters = inspect.signature(dashboard.render_dashboard).parameters

    for name in ("focus", "width", "height", "started"):
        parameter = parameters[name]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty


# --------------------------------------------------------------------------
# The status strip -- what the frame says about the sites it is not showing
# --------------------------------------------------------------------------

# Where the header's lines land in a rendered frame: the panel's top border,
# then the identity line, then the strip, then the error line when there is
# one. Indices rather than a search, because the order is itself the thing
# being asserted -- a strip drawn below the error line would still contain
# every marker and still be wrong.
IDENTITY_LINE = 1
STRIP_LINE = 2
ERROR_LINE = 3


def build_strip(
    *,
    site_count: int = 3,
    width: int = 200,
    ascii_mode: bool = False,
    no_color: bool = False,
    focus: str = FOCUS,
    last_error: str | None = None,
    counters: dict | None = None,
    counter_event: str | None = COUNTER_EVENT,
):
    """One strip, from the same sites and statuses the sweep renders.

    `counters` defaults to a distinct count per site rather than a shared one,
    so a strip that read the wrong site's number would show the wrong value
    rather than the right one by coincidence.
    """
    panels = panels_module()
    sites = make_sites(site_count, counter_event=counter_event)
    statuses = make_statuses(sites, last_error=last_error)
    snapshots = {site.name: statuses[site.name].snapshot() for site in sites}
    if counters is None:
        counters = {
            site.name: count_for(site.name)
            for site in sites
            if site.counter_event is not None
        }
    return panels.build_status_strip(
        sites,
        snapshots,
        counters=counters,
        focus=focus,
        width=width,
        no_color=no_color,
        ascii_mode=ascii_mode,
    )


def count_markers(plain: str, glyph: int) -> int:
    panels = panels_module()
    return sum(
        plain.count(marker[glyph])
        for marker in (
            panels.MARKER_OK,
            panels.MARKER_FAILED,
            panels.MARKER_IDLE,
        )
    )


@pytest.mark.parametrize("ascii_mode", [False, True], ids=["unicode", "ascii"])
def test_status_strip_draws_all_three_markers(ascii_mode):
    """§7.6's table, both halves of it.

    `make_statuses` puts one healthy, one failed and one stale site in the
    first three, so a strip that only ever drew one glyph would fail here
    rather than look plausible.
    """
    panels = panels_module()

    plain = build_strip(ascii_mode=ascii_mode).plain
    glyph = 1 if ascii_mode else 0

    assert panels.MARKER_OK[glyph] in plain
    assert panels.MARKER_FAILED[glyph] in plain
    assert panels.MARKER_IDLE[glyph] in plain
    if ascii_mode:
        # The whole point of --ascii: nothing on the line needs a code page
        # the console may not have. This raises rather than fails if one of
        # them slipped through, which is the same way it would go wrong at
        # runtime -- except that there it happens inside a Live refresh and
        # takes the display with it.
        assert plain.encode("ascii")


def test_status_strip_marks_the_focused_site_without_relying_on_colour():
    """The bracket is the half of the emphasis that survives NO_COLOR.

    Under NO_COLOR every style in `panels` collapses to nothing, so a strip
    that distinguished the focused site by weight alone would distinguish
    nothing at all for the readers who set it.
    """
    plain = build_strip(no_color=True).plain

    assert f"[{FOCUS} " in plain
    assert f"[{SITE_NAMES[1]} " not in plain

    moved = build_strip(no_color=True, focus=SITE_NAMES[1]).plain

    assert f"[{SITE_NAMES[1]} " in moved
    assert f"[{FOCUS} " not in moved


def test_status_strip_names_every_site_when_the_names_fit():
    plain = build_strip(site_count=12, width=200).plain

    for name in SITE_NAMES:
        assert name in plain
    assert count_markers(plain, 0) == 12


def test_status_strip_counts_each_site_beside_its_own_marker():
    """The number the strip exists to add, and whose it is.

    Distinct counts per site, so a strip reading the wrong site's number would
    print a number that is wrong rather than one that is right by accident.
    """
    plain = build_strip(site_count=3, width=200).plain

    for name in SITE_NAMES[:3]:
        assert f"{name} " in plain
        assert f"{count_for(name):,}" in plain


def test_a_strip_count_is_formatted_like_every_other_total_on_screen():
    """Thousands separators, because the footer and the table use them.

    The same figure appearing twice in one frame in two formats reads as two
    different figures.
    """
    plain = build_strip(site_count=1, counters={FOCUS: 1234567}).plain

    assert "1,234,567" in plain


def test_a_site_with_no_counter_event_gets_no_number():
    """None is "not counting", which is not the same as counting zero.

    A site opted out of the counter shows its name and its marker and nothing
    else -- rather than a 0 that reads as "this site collected nothing today".
    """
    plain = build_strip(site_count=3, width=200, counter_event=None).plain

    for name in SITE_NAMES[:3]:
        assert f"{name} " in plain
        assert f"{count_for(name):,}" not in plain
    assert count_markers(plain, 0) == 3
    # Character for character what the strip drew before counts existed.
    # Asserting on the absence of digits cannot say this: the site names are
    # site01, site02, site03.
    assert plain == build_strip(site_count=3, width=200, counters={}).plain


def test_a_zero_count_is_shown_rather_than_hidden():
    """The other half of the distinction: counted, and nothing arrived.

    That is the state the counter is most worth having in -- a site polling
    successfully every five minutes and bringing back nothing is green in the
    marker, and only the number says otherwise.
    """
    plain = build_strip(site_count=1, counters={FOCUS: 0}).plain

    assert f"[{FOCUS} " in plain
    assert " 0]" in plain


def test_the_strip_drops_the_counts_before_it_drops_the_names():
    """The middle rung of the ladder, which is why there are three.

    Without it the counts would push a narrow terminal straight past the named
    form to bare markers, so adding the numbers would take away names that
    terminal shows today. New information must not cost existing information.
    """
    panels = panels_module()

    wide = build_strip(site_count=3, width=200)
    middle = build_strip(site_count=3, width=44)
    narrow = build_strip(site_count=3, width=30)

    # Wide: names and counts.
    assert FOCUS in wide.plain
    assert f"{count_for(FOCUS):,}" in wide.plain

    # Middle: names survive, counts do not -- and every marker is still there.
    assert FOCUS in middle.plain
    assert f"{count_for(FOCUS):,}" not in middle.plain
    assert count_markers(middle.plain, 0) == 3
    assert middle.cell_len <= 44 - panels.PANEL_CHROME

    # Narrow: markers alone, which is where it ended before the counts.
    assert FOCUS not in narrow.plain
    assert count_markers(narrow.plain, 0) == 3
    assert narrow.cell_len <= 30 - panels.PANEL_CHROME


def test_status_strip_drops_the_names_before_it_drops_a_site():
    """Twelve sites in 40 columns: markers survive, names do not.

    Cropping the named form after the third site would lose the health of the
    remaining nine while still looking like a complete answer -- which is
    exactly the silent failure the strip exists to prevent.
    """
    panels = panels_module()

    strip = build_strip(site_count=12, width=40)

    assert SITE_NAMES[0] not in strip.plain
    assert count_markers(strip.plain, 0) == 12
    assert strip.cell_len <= 40 - panels.PANEL_CHROME


def test_a_site_that_failed_after_succeeding_is_not_drawn_healthy():
    """The order of the two tests in `status_marker`, as behaviour.

    A poll that failed ten seconds after one that succeeded leaves
    `last_success` recent and `last_error` set. Reading the age first would
    draw that site green.
    """
    panels = panels_module()

    snapshot = FakeStatus(
        last_success=utcnow() - timedelta(seconds=10),
        last_error="503 ServiceUnavailable",
    ).snapshot()

    assert panels.status_marker(snapshot, 300, False) == (
        panels.MARKER_FAILED[0],
        panels.STYLE_FAILED,
    )


def test_a_site_is_stale_once_two_intervals_have_passed():
    """The same threshold the header's reddening age uses, so they agree."""
    panels = panels_module()

    fresh = FakeStatus(last_success=utcnow() - timedelta(seconds=599))
    stale = FakeStatus(last_success=utcnow() - timedelta(seconds=601))

    fresh_marker, _ = panels.status_marker(fresh.snapshot(), 300, False)
    stale_marker, _ = panels.status_marker(stale.snapshot(), 300, False)

    assert fresh_marker == panels.MARKER_OK[0]
    assert stale_marker == panels.MARKER_IDLE[0]


def test_a_site_with_no_status_at_all_reads_as_never_polled():
    """What a PollerSet looks like before its first poll lands."""
    panels = panels_module()

    assert panels.status_marker(None, 300, False)[0] == panels.MARKER_IDLE[0]


@pytest.mark.parametrize(
    "last_error,expected",
    [(None, 4), (LONG_ERROR, 5)],
    ids=["header4", "header5"],
)
def test_header_is_exactly_the_height_the_budget_was_promised(
    last_error, expected
):
    """4 lines, or 5 with an error: two borders, identity, strip, error.

    `header_height` is what the budget subtracts before the panel exists, so a
    header one line taller than it claims does not misdraw the header -- it
    pushes the footer past the bottom of the terminal.
    """
    panels = panels_module()

    sites = make_sites(3)
    statuses = make_statuses(sites, last_error=last_error)
    snapshots = {site.name: statuses[site.name].snapshot() for site in sites}
    header = panels.build_header(
        panels.site_identity(sites[0], None),
        snapshots[FOCUS],
        sites[0].interval,
        False,
        False,
        strip=build_strip(last_error=last_error),
    )

    assert panels.header_height(snapshots[FOCUS]) == expected
    assert len(measure(header, 100, 40)) == expected


def test_strip_sits_between_the_identity_line_and_the_error_line(sweep_store):
    """The header's three lines, in the order §7.6 puts them in."""
    dashboard = dashboard_module()

    frame = render(
        dashboard, sweep_store, width=140, height=40, last_error=LONG_ERROR
    )
    lines = measure(frame, 140, 40)

    assert display_name_for(FOCUS)[:14] in lines[IDENTITY_LINE]
    assert f"[{FOCUS} " in lines[STRIP_LINE]
    assert "last error" in lines[ERROR_LINE]


def test_frame_carries_a_marker_for_every_site(sweep_store):
    """Twelve sites, twelve markers, on one line of a real frame.

    The strip is the only place the other eleven appear, so a frame that
    dropped one of them would be a site failing with nothing on screen saying
    so.
    """
    dashboard = dashboard_module()

    frame = render(dashboard, sweep_store, width=200, height=45, site_count=12)
    lines = measure(frame, 200, 45)

    assert count_markers(lines[STRIP_LINE], 0) == 12


# --------------------------------------------------------------------------
# Per-site focus: which day the numbers are from, and whose chart it is
# --------------------------------------------------------------------------


class RecordingStore:
    """A real store that also remembers which day each read asked for.

    Everything the frame does not read through one of the four day-scoped
    queries -- `list_sites`, `path`, `size_bytes` -- falls through to the real
    store, so what is under test is the arguments rather than a mock's idea
    of a database.

    `event_total` is recorded apart from the other four, in `counter_bounds`
    keyed by site, because it is the one read that is *not* scoped to the
    focused site: the status strip counts every site over its own day. Folding
    it in with the rest would make "every day-scoped read agrees" false for a
    correct frame.
    """

    def __init__(self, store):
        self._store = store
        self.bounds: dict[str, tuple[str, str]] = {}
        self.counter_bounds: dict[str, tuple[str, str]] = {}

    def __getattr__(self, name):
        return getattr(self._store, name)

    def take(self) -> dict[str, tuple[str, str]]:
        taken, self.bounds = self.bounds, {}
        return taken

    def take_counters(self) -> dict[str, tuple[str, str]]:
        taken, self.counter_bounds = self.counter_bounds, {}
        return taken

    def event_total(self, site, event, start, end):
        self.counter_bounds[site] = (start, end)
        return self._store.event_total(site, event, start, end)

    def day_totals(self, site, conversions, start, end):
        self.bounds["day_totals"] = (start, end)
        return self._store.day_totals(site, conversions, start, end)

    def top_events(self, site, start, end, limit=10):
        self.bounds["top_events"] = (start, end)
        return self._store.top_events(site, start, end, limit=limit)

    def pageview_series(self, site, start, end):
        self.bounds["pageview_series"] = (start, end)
        return self._store.pageview_series(site, start, end)

    def conversion_minutes(self, site, conversions, start, end):
        self.bounds["conversion_minutes"] = (start, end)
        return self._store.conversion_minutes(site, conversions, start, end)


def test_switching_focus_moves_the_day_every_figure_is_scoped_to(sweep_store):
    """§7.6: "today" is the *focused* site's local day, everywhere at once.

    site01 is in Sao Paulo and site03 in Tokyo, twelve hours apart, so Tab
    between them genuinely moves the day boundary. Every day-scoped read in
    one frame has to agree on that boundary: totals from one day and a chart
    from another would be a frame nobody could tell was wrong.
    """
    dashboard = dashboard_module()
    recording = RecordingStore(sweep_store.store)

    def frame_for(focus: str) -> dict[str, tuple[str, str]]:
        render(
            dashboard,
            sweep_store,
            width=140,
            height=40,
            store=recording,
            focus=focus,
        )
        return recording.take()

    sao_paulo = frame_for(SITE_NAMES[0])
    tokyo = frame_for(SITE_NAMES[2])

    assert set(sao_paulo) == {
        "day_totals",
        "top_events",
        "pageview_series",
        "conversion_minutes",
    }
    assert set(tokyo) == set(sao_paulo)
    assert len(set(sao_paulo.values())) == 1
    assert len(set(tokyo.values())) == 1
    assert sao_paulo["day_totals"] != tokyo["day_totals"]


def test_each_strip_count_is_measured_over_its_own_sites_day(sweep_store):
    """The one read that must *not* follow the focus.

    Everything else in the frame belongs to the focused site's local day. The
    strip does not: it counts every site, and a site in Tokyo is most of a day
    ahead of one in Sao Paulo. Measuring the twelve counts against whichever
    site happened to be focused would put each of them out by up to a day and
    change all of them on every Tab -- while still looking like twelve
    plausible numbers.

    Asserted against each site's own zone rather than by counting distinct
    answers: site01 and site02 are Sao Paulo and Buenos Aires, both UTC-3, so
    three sites legitimately produce two sets of bounds and a distinct-count
    assertion would fail on correct code.
    """
    dashboard = dashboard_module()
    recording = RecordingStore(sweep_store.store)

    render(
        dashboard,
        sweep_store,
        width=200,
        height=40,
        store=recording,
        focus=SITE_NAMES[0],
    )
    counters = recording.take_counters()

    assert set(counters) == set(SITE_NAMES[:3])
    for name in SITE_NAMES[:3]:
        zone = ZoneInfo(zone_for(name))
        today = utcnow().astimezone(zone).date()
        assert counters[name] == day_bounds_utc(today, zone)
    # And Tokyo is genuinely a different day from Sao Paulo, so the loop
    # above is comparing something rather than agreeing with itself.
    assert counters[SITE_NAMES[2]] != counters[FOCUS]

    # And the focus moving does not move them.
    render(
        dashboard,
        sweep_store,
        width=200,
        height=40,
        store=recording,
        focus=SITE_NAMES[2],
    )

    assert recording.take_counters() == counters


def test_chart_is_drawn_in_the_focused_sites_zone_and_colour(
    sweep_store, monkeypatch
):
    """The axis is that site's clock, and the curve is that site's colour.

    `color` is per-site because the dashboard shows one site at a time: the
    colour is what says which site you are looking at without reading the
    header, and it is worth nothing if it does not follow the focus.
    """
    dashboard = dashboard_module()
    seen: dict[str, object] = {}
    real = dashboard.build_chart

    def spy(points, tz, width, height, **kwargs):
        seen["tz"] = tz
        seen["color"] = kwargs["color"]
        return real(points, tz, width, height, **kwargs)

    monkeypatch.setattr(dashboard, "build_chart", spy)

    focus = SITE_NAMES[2]
    render(dashboard, sweep_store, width=140, height=40, focus=focus)

    assert str(seen["tz"]) == zone_for(focus)
    assert seen["color"] == color_for(focus)


# --------------------------------------------------------------------------
# The footer crops the breakdown, never the totals
# --------------------------------------------------------------------------


def build_footer_at(width: int):
    panels = panels_module()
    totals = DayTotals(
        page_views=12,
        events=34,
        conversions={"generate_lead": 5, "purchase": 6},
        conversions_total=11,
        minutes_recorded=7,
    )
    footer = panels.build_footer(
        totals,
        None,
        Path("out") / "ga4_realtime.db",
        1024,
        False,
        False,
        started=time.monotonic() - 3661,
        conversion_color="green",
    )
    return "\n".join(measure(footer, width, 10))


def test_footer_crops_the_breakdown_before_it_crops_the_totals():
    """§7.7: the per-event breakdown is what a narrow terminal loses first.

    The footer is a fixed-height panel of no_wrap lines, so everything past
    the right edge is cropped rather than wrapped -- which makes the order the
    line is written in the decision about what survives. The totals are
    written first for that reason.
    """
    wide = build_footer_at(200)
    narrow = build_footer_at(48)

    assert "conversions 11" in wide
    assert "generate_lead 5" in wide

    assert "conversions 11" in narrow
    assert "generate_lead" not in narrow
