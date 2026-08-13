"""The regions of the frame: header, status strip, events table, footer.

Every panel here is built to a height its caller already knows. That is the
whole discipline of this file: `render_dashboard` allocates rows from a budget
before any of these functions run, so a panel that came back one line taller
than agreed would push the frame past the bottom of the terminal, and a
`Live(screen=False)` region scrolls the overflow and tears on every refresh.
Hence the explicit `height=` on each Panel, the `no_wrap` on every Text inside
one, and `header_height()` and `FOOTER_HEIGHT` living here for the budget to
read rather than being the same numbers written down in two files.
"""

import os
import time
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..config import SiteConfig
from ..store import DayTotals, SiteMeta
from ..timeutil import humanize_delta, utcnow
from .theme import frame_box

# Two borders and two lines of text. Fixed rather than derived because the
# budget subtracts it before the footer exists, and a footer that grew a line
# would take that line from the chart without either of them noticing.
FOOTER_HEIGHT = 4

# Title, top border, header row, header rule, bottom border. The rows a table
# can show are whatever is left after these five, which is what makes a table
# in a 15-line terminal a single row rather than an error.
TABLE_CHROME = 5

# Two border columns and the one column of padding inside each of them. What
# a panel has left for text, subtracted here rather than by the caller: the
# status strip is the only thing in the frame that changes shape when it runs
# out of room, and the number it measures itself against is this file's own
# `padding=(0, 1)` rather than anything the dashboard decided.
PANEL_CHROME = 4

# The error is cut in the string as well as by the Text's own no_wrap. The
# panel's height is fixed either way, so the only question is how much of a
# long message is worth the width it costs the rest of the line: 160
# characters is about what a PermissionDenied says before it starts repeating
# the resource name.
MAX_ERROR_CHARS = 160

# The three states a site can be in, one glyph each: unicode first, ascii
# second. The ascii half is not a style preference -- a legacy Windows code
# page cannot encode the first of each pair at all, and a UnicodeEncodeError
# raised from inside a Live refresh is a crash in the middle of the dashboard
# rather than one wrong character.
MARKER_OK = ("●", "*")
MARKER_FAILED = ("✗", "!")
MARKER_IDLE = ("○", ".")

# Green, red, and a yellow that says "nothing is arriving from here" without
# claiming a failure was reported: a site that has never been polled and a
# site that stopped four minutes ago are both amber rather than red, because
# neither has an error message to go and read.
STYLE_OK = "green"
STYLE_FAILED = "red"
STYLE_IDLE = "yellow"

# One site in the strip: name, marker glyph, marker style, and whether it is
# the site the frame is about.
StripEntry = tuple[str, str, str, bool]


@dataclass(frozen=True)
class SiteIdentity:
    """Who the frame is about, as the header says it.

    Assembled from `site_meta` -- the same row `report` reads, so the two
    cannot disagree about which day "today" is -- with the config's `label`
    and `timezone` over the top and a fallback for a site whose first poll has
    not landed yet.

    Deliberately not `ga4.PropertyInfo`. Nothing in `ui/` imports the API
    layer: what the header shows is what the database cached, which is
    available with no credentials, no network and no Admin API grant.
    """

    name: str
    display_name: str
    property_id: str
    tz: ZoneInfo
    tz_name: str


def resolve_zone(tz_name: str) -> tuple[ZoneInfo, str]:
    """A zone and the name to print for it, falling back to UTC in words.

    Both inputs to this are already validated -- the config resolves its own
    `timezone` at load time, and a cached one came back from the Admin API
    through this tool -- so the fallback is close to unreachable. It exists
    because the one place it *would* be reached is inside a Live refresh,
    where an exception costs the entire display rather than one wrong label.
    The name is returned alongside so the header says UTC when UTC is what
    the day boundaries were actually computed in.
    """
    try:
        return ZoneInfo(tz_name), tz_name
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC"), "UTC"


