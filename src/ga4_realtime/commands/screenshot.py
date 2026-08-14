"""`--screenshot`: one frame of invented data, for the documentation.

The dashboard is the whole product and there was no way to photograph it. Doing
so meant a service account, Viewer on a real property, a config file, and
enough accumulated traffic to be worth a picture -- after which the picture
held a real client's property ID, a real site name and real numbers. So the
README shipped with no image of the thing it describes.

This draws the dashboard from three invented sites and exits. No API call, no
credentials, no config file, no database file: it runs in a fresh clone on a
machine that has never run `init`, which is the whole claim. A flag rather than
a subcommand because it produces the *dashboard*, and the dashboard is the bare
invocation -- it has no subcommand word to hang a mode off, which is the same
reason every other rendering flag is global.

**The store is real.** `demo_store` opens an ordinary `RealtimeStore` on
`:memory:` and writes these rows through the ordinary `upsert_minutes`, so the
totals, the events table and the cumulative curve are computed by the same SQL
that computes them from real traffic. The rejected alternative -- an object
answering `day_totals`, `top_events`, `pageview_series` and
`conversion_minutes` from fabricated returns -- is a second implementation of
the tool's output, and a second implementation is the one that drifts until the
screenshot quietly stops resembling the program. `live.print_report` refuses to
reimplement `report` for exactly this reason.

**Nothing here is random.** Every number is an arithmetic function of the
minute it belongs to, so two runs an hour apart differ only by the extra hour
of curve: a screenshot retaken after an edit differs where the edit changed
something and nowhere else, and the suite can assert on the result.

**The day is anchored to now, never to a fixed date.** `render_dashboard`
recomputes the day bounds from `utcnow()` on every frame, so rows written under
a hardcoded date fall outside them and the frame renders empty -- the trap
`tests/conftest.py` documents on its own seeded store. The three sites sit in
three real timezones, and the one whose local clock is deepest into its
afternoon is the one the frame opens on, so a screenshot taken at 00:20 shows a
busy day somewhere rather than twenty minutes of ramp.

Everything invented lives here rather than in a top-level `demo.py`. It has one
consumer, and a module beside `store.py` and `poller.py` in the layout would
read as part of the collection layer and become importable API whose only job
is a picture. `live.py` carries `Focus`, `run_loop` and five more names beside
its own `run`; a command module is allowed to hold what only it uses.

Nothing here imports `ga4.py`. Rows reach the store structurally as
`MinuteRowLike` -- six fields and a `key()` -- the same seam that keeps
`store.py` from importing `ga4.MinuteRow` and closing a dependency cycle.
"""

import logging
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rich.console import Console, RenderableType

from .. import paths
from ..config import (
    DEFAULT_CONVERSION_COLOR,
    DEFAULT_INTERVAL,
    DEFAULT_POLL_WINDOW,
    DEFAULT_REFRESH,
    DEFAULT_TOP_EVENTS,
    DEFAULT_WINDOW_HOURS,
    SiteConfig,
    UiConfig,
)
from ..errors import ConfigError
from ..logging_setup import RingBufferHandler
from ..store import RealtimeStore
from ..timeutil import (
    day_bounds_utc,
    floor_minute,
    iso_minute,
    parse_minute,
    utcnow,
)
from ..ui.dashboard import render_dashboard
from ..ui.theme import no_color_requested
from . import ALL_SITES, Context, live

# Printed on stderr when the frame has been drawn. On stderr and not stdout for
# the reason `live.print_priming` gives: stdout is the artefact here, and
# `--screenshot > frame.txt` should capture the frame and nothing else.
NOTICE = (
    "^ mockup: three invented sites, one invented day. No API call, no "
    "config file, no database -- nothing above is real data."
)

# How long the demo has supposedly been running, for the footer's "up" figure.
# A fresh monotonic origin would print "up 0s", which is true and looks
# unfinished -- it is the one moment of a real run nobody would screenshot.
DEMO_UPTIME_SECONDS = 3 * 3600 + 47 * 60

