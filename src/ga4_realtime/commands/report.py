"""The daily summary, per site, from the local database alone.

Reads only SQLite: the site names, property IDs and timezones all come from
`site_meta`, so this works from `--db` with no config file, no credentials and
no network. `--site all` and the omitted form print one section per site --
each scoped to *that* site's local day, in exactly the format `--site NAME`
produces on its own -- followed by a combined total labelled as a sum of
differing spans.

**Why a sum of local days rather than one shared window.** Totalling every
site over a single span was rejected: each site's contribution would then
disagree with its own single-site report, and a reader comparing the two
would have no way to tell which of them was wrong. A labelled sum is a worse
number with an honest label, which is the better trade -- so the label is not
decoration and neither is the warning line that appears when the zones
differ.

**The section is built once and printed by both forms.** That is what makes
"byte-identical" true rather than aspirational: there is no second code path
to drift, and a script parsing one form parses the other.

**The furniture is ASCII.** The dashboard has `--ascii` for terminals that
cannot encode block glyphs; a report needs no such flag because nothing it
draws for itself -- headings, labels, the number columns, the coverage line --
reaches for a glyph that would need one. That is what makes the shape of the
output safe to pipe into a file, an e-mail or `findstr`.

The *data* inside it is another matter, and this module cannot promise
anything about it: a display name is whatever the property's owner typed and
an event name is whatever the site sends, both arriving through `site_meta`
untouched. Printed to a stream that cannot encode them -- a `cp1252` console,
redirection under a legacy codepage -- `print` raises `UnicodeEncodeError`,
which surfaces as a traceback rather than as the exit 1 a `ConfigError` gets.
Re-encoding the names here was rejected: a mangled display name is harder to
act on than a loud failure, and `PYTHONIOENCODING=utf-8` fixes it for every
command at once rather than for this one.

The config is consulted for exactly one thing -- which events count as
conversions -- and is optional even for that. A site no config file names --
orphaned, or read on a machine that has no config at all -- gets `n/a` there
rather than a zero: "nothing was configured" and "nothing happened" are
different answers, and printing the second for the first is the lie that
matters. A site that is merely `enabled = false` still has its block in the
file, so its conversions are known and counted like any other's.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..errors import ConfigError
from ..store import DayTotals, RealtimeStore, SiteMeta
from ..timeutil import day_bounds_utc, parse_minute, utcnow
from . import ALL_SITES, Context

# The label D14 requires on the combined block. A constant because it is the
# one part of that block a script is expected to look for, and because it is
# the sentence that makes the sum publishable at all.
SUM_LABEL = "sum of each site's local day"

# Fixed, rather than read from [ui] top_events. That key sizes a panel whose
# height the dashboard has to negotiate against a terminal; a text report has
# no such budget, and taking the number from the config would make `report`
# need a config file for cosmetics alone.
TOP_EVENTS = 10

# Label column and figure column, carried over from the single-file tool so a
# report from before the split and one from after line up in the same
# terminal. Every indented breakdown block below is sized to keep its numbers
# in the same column as these.
LABEL_WIDTH = 15
FIGURE_WIDTH = 8

# What stands in for a count nobody configured, and why it is not a number.
# Two wordings rather than one: the note follows the block it sits in, and
# "this site's" under a heading reading "all sites" would name a site the
# reader cannot point at.
UNKNOWN = "n/a"
UNKNOWN_NOTE = "(no config entry names this site's conversions)"
UNKNOWN_NOTE_ALL = "(no config entry names any site's conversions)"


@dataclass(frozen=True)
class _SiteDay:
    """One site's section, resolved: which zone, which day, what it holds.

    Built while the store is open and read after it has been closed, which is
    why it carries values rather than a cursor. `conversions` is None when no
    config named this site -- distinct from an empty list, which would mean a
    config named it and asked for nothing.
    """

    meta: SiteMeta
    tz: ZoneInfo
    tz_name: str
    day: date
    conversions: list[str] | None
    totals: DayTotals
    events: list[tuple[str, int]]


def run(ctx: Context) -> int:
    path = ctx.database()
    _refuse_a_missing_database(path)

    store = RealtimeStore(path)
    try:
        metas = _sites_to_report(store, ctx.args.site, path)
        configured = _configured_conversions(ctx)
        days = [
            _read(
                store,
                meta,
                configured.get(meta.site),
                tz_override=ctx.overrides.timezone,
                raw_date=ctx.args.date,
            )
            for meta in metas
        ]
    finally:
        store.close()

    for index, day in enumerate(days):
        # A blank line *between* sections and never inside one, so the section
        # a single-site run prints is the same text either way.
        if index:
            print()
        print("\n".join(section(day)))

    if len(days) > 1:
        # A sum of one is the section again, and printing it twice tells the
        # reader nothing they cannot already see.
        print()
        print("\n".join(combined(days)))
    return 0


# --------------------------------------------------------------------------
# What to report on
# --------------------------------------------------------------------------


def _refuse_a_missing_database(path: Path) -> None:
    """Fail before opening anything, because opening it would create it.

    `RealtimeStore` creates the file it is pointed at, parent directories
    included. Without this check a typo'd `--db` would leave an empty database
    behind and then report zeros out of it -- which reads exactly like a day
    on which nothing happened.
    """
    if path.exists():
        return
    raise ConfigError(
        f"no database at\n  {path}\n"
        "Nothing has been collected there yet. Run the dashboard so it can "
        "start polling:\n"
        "  ga4-realtime\n"
        "or point --db at the file another run wrote."
    )


def _sites_to_report(
    store: RealtimeStore, wanted: str | None, path: Path
) -> list[SiteMeta]:
    """Which sites this run prints, taken from the database (D17).

    From `site_meta` and not from the config, which is the whole of D17: a
    site disabled or deleted from the file still has rows, and nothing you
    collected should become unreachable because you edited a text file.
    """
    known = store.list_sites()
    if not known:
        raise ConfigError(
            f"the database at\n  {path}\n"
            "has no sites recorded in it: nothing has ever been polled into "
            "it.\n"
            "Run the dashboard once so it can collect a day and cache what "
            "it is polling:\n"
            "  ga4-realtime"
        )

    # "all" and the omitted form are the same thing here. They differ only for
    # the dashboard, which shows one site at a time and has no all-sites frame
    # for the word to mean anything else.
    if wanted is None or wanted == ALL_SITES:
        return known

    for meta in known:
        if meta.site == wanted:
            return [meta]

    names = ", ".join(meta.site for meta in known)
    raise ConfigError(
        f"no site named {wanted!r} in\n  {path}\n"
        f"Sites in this database: {names}.\n"
        "report reads the database rather than the config, so a site appears "
        "here once it has been polled at least once -- and stays, whether or "
        "not the config still names it."
    )


def _configured_conversions(ctx: Context) -> dict[str, list[str]]:
    """Which events each site counts as conversions, if anything says so.

    The one thing `report` wants from the config, and it is optional even
    here -- but only for a file *discovery* went looking for. Such a
    `ConfigError` is swallowed because reaching this line at all means `--db`
    was given: `ctx.database()` only consults the config when it was not, so
    a config that failed to load has already been proved unnecessary for the
    numbers. The cost is that the conversions cannot be named, which is what
    the `n/a` line says out loud -- a stored day staying unreadable until a
    syntax error in an unrelated file is fixed would be the worse outcome.

    A `--config` the user typed is not discovery and gets none of that (§6,
    step 1): it exists and parses, or the run stops. Swallowing there would
    print `n/a (no config entry names this site's conversions)` about a file
    that may well name them and was simply never opened -- a false statement
    dressed as a measurement, and the one lie this module is built to avoid.
    """
    try:
        config = ctx.config()
    except ConfigError:
        if ctx.args.config is not None:
            raise
        return {}
    return {site.name: site.conversions for site in config.sites}


def _read(
    store: RealtimeStore,
    meta: SiteMeta,
    conversions: list[str] | None,
    *,
    tz_override: str | None,
    raw_date: str | None,
) -> _SiteDay:
    """Everything one section needs, read while the store is open."""
    # site_meta already holds whatever the config asked for: the live view
    # resolves each site's timezone -- from the config key or from the Admin
    # API -- and caches it there, so reading the database gets the configured
    # answer without needing the file. --timezone is the documented debugging
    # override on top of that, and it forces every site into one zone so two
    # properties in different ones become comparable for a single run.
    tz_name = tz_override or meta.tz_name
    where = "--timezone" if tz_override else f"stored for site {meta.site!r}"
    tz = _zone(tz_name, where)

    day = _day(raw_date, tz)
    start, end = day_bounds_utc(day, tz)
    return _SiteDay(
        meta=meta,
        tz=tz,
        tz_name=tz_name,
        day=day,
        conversions=conversions,
        # An unknown conversion list is an empty query, not a missing one:
        # day_totals returns zeros for it and the section prints n/a instead
        # of them.
        totals=store.day_totals(meta.site, conversions or (), start, end),
        events=store.top_events(meta.site, start, end, limit=TOP_EVENTS),
    )


def _day(raw: str | None, tz: ZoneInfo) -> date:
    """The calendar day being reported, in the site's own zone.

    With no `--date`, "today" is today *there*, which is the point of storing
    the timezone: two sites eight hours apart are on different days for eight
    hours out of every twenty-four. With one, the same calendar date is read
    in each site's zone, so `--date 2026-08-05` means that Wednesday where
    each property lives rather than one shared span of UTC.

    `strptime` rather than `date.fromisoformat`, which since 3.11 also accepts
    `20260805` and `2026-W32-3` -- spellings this flag does not document and
    nobody would be able to guess were allowed. It does accept `2026-8-5`,
    which is the same date written loosely and not worth a second parser.
    """
    if raw is None:
        return utcnow().astimezone(tz).date()
    try:
        # The naive-datetime rule is silenced rather than satisfied: the
        # parsed value never becomes an instant. It is a calendar date, and
        # `day_bounds_utc` is what turns it into a span -- in the site's own
        # zone, which is the only zone that could be right here. Attaching a
        # tzinfo at this line would name a second one that means nothing.
        return datetime.strptime(raw, "%Y-%m-%d").date()  # noqa: DTZ007
    except ValueError:
        raise ConfigError(
            f"--date must be written YYYY-MM-DD, and {raw!r} is not.\n"
            "For example: --date 2026-08-05."
        ) from None


def _zone(name: str, where: str) -> ZoneInfo:
    """Resolve a timezone name, saying what a failure usually means.

    The same explanation `config.py` gives, because the failure is the same
    one and the fix is the same fix: on Windows there is no system IANA
    database at all, so a name that is spelled perfectly still fails until
    `tzdata` is installed. `where` names which of the two sources the name
    came from -- the database or the flag -- since only one of them is
    something the user can retype.
    """
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise ConfigError(
            f"timezone {name!r} ({where}) could not be resolved.\n"
            "If the name is right, Python has no system timezone database "
            "on this machine -- Windows ships none. Install the data "
            "package:\n"
            "  python -m pip install tzdata"
        ) from None
    except (ValueError, KeyError):
        raise ConfigError(
            f"timezone {name!r} ({where}) is not a valid IANA name.\n"
            'It looks like "America/Sao_Paulo" or "Asia/Tokyo".'
        ) from None


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def section(day: _SiteDay) -> list[str]:
    """One site's whole section, as lines.

    The single-site form prints exactly this, and the all-sites form prints
    exactly this per site with a blank line between them. One function, so
    "byte-identical" cannot become untrue by editing only one of two places.
    """
    meta = day.meta
    lines = [
        f"{meta.site} - {meta.display_name} ({meta.property_id})",
        f"{day.tz_name}   {day.day.isoformat()}",
        "",
        _figure("page views", day.totals.page_views),
        _figure("events", day.totals.events),
    ]
    lines.extend(_conversion_lines(day.conversions, day.totals))
    lines.append("")
    if day.events:
        lines.append("  top events")
        lines.extend(_breakdown(day.events, indent=4))
        lines.append("")
    lines.append(_coverage(day))
    return lines


def combined(days: Sequence[_SiteDay]) -> list[str]:
    """The labelled sum, and the warning that keeps it honest."""
    lines = [f"all sites - {SUM_LABEL}"]

    zones = sorted({day.tz_name for day in days})
    if len(zones) > 1:
        # One line, and it names the zones rather than saying "some sites
        # differ": the reader's next question is always which ones, and an
        # answer that does not carry it is a line worth deleting.
        lines.append(
            f"  warning: timezones differ ({', '.join(zones)}), so the days "
            "summed below are not one span of time."
        )
    lines.append("")
    lines.append(
        _figure("page views", sum(day.totals.page_views for day in days))
    )
    lines.append(_figure("events", sum(day.totals.events for day in days)))
    lines.extend(_combined_conversions(days))
    return lines


def _combined_conversions(days: Sequence[_SiteDay]) -> list[str]:
    """The conversion total across sites, and what it left out.

    Conversion names are per site, so the merge is by name: two sites both
    counting `purchase` add up, and a name only one of them counts appears
    with only that one's figure.
    """
    known = [day for day in days if day.conversions is not None]
    if not known:
        return [_unknown_conversions(UNKNOWN_NOTE_ALL)]

    merged: dict[str, int] = {}
    for day in known:
        for name, count in day.totals.conversions.items():
            merged[name] = merged.get(name, 0) + count

    line = _figure("conversions", sum(merged.values()))
    if len(known) < len(days):
        # Said on the line rather than left to the reader to work out from the
        # n/a above: a total that silently omits a site is the kind of number
        # somebody copies into a report of their own.
        line += f"  (from {len(known)} of {len(days)} sites)"
    return [line, *_breakdown(sorted(merged.items()), indent=4)]


def _conversion_lines(
    conversions: list[str] | None, totals: DayTotals
) -> list[str]:
    if conversions is None:
        return [_unknown_conversions()]
    # Name order rather than config order, so two sites listing the same
    # events in different orders still produce comparable sections. Every
    # configured name is in the dict, zero included -- store.day_totals seeds
    # it -- which is what makes an event that did not fire today visibly zero
    # rather than absent.
    return [
        _figure("conversions", totals.conversions_total),
        *_breakdown(sorted(totals.conversions.items()), indent=4),
    ]


def _unknown_conversions(note: str = UNKNOWN_NOTE) -> str:
    """The figure line for a count nothing configured, in either block.

    Same line, same columns, different note -- so the two blocks stay
    parseable as one shape while neither of them claims to be the other.
    """
    return (
        f"  {'conversions':<{LABEL_WIDTH}}  {UNKNOWN:>{FIGURE_WIDTH}}  {note}"
    )


def _figure(label: str, value: int) -> str:
    return f"  {label:<{LABEL_WIDTH}}  {value:>{FIGURE_WIDTH},}"


def _breakdown(pairs: Sequence[tuple[str, int]], *, indent: int) -> list[str]:
    """Indented name/count lines, self-sizing to the longest name.

    The floor keeps the numbers in the same column as the figures above when
    every name is short, and the max lets a long event name push the column
    right rather than be truncated: an event name is what the user typed into
    their config, and half of one is not something they can act on.
    """
    if not pairs:
        return []
    width = max(LABEL_WIDTH - indent + 2, *(len(name) for name, _ in pairs))
    return [
        f"{' ' * indent}{name:<{width}}  {count:>{FIGURE_WIDTH},}"
        for name, count in pairs
    ]


def _coverage(day: _SiteDay) -> str:
    """How much of the day actually has rows behind it.

    Worth a line of its own because the tool only sees what it was running
    for: a page-view total is a different thing when it covers twelve hours
    than when it covers the twenty minutes somebody left the dashboard open.
    """
    totals = day.totals
    if not (totals.first_minute and totals.last_minute):
        return "  coverage: nothing recorded for this day"
    first = parse_minute(totals.first_minute).astimezone(day.tz)
    last = parse_minute(totals.last_minute).astimezone(day.tz)
    return (
        f"  coverage: {totals.minutes_recorded} minutes recorded, "
        f"{first:%H:%M} to {last:%H:%M}"
    )