def site_identity(site: SiteConfig, meta: SiteMeta | None) -> SiteIdentity:
    """What to call one site, from the config first and the cache after.

    `label` wins because it is the only one of the three the user chose: a GA4
    property's display name is whatever its owner typed years ago, and the
    documented promise is that setting `label` renames the site on screen.
    Without it the Admin API's display name is used, and `property <id>` is
    the last resort -- the same string `fetch_property` stores when the Admin
    API refuses, so the two agree rather than inventing separate placeholders.

    `timezone` is the mirror image: the config's value wins when it is set
    (which includes --timezone, applied to every site for one debugging run),
    the cached one otherwise, UTC last.
    """
    display_name = (
        site.label
        or (meta.display_name if meta else None)
        or f"property {site.property_id}"
    )
    tz, tz_name = resolve_zone(
        site.timezone or (meta.tz_name if meta else None) or "UTC"
    )
    return SiteIdentity(
        name=site.name,
        display_name=display_name,
        property_id=site.property_id,
        tz=tz,
        tz_name=tz_name,
    )


def status_marker(
    snapshot: Mapping[str, Any] | None,
    interval: int,
    ascii_mode: bool,
) -> tuple[str, str]:
    """One site's health as a glyph and the style to draw it in.

    The error is tested before the age, and that order *is* the behaviour: a
    poll that failed ten seconds after one that succeeded leaves
    `last_success` recent **and** `last_error` set, so reading the age first
    would draw that site green -- which is precisely the silent failure this
    strip exists to prevent. The poller clears `last_error` on every success,
    so a set one always means the *last* attempt failed, not that one once
    did.

    Two missed intervals is where a slow poll becomes a broken one, the same
    threshold the header's reddening age uses, so the marker beside a site's
    name and the words inside its own header cannot disagree.

    Never-polled and stale share a marker deliberately. The difference
    between "has not started yet" and "stopped four minutes ago" is worth a
    line of prose and not a third glyph to learn; the focused site's header
    spells it out, and for every other site the actionable fact is identical
    -- nothing is being collected there right now.
    """
    glyph = 1 if ascii_mode else 0
    # No status object at all rather than an empty one: that is what a
    # PollerSet looks like between construction and the first poll landing,
    # and it means exactly what a never-polled status means.
    if snapshot is None:
        return MARKER_IDLE[glyph], STYLE_IDLE
    if snapshot["last_error"]:
        return MARKER_FAILED[glyph], STYLE_FAILED
    last = snapshot["last_success"]
    if last is not None and (utcnow() - last).total_seconds() <= interval * 2:
        return MARKER_OK[glyph], STYLE_OK
    return MARKER_IDLE[glyph], STYLE_IDLE


def _strip_text(
    entries: Sequence[StripEntry],
    *,
    named: bool,
    no_color: bool,
    ascii_mode: bool,
) -> Text:
    """The strip in one of its two forms, from the same entries.

    Three spaces between named entries and one between bare markers. A single
    space in the named form reads as `site01 ● site02` -- the marker sits
    between two names and belongs to neither -- while bare markers have
    nothing to be confused with and only need to be countable.
    """
    line = Text(no_wrap=True, overflow="crop" if ascii_mode else "ellipsis")
    separator = "   " if named else " "
    for index, (name, marker, marker_style, focused) in enumerate(entries):
        if index:
            line.append(separator)
        emphasis = "" if no_color else ("bold" if focused else "dim")
        if focused:
            line.append("[", style=emphasis)
        if named:
            line.append(f"{name} ", style=emphasis)
        line.append(marker, style="" if no_color else marker_style)
        if focused:
            line.append("]", style=emphasis)
    return line