# How long ago the focused site last polled. Inside `interval * 2`, so
# `panels.status_marker` calls it fresh and the strip marker comes out green.
DEMO_LAST_POLL_SECONDS = 12

# The local hour the default focus aims for: mid-afternoon, past the day's
# ramp and inside the peak, so the chart has a full shape in it.
DEMO_TARGET_HOUR = 16


# --------------------------------------------------------------------------
# The invented shapes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DemoRow:
    """One fan-out row, structurally a `store.MinuteRowLike`.

    No `site` field, for the reason `ga4.MinuteRow` has none: the site is what
    the caller is writing *as*, and it is passed once to `upsert_minutes`
    rather than repeated on every row.
    """

    minute_ts_utc: str
    event_name: str
    device: str
    country: str
    screen_page_views: int
    event_count: int

    def key(self) -> tuple[str, str, str, str]:
        return (self.minute_ts_utc, self.event_name, self.device, self.country)


@dataclass(frozen=True)
class DemoTokens:
    """One of the two quota buckets the footer reads off a property quota."""

    consumed: int
    remaining: int


@dataclass(frozen=True)
class DemoQuota:
    """What `panels.build_footer` duck-types out of a real property quota.

    It reaches for `tokens_per_hour` and `tokens_per_day` with `getattr` and
    wants `.consumed` and `.remaining` on each, so this satisfies that shape
    and nothing else. Without a quota the footer prints "quota n/a", which is
    honest during a real run and reads as broken in a picture.
    """

    tokens_per_hour: DemoTokens
    tokens_per_day: DemoTokens


@dataclass(frozen=True)
class DemoStatus:
    """A poller status, as `ui.dashboard.StatusLike` sees one.

    That protocol is a single `snapshot()`, so this is a frozen dataclass with
    **no lock**: the real `PollStatus` carries one because poller threads
    mutate it while a frame reads it, and here there are no threads. All six
    keys are present because every consumer indexes them directly, and a
    KeyError raised inside a render costs the whole frame.
    """

    last_success: Any = None
    last_error: str | None = None
    in_flight: bool = False
    poll_count: int = 0
    error_count: int = 0
    quota: object | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "last_success": self.last_success,
            "last_error": self.last_error,
            "in_flight": self.in_flight,
            "poll_count": self.poll_count,
            "error_count": self.error_count,
            "quota": self.quota,
        }


class MockupStore(RealtimeStore):
    """A real store, in memory, wearing the path a real run would use.

    `Path(":memory:").parent` is `Path(".")`, so the base constructor's
    `mkdir(parents=True, exist_ok=True)` is a no-op and this creates nothing
    anywhere, even in a read-only working directory. A fresh in-memory database
    reads `user_version` 0 and has no `realtime_minute` table, so the schema
    guard creates and stamps it exactly as it would on disk.

    Both overrides exist for the footer alone. The alternative -- passing a
    display path and a byte count into `render_dashboard` -- would widen an
    invariant-bearing signature for the sake of a mockup, which is the wrong
    way round.
    """

    def __init__(self) -> None:
        super().__init__(Path(":memory:"))
        # `paths` answers "where", never "make it so": nothing in that module
        # touches the filesystem, so naming the real platform data path here
        # creates neither the directory nor the file. `shorten_path` collapses
        # it under ~, so the picture carries no username either.
        self.path = paths.default_database_path()

    def size_bytes(self) -> int:
        # Measured, not invented. The base method stats a file that does not
        # exist and would answer 0, which next to nine thousand page views
        # reads as a bug; page count times page size is what this data would
        # weigh on disk, so the figure keeps matching the seed when the seed
        # changes.
        pages = self._conn.execute("PRAGMA page_count").fetchone()[0]
        size = self._conn.execute("PRAGMA page_size").fetchone()[0]
        return int(pages) * int(size)


# --------------------------------------------------------------------------
# The sites
# --------------------------------------------------------------------------