def build_status_strip(
    sites: Sequence[SiteConfig],
    snapshots: Mapping[str, Mapping[str, Any]],
    *,
    focus: str,
    width: int,
    no_color: bool,
    ascii_mode: bool,
) -> Text:
    """One line saying how the sites that are *not* on screen are doing.

    Showing one site at a time (D4) has exactly one weakness: a site nobody
    has pressed Tab to in an hour can have been failing for that hour without
    the frame ever admitting it. This line is the answer, and it is what the
    extra header row is spent on.

    Two forms, and the width picks between them. Named --
    ``[site01 ●]   site02 ✗`` -- while the names fit; markers alone when they
    do not. Twelve sites is a hundred characters of names, and a terminal 40
    columns wide would crop that after the third, dropping the health of the
    remaining nine *and* looking like a complete answer while doing it. The
    compact form keeps every marker, which is the one thing the line is for;
    the identity line immediately above is already saying which site the
    numbers belong to.

    Both forms are in config order, which is the order `1`-`9` jump in, so a
    marker's position in the compact form is the key that focuses it.

    The focused entry is bracketed as well as emphasised because the bracket
    is the half of that emphasis which survives ``NO_COLOR``, where every
    style in this file collapses to nothing.

    `width` is the terminal's, not the panel's: `PANEL_CHROME` is this
    module's own border and padding, so subtracting it here keeps the caller
    from having to know how the header is built.
    """
    entries: list[StripEntry] = [
        (
            site.name,
            *status_marker(
                snapshots.get(site.name), site.interval, ascii_mode
            ),
            site.name == focus,
        )
        for site in sites
    ]
    named = _strip_text(
        entries, named=True, no_color=no_color, ascii_mode=ascii_mode
    )
    if named.cell_len <= max(1, width - PANEL_CHROME):
        return named
    return _strip_text(
        entries, named=False, no_color=no_color, ascii_mode=ascii_mode
    )


def header_height(snapshot: Mapping[str, Any]) -> int:
    """How many lines the header will take, before it has been built.

    Two borders, the identity line and the status strip, plus the error line
    when there is one: 4 or 5, where it was 3 or 4 before the strip. That
    extra row comes out of the chart's budget at every terminal size, which is
    the price of the one thing a one-site-at-a-time dashboard cannot
    otherwise give you -- knowing that the other eleven sites are still
    collecting.

    The budget needs this number first and the panel has to be exactly it.
    They lived one line apart in the single file and could be read together;
    now they are in different modules, so the number is computed once here and
    both callers ask.
    """
    return 5 if snapshot["last_error"] else 4


def build_header(
    identity: SiteIdentity,
    snapshot: Mapping[str, Any],
    interval: int,
    no_color: bool,
    ascii_mode: bool = False,
    *,
    strip: Text,
) -> Panel:
    """Which site, how fresh its numbers are, and how the others are doing.

    Three lines at most: the identity line, the status strip, and the error
    line when there is one.

    With one site on screen at a time, a frame that does not say which site it
    is showing is a frame that cannot be read after pressing Tab -- and the
    local clock beside the zone is what makes two sites at different points in
    their own day legible rather than confusing.

    `strip` is built by the caller and required rather than optional:
    `header_height` counts its line unconditionally, and an omitted one would
    leave the panel a line shorter than the budget was told to expect.
    """
    now_local = utcnow().astimezone(identity.tz)
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
    line = Text(no_wrap=True, overflow="crop" if ascii_mode else "ellipsis")
    line.append(f"{identity.display_name} ", style="" if no_color else "bold")
    line.append(f"({identity.property_id})  ", style="" if no_color else "dim")
    line.append(f"{identity.tz_name}  ")
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

    # The strip goes on its own line, always -- including with a single site
    # configured, where it does repeat what the line above already says. A
    # header that changed height when a site was disabled would shift the
    # whole frame under the reader in exchange for one row.
    line.append("\n")
    line.append_text(strip)

    if snapshot["last_error"]:
        line.append("\n")
        line.append(
            f"last error: {snapshot['last_error'][:MAX_ERROR_CHARS]}",
            style="" if no_color else "red",
        )
    return Panel(
        line,
        padding=(0, 1),
        box=frame_box(ascii_mode),
        height=header_height(snapshot),
    )