# Three, not one. The status strip is one marker per site and exists so that a
# site nobody has pressed Tab to cannot fail quietly -- a strip with a single
# entry demonstrates none of that. Three timezones because the header's zone
# and clock are what make Tab crossing a day boundary legible, and because
# between them these three put *some* site in the middle of its afternoon at
# every hour of UTC (see `default_focus`).
#
# `.example` is reserved by RFC 2606 and can never be registered, so no real
# site is depicted; the property IDs are sequential nonsense for the same
# reason.
_DEMO_SITES = (
    ("demo01", "123456789", "example.com", "America/Sao_Paulo", "cyan"),
    ("demo02", "987654321", "shop.example", "Europe/Lisbon", "magenta"),
    ("demo03", "456789123", "docs.example", "Asia/Tokyo", "yellow"),
)

# Per site: the events that count as conversions, and the country the traffic
# is reported from. One country each keeps the fan-out narrow -- the dimension
# is real, but nothing on screen breaks it down, so a second would add rows the
# frame cannot distinguish.
_DEMO_CONVERSIONS = {
    "demo01": ["generate_lead"],
    "demo02": ["purchase", "sign_up"],
    "demo03": ["sign_up"],
}
_DEMO_COUNTRIES = {
    "demo01": "Brazil",
    "demo02": "Portugal",
    "demo03": "Japan",
}


def demo_sites(*, timezone: str | None = None) -> list[SiteConfig]:
    """The three invented sites, as ordinary `SiteConfig` objects.

    `SiteConfig` validates nothing itself -- every check lives in the loader
    functions in `config.py` -- which is what makes one constructible here with
    no file to load. `credentials` therefore names a path that does not exist
    and never will: a frame reads a site's name, label, colour, timezone,
    property ID, interval and conversions, and nothing in `ui/` opens a key.

    `interval` is the shipped default rather than a token value because it is
    not decoration: `status_marker` calls a site stale past `interval * 2`, so
    it decides what colour the strip comes out.
    """
    return [
        SiteConfig(
            name=name,
            property_id=property_id,
            credentials=Path("secrets") / f"{name}.json",
            conversions=list(_DEMO_CONVERSIONS[name]),
            interval=DEFAULT_INTERVAL,
            poll_window=DEFAULT_POLL_WINDOW,
            timezone=timezone or zone,
            label=label,
            color=color,
            enabled=True,
        )
        for name, property_id, label, zone, color in _DEMO_SITES
    ]


def demo_ui(
    *, window: int | None = None, ascii_mode: bool = False
) -> UiConfig:
    """The shipped defaults, with the two flags that change a frame folded in.

    Built from `config.py`'s own `DEFAULT_*` constants rather than repeated
    literals, so the picture always shows what an unedited config produces: if
    the default window changes, the screenshot changes with it instead of
    quietly documenting the old value.
    """
    return UiConfig(
        refresh=DEFAULT_REFRESH,
        window=window or DEFAULT_WINDOW_HOURS,
        top_events=DEFAULT_TOP_EVENTS,
        ascii=ascii_mode,
        # Every demo site names its own colour, so this fallback is never
        # reached; it is here because UiConfig requires it.
        color="cyan",
        conversion_color=DEFAULT_CONVERSION_COLOR,
    )


def demo_statuses(sites: list[SiteConfig]) -> dict[str, DemoStatus]:
    """One status per site: two polling healthily, one not yet polled.

    Deliberately no failure marker. A red cross is a real state worth
    documenting, but a screenshot of this project showing one reads as "the
    tool is broken" to everyone who has not yet learned the strip is doing its
    job, and the README's marker table already explains all three in words.

    The third site is *absent* from the mapping rather than present with an
    empty status, because absent is what a real `PollerSet` looks like between
    construction and the first poll returning -- which is the gap
    `dashboard._NEVER_POLLED` is written for.
    """
    now = utcnow()
    quota = DemoQuota(
        tokens_per_hour=DemoTokens(consumed=1_240, remaining=3_760),
        tokens_per_day=DemoTokens(consumed=18_600, remaining=181_400),
    )
    statuses = {
        sites[0].name: DemoStatus(
            last_success=now - timedelta(seconds=DEMO_LAST_POLL_SECONDS),
            in_flight=True,
            poll_count=45,
            quota=quota,
        )
    }
    if len(sites) > 1:
        statuses[sites[1].name] = DemoStatus(
            last_success=now - timedelta(seconds=DEMO_LAST_POLL_SECONDS + 31),
            poll_count=45,
            quota=quota,
        )
    return statuses


# What --verbose shows. The log panel is the one part of the frame that is
# prose, so these are shaped like the lines a healthy run actually emits.
_DEMO_LOG = (
    ("demo01", logging.INFO, "polled 10 min: 118 rows, 6 new, 112 updated"),
    ("demo02", logging.INFO, "polled 10 min: 74 rows, 4 new, 70 updated"),
    ("demo03", logging.INFO, "property resolved: docs.example, Asia/Tokyo"),
    ("demo01", logging.DEBUG, "next poll in 300s"),
    ("demo02", logging.INFO, "polled 10 min: 71 rows, 5 new, 66 updated"),
    ("demo01", logging.INFO, "polled 10 min: 121 rows, 7 new, 114 updated"),
)


def demo_ring() -> RingBufferHandler:
    """A ring buffer already holding a few lines, for `--verbose`.

    Built and fed directly rather than through `setup_logging`, which would
    resolve a database path this mode does not have, create the directory
    beside it and open a rotating log file -- two things on disk from a command
    whose promise is that it writes nothing. The handler formats each record on
    the way in, so a synthetic one produces exactly the line a real one would.
    """
    ring = RingBufferHandler()
    for site, level, message in _DEMO_LOG:
        ring.emit(
            logging.makeLogRecord(
                {
                    "levelno": level,
                    "levelname": logging.getLevelName(level),
                    "msg": message,
                    "site": site,
                }
            )
        )
    return ring


# --------------------------------------------------------------------------
# The traffic
# --------------------------------------------------------------------------

# Page views per minute at each hour of the local day, 00:00 through 23:00. A
# real day in shape if not in size: a trough near 03:00, a ramp from 06:00, a
# broad midday plateau, the afternoon peak, an evening decline. The chart is
# the part of the frame being photographed, so a flat line -- or the sawtooth
# the test fixtures use, which only has to be non-empty -- would waste it.
#
# The scale is a small business site, a couple of thousand views a day, rather
# than the largest number that would fit: a figure a reader recognises says
# more about what the tool is for than an impressive one does.
#
# **Views per hour, not per minute.** An hourly figure is the one a reader can
# sanity-check against a site they know, and at this volume a per-minute anchor
# would be 0, 1 or 2 all day -- three values with no room for a shape between
# them. `_intensity` converts, in fixed point, so the curve stays smooth at
# volumes far below one view a minute.
#
# The hour-to-hour contrast is the point rather than a side effect. The chart
# draws the *cumulative* total, which turns a rate into a slope: a shape that
# only rose and fell gently would integrate into a line that looks straight
# however long you watched it. The lunch dip and the two peaks are what give
# the curve visible angles -- climb, plateau, climb -- inside any window the
# chart is likely to show.
#
# Grouped six hours to a row rather than laid out flat, because the formatter
# puts a flat tuple of twenty-four numbers on twenty-four lines and the shape
# is the whole point of the value -- it has to be readable as a curve.
_SHAPE = (
    (15, 8, 5, 4, 6, 12),  # 00-05  the overnight trough
    (28, 70, 145, 210, 60, 235),  # 06-11  ramp, a lull, the mid-morning rush
    (300, 90, 40, 205, 320, 130),  # 12-17  peak, lunch dip, afternoon peak
    (95, 150, 70, 45, 28, 18),  # 18-23  an evening bump, then the decline
)

# Fixed-point denominator for `_intensity`. Views per minute is a fraction at
# this volume, and carrying it as hundredths keeps the shape's proportions
# intact through the integer arithmetic below.
_SCALE = 100