def build_events_table(
    events: Sequence[tuple[str, int]],
    width: int,
    conversions: Collection[str],
    ascii_mode: bool,
    no_color: bool,
    *,
    color: str,
    conversion_color: str,
) -> Table:
    """Today's events by count, with the configured conversions emphasised.

    The breakdown is by event rather than by page, and that is a limit of the
    data rather than a choice: the Realtime API has no pagePath dimension, and
    unifiedScreenName returns the page *title*. Counting events instead is
    also what puts a conversion on screen live.
    """
    table = Table(
        expand=True,
        padding=(0, 1),
        show_edge=True,
        box=frame_box(ascii_mode),
        title="Top events today",
    )
    table.add_column("event", ratio=3, no_wrap=True)
    # no_wrap on a fixed width column too: a site busy enough to reach a
    # seven-figure count would otherwise wrap "1,234,567" onto a second line
    # and spend a row the budget had already given to the chart.
    table.add_column("count", justify="right", width=8, no_wrap=True)
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
            # The emphasis style is the conversion colour the chart's rug uses
            # and the footer's total is written in, so one event is one colour
            # wherever it appears in the frame.
            style = (
                f"bold {conversion_color}" if name in conversions else color
            )
        table.add_row(name, f"{count:,}", Text(bar_char * filled, style=style))
    return table


def shorten_path(path: Path) -> str:
    """The database path, shortened against home when it lives there.

    The absolute path is long enough on its own to fill the footer -- the
    Windows default is `C:\\Users\\name\\AppData\\Local\\ga4-realtime\\...` --
    and the line is cropped rather than wrapped, so every character spent here
    is one taken off the end. The repo-relative form this had went with the
    repo-relative layout: the database now lives in the platform data
    directory or wherever the config points it, and home is the only base
    those two share.
    """
    try:
        return f"~{os.sep}{path.relative_to(Path.home())}"
    except ValueError:
        # Not under home: an absolute path elsewhere on disk, printed whole.
        return str(path)
    except RuntimeError:
        # Path.home() raises rather than returning None when it cannot work
        # out where home is -- a service account on Windows, typically.
        return str(path)


def build_footer(
    totals: DayTotals,
    quota: object | None,
    db_path: Path,
    db_bytes: int,
    no_color: bool,
    ascii_mode: bool = False,
    *,
    started: float,
    conversion_color: str,
) -> Panel:
    """The day's totals, and where the numbers behind them are being kept."""
    quota_text = "quota n/a"
    if quota is not None:
        hourly = getattr(quota, "tokens_per_hour", None)
        daily = getattr(quota, "tokens_per_day", None)
        parts = []
        if hourly is not None:
            budget = hourly.consumed + hourly.remaining
            parts.append(f"hour {hourly.remaining}/{budget}")
        if daily is not None:
            parts.append(f"day {daily.remaining}")
        if parts:
            quota_text = "tokens " + "  ".join(parts)

    line = Text(no_wrap=True, overflow="crop" if ascii_mode else "ellipsis")
    line.append(f"page views {totals.page_views:,}   ")
    line.append(f"events {totals.events:,}   ")
    line.append(
        f"conversions {totals.conversions_total:,}",
        style="" if no_color else f"bold {conversion_color}",
    )
    # The per-event breakdown comes after the total and never before it. The
    # line is no_wrap inside a panel of fixed height, so what does not fit is
    # cropped from the right -- which makes the order here the decision about
    # what a narrow terminal loses first.
    if totals.conversions:
        breakdown = "  ".join(
            f"{name} {count:,}" for name, count in totals.conversions.items()
        )
        line.append(f"   ({breakdown})", style="" if no_color else "dim")
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
        f"{quota_text}   {shorten_path(db_path)} {db_bytes / 1024:.0f} KiB   "
        f"{totals.minutes_recorded} min recorded   up {uptime}   q to quit",
        style="" if no_color else "dim",
    )
    return Panel(
        line, padding=(0, 1), box=frame_box(ascii_mode), height=FOOTER_HEIGHT
    )