def _hourly(hour: int) -> int:
    """Views in the hour beginning at `hour`, out of the grouped shape."""
    return _SHAPE[hour // 6][hour % 6]


# How often each event fires per hundred page views, in roughly the proportions
# a small content site reports. Four, and the count is deliberate: the events
# table is a supporting panel, and a screenshot of it should be readable at a
# glance rather than exhaustive. An events table whose rows all held the same
# number would read as placeholder text, which is what it would be.
_EVENT_MIX = (
    ("page_view", 100),
    ("user_engagement", 58),
    ("scroll", 34),
    ("session_start", 19),
)

# The two device categories, and how the traffic splits between them.
_DEVICE_MIX = (("mobile", 58), ("desktop", 42))


def _intensity(minute_of_day: int) -> int:
    """Page views this minute in hundredths, between the hourly anchors.

    The wobble is arithmetic rather than random, so the curve has the
    minute-to-minute texture real traffic has while staying identical between
    runs -- which is what lets a screenshot be retaken and compared. It is a
    percentage of the level rather than a fixed count, so it stays visible at
    the busy end without swamping the shape at the quiet one.
    """
    hour, within = divmod(minute_of_day, 60)
    start = _hourly(hour % 24)
    end = _hourly((hour + 1) % 24)
    per_hour = start + (end - start) * within // 60
    level = per_hour * _SCALE // 60
    return max(0, level + level * ((minute_of_day * 7) % 13 - 6) // 100)


# One whole event, in the units `level * share * split` is measured in:
# hundredths of a view, times a share per hundred, times a split per hundred.
_UNIT = 10_000 * _SCALE


def _rows_for_minute(
    site: SiteConfig,
    minute_key: str,
    minute_of_day: int,
    carry: dict[tuple[str, str], int],
) -> list[DemoRow]:
    """Every fan-out row for one minute: event x device, one country.

    `carry` holds the leftover fraction of each event-and-device series, and it
    is what makes the daily totals come out at the declared proportions. Two
    thousand views a day is under two a minute, so every share of it is a
    fraction and plain division would floor all four events to zero in every
    minute of the day. The obvious repair -- fire when the fraction beats some
    spread-out threshold -- looks right and is not: the threshold cycles with
    the clock, the fraction cycles with the traffic, and the two beat against
    each other into a bias that put `user_engagement` at 47% of page views
    where the mix declares 58%. Accumulating the remainder instead is exact by
    construction: every whole event that has built up is emitted, and what is
    left over waits for the next minute.

    An event whose accumulator has not yet reached one produces no row at all,
    which is also what the API does -- it reports what fired, not a zero for
    everything that did not. Overnight that empties most of the fan-out and the
    cumulative curve goes flat, which is correct rather than a gap.
    """
    country = _DEMO_COUNTRIES[site.name]
    level = _intensity(minute_of_day)
    rows: list[DemoRow] = []
    for event, share in _EVENT_MIX:
        for device, split in _DEVICE_MIX:
            series = (event, device)
            carry[series] = carry.get(series, 0) + level * share * split
            count, carry[series] = divmod(carry[series], _UNIT)
            if count <= 0:
                continue
            rows.append(
                DemoRow(
                    minute_ts_utc=minute_key,
                    event_name=event,
                    device=device,
                    country=country,
                    # Page views sit on the page_view rows only. Spreading them
                    # across every event would count one view several times,
                    # which is the mistake a fan-out makes easy.
                    screen_page_views=count if event == "page_view" else 0,
                    event_count=count,
                )
            )
    rows.extend(_conversion_rows(site, minute_key, minute_of_day, level))
    return rows


def _conversion_rows(
    site: SiteConfig, minute_key: str, minute_of_day: int, level: int
) -> list[DemoRow]:
    """A conversion now and then, and only where there is traffic to convert.

    Gated on `level` so no leads arrive at 04:00 from nobody, and spaced by a
    prime so the `*` markers along the chart's baseline do not settle into a
    visibly repeating pattern. The threshold is one view a minute, in the
    hundredths `_intensity` works in -- which puts conversions across the
    working day and leaves the overnight stretch of the baseline clear.
    """
    if level < _SCALE or minute_of_day % 23:
        return []
    country = _DEMO_COUNTRIES[site.name]
    return [
        DemoRow(
            minute_ts_utc=minute_key,
            event_name=event,
            device="mobile" if index % 2 else "desktop",
            country=country,
            screen_page_views=0,
            event_count=1,
        )
        for index, event in enumerate(site.conversions)
    ]


def seed_rows(site: SiteConfig) -> list[DemoRow]:
    """One local day of invented traffic, midnight to the current minute.

    The day is the *site's* own, resolved through the same `day_bounds_utc` the
    renderer uses, so a site in Tokyo and a site in Sao Paulo are seeded across
    two different stretches of UTC and each looks right under its own header.
    Anchoring to `utcnow()` rather than to a fixed date is what keeps the rows
    inside the bounds the frame computes; a hardcoded date renders an empty
    dashboard, which is the failure this function is shaped to avoid.

    `offset` is minutes past *local* midnight, which is what the traffic shape
    is indexed by -- so the curve peaks in the middle of the site's own
    afternoon rather than in the middle of UTC's.
    """
    tz = zone_of(site)
    now = floor_minute(utcnow())
    start_key, _ = day_bounds_utc(now.astimezone(tz).date(), tz)
    midnight = parse_minute(start_key)
    elapsed = max(0, int((now - midnight).total_seconds() // 60))
    rows: list[DemoRow] = []
    # One accumulator for the whole day, walked from midnight forward: the
    # fractions each minute leaves behind are what the next one adds to, so
    # the day has to be generated in order. It is, and only from here.
    carry: dict[tuple[str, str], int] = {}
    for offset in range(elapsed + 1):
        moment = midnight + timedelta(minutes=offset)
        rows.extend(_rows_for_minute(site, iso_minute(moment), offset, carry))
    return rows


def demo_store(sites: list[SiteConfig], focus: str) -> MockupStore:
    """An in-memory store holding one seeded day, and a meta row per site.

    Only the focused site is seeded. It is the only one a frame reads rows for
    -- everything day-scoped on screen belongs to the site the header names --
    and seeding all three would triple the work to fill tables nothing reads.
    Every site still gets its `site_meta` row, because that is what `load_site`
    answers from and what a real database holds after one poll of each.
    """
    store = MockupStore()
    try:
        for site in sites:
            store.save_site(
                site.name,
                property_id=site.property_id,
                display_name=site.label or site.name,
                tz_name=site.timezone or "UTC",
            )
            if site.name == focus:
                store.upsert_minutes(site.name, seed_rows(site))
    except Exception:
        # A half-built store must not be left open: on Windows a live
        # connection holds a handle, and the caller is about to let this out
        # rather than close anything itself.
        store.close()
        raise
    return store


# --------------------------------------------------------------------------
# Which site, and the frame
# --------------------------------------------------------------------------


def zone_of(site: SiteConfig) -> ZoneInfo:
    """A demo site's zone. Always set, so the fallback is belt and braces."""
    return ZoneInfo(site.timezone) if site.timezone else ZoneInfo("UTC")


def default_focus(sites: list[SiteConfig]) -> str:
    """The site whose local clock is nearest mid-afternoon.

    Not "the first one", which is what the real dashboard does and what this
    mode cannot afford. A frame is day-scoped, so a screenshot taken at 00:20
    local would show twenty minutes of overnight ramp and a nearly empty chart
    -- a broken-looking picture produced by entirely correct code, at an hour
    nobody chooses on purpose. The three zones above are far enough apart that
    one of them is always mid-afternoon, so the default frame is always a busy
    day. `--site` still overrides it, and the header names whichever site won.
    """
    now = utcnow()

    def distance(site: SiteConfig) -> float:
        local = now.astimezone(zone_of(site))
        return abs(local.hour + local.minute / 60 - DEMO_TARGET_HOUR)

    return min(sites, key=distance).name


def resolve_focus(sites: list[SiteConfig], wanted: str | None) -> str:
    """Which demo site the frame is about, refusing a name that is not one.

    `render_dashboard` falls back to the first site for an unknown focus rather
    than raising, which is right inside a render and wrong here: `--screenshot
    --site typo` would quietly draw a different site than the one asked for.
    The message lists every name that would have worked, which is the same
    shape -- and the same cheapest-fix-for-a-typo reasoning -- as `Config.site`.
    """
    if wanted is None or wanted == ALL_SITES:
        return default_focus(sites)
    names = [site.name for site in sites]
    if wanted not in names:
        raise ConfigError(
            f"--screenshot draws invented sites, and there is none named "
            f"{wanted!r}.\n"
            f"Demo sites: {', '.join(names)}.\n"
            "Drop --screenshot to open the dashboard on a site from your "
            "config file."
        )
    return wanted


def resolve_timezone(wanted: str | None) -> str | None:
    """Validate `--timezone` here, because no config file will.

    In the ordinary path `config._timezone` resolves the zone at load time and
    produces a message naming tzdata when it cannot. This mode never loads a
    config, and `panels.resolve_zone` falls back to UTC in silence rather than
    raising -- so without this check `--screenshot --timezone Not/AZone` would
    draw a UTC frame and claim it was the zone that was asked for.
    """
    if wanted is None:
        return None
    try:
        ZoneInfo(wanted)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(
            f"--timezone {wanted!r} is not a timezone this machine can "
            "resolve.\nUse an IANA name such as America/Sao_Paulo or "
            "Asia/Tokyo."
        ) from None
    return wanted


def frame(
    store: RealtimeStore,
    sites: list[SiteConfig],
    ui: UiConfig,
    *,
    focus: str,
    width: int,
    height: int,
    verbose: bool,
    no_color: bool,
) -> RenderableType:
    """One mockup frame, at a size the caller chose.

    `width` and `height` are arguments rather than a `get_terminal_size()` call
    in here, for the reason `render_dashboard` gives for the same choice: the
    test sweep has to be able to vary them, and a renderer that measures the
    terminal itself cannot be swept.
    """
    return render_dashboard(
        store,
        sites,
        demo_statuses(sites),
        ui,
        demo_ring() if verbose else None,
        focus=focus,
        width=width,
        height=height,
        verbose=verbose,
        no_color=no_color,
        # A monotonic origin in the past, so the footer reads a plausible
        # uptime instead of "up 0s".
        started=time.monotonic() - DEMO_UPTIME_SECONDS,
    )


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------


def run(ctx: Context) -> int:
    sites = demo_sites(timezone=resolve_timezone(ctx.args.timezone))
    focus = resolve_focus(sites, ctx.args.site)
    ui = live.resolve_ascii(
        demo_ui(window=ctx.args.window, ascii_mode=ctx.args.ascii)
    )
    no_color = no_color_requested()

    size = shutil.get_terminal_size(fallback=live.TERMINAL_FALLBACK)
    # One row short of the terminal, unlike the live dashboard, which owns the
    # screen and uses all of it. Here the shell prompt comes back underneath:
    # a frame filling the last row scrolls the terminal by one and takes the
    # header's top border off the top of the very screen being photographed.
    height = max(1, size.lines - 1)

    store = demo_store(sites, focus)
    try:
        rendered = frame(
            store,
            sites,
            ui,
            focus=focus,
            width=size.columns,
            height=height,
            verbose=ctx.args.verbose,
            no_color=no_color,
        )
        # The size is passed to the Console as well, and not left to be
        # detected. A Console writing to a pipe measures 80 columns while
        # `get_terminal_size` returns TERMINAL_FALLBACK's 100, so a detected
        # width would wrap every line of a frame built for a wider one --
        # tearing the display in the one command that exists to produce a
        # clean picture of it.
        Console(
            width=size.columns,
            height=height,
            no_color=no_color,
            soft_wrap=False,
        ).print(rendered)
    finally:
        # An in-memory database dies with its connection anyway; the close is
        # here because every other store in this package is closed by whoever
        # opened it, and an exception on the way out must not be the reason
        # this one is the exception.
        store.close()

    print(NOTICE, file=sys.stderr)
    return 0
