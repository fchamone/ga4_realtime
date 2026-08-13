"""List every site in all three of its possible states.

In the config and enabled, in the config and disabled, and in `site_meta` but
not in the config -- the last marked *orphaned*, because nothing you collected
should become unreachable by editing a text file (D17). The two sources are
what makes the three states possible: the config file says which sites exist
*now*, `site_meta` says which ones have history, and `sites` is the command
that shows where they disagree.

**It must not create a database that is not there.** `RealtimeStore` creates
the file it is handed, so the existence check happens before the open and the
missing case is reported in words. A run that quietly brought an empty
database into being would make the *next* run report a file this one invented.

**It needs no config when `--db` is given** (§6, D15): the names, property IDs
and timezones all come out of `site_meta`, so this works against a database
copied off another machine with no config file, no credentials and no network.
In that mode there is nothing for a site to be orphaned *from*, and the
listing says so instead of marking every stored site orphaned against a config
it never read.
"""

from pathlib import Path

from ..config import Config
from ..store import RealtimeStore, SiteMeta
from . import Context

# The three states of §7.9, as the words printed beside each name, plus the
# fourth that only exists without a config: "stored" is what a site is when
# nothing has said whether it is still configured.
ENABLED = "enabled"
DISABLED = "disabled"
ORPHANED = "orphaned"
STORED = "stored"


def run(ctx: Context) -> int:
    database = ctx.database()
    config = ctx.config() if _consults_config(ctx) else None
    stored = _stored_sites(database)

    print(f"database  {database}")
    if config is None:
        print("config    not consulted: --db names the database, so nothing")
        print("          here says whether a site is still configured.")
    else:
        print(f"config    {config.source}")
    print()

    missing = stored is None
    if stored is None:
        print("There is no database at that path yet. The first poll of the")
        print("live dashboard creates it; nothing here does.")
        print()
        stored = {}

    rows = _rows(config, stored)
    if not rows:
        # Only when a database is there and empty. Saying "no sites recorded"
        # of a file that does not exist would read as a fact about its
        # contents, and the paragraph above has already said the truer thing.
        if not missing:
            print("No sites recorded in this database yet -- the live view")
            print("writes one row per site on its first poll.")
        return 0

    width = max(len(name) for name, _ in rows)
    for name, state in rows:
        # Each block ends with its own blank line, so the summary needs none
        # in front of it.
        _print_site(name, state, width, stored.get(name), config)
    print(_summary(rows))
    return 0


def _consults_config(ctx: Context) -> bool:
    """Whether the config file has anything to add to this listing.

    Skipped when the user pointed at a database and named no config file. §6
    says config is consulted only to *find* the database, and with `--db` that
    job is already done -- so listing a database copied from somewhere else
    does not fail because this machine has never run `init`. An explicit
    `--config` is an answer rather than a search, so it is read even alongside
    `--db`: naming both is how a stored history gets compared against the
    config that is supposed to describe it.
    """
    return ctx.args.config is not None or ctx.args.db is None


def _stored_sites(database: Path) -> dict[str, SiteMeta] | None:
    """What `site_meta` holds, or None when there is no database yet.

    The existence check before the open is the whole of the promise: pointing
    `RealtimeStore` at a path *creates* it, parent directory and all, so
    asking the store first would answer "no sites" by manufacturing the
    database it then reported on.
    """
    if not database.is_file():
        return None
    store = RealtimeStore(database)
    try:
        return {meta.site: meta for meta in store.list_sites()}
    finally:
        # On Windows more than anywhere else: an open connection holds a
        # handle on the file and leaves the -wal sidecar unfolded.
        store.close()


def _rows(
    config: Config | None, stored: dict[str, SiteMeta]
) -> list[tuple[str, str]]:
    """Every name to print, with its state, config order first.

    Configured sites keep the file's order because that is the order the
    dashboard cycles through them in, and it is the order the user can change.
    Orphans have no such order to keep, so they are sorted and come last --
    they are history, not rotation.
    """
    if config is None:
        return [(name, STORED) for name in sorted(stored)]
    rows = [
        (site.name, ENABLED if site.enabled else DISABLED)
        for site in config.sites
    ]
    orphans = sorted(set(stored) - set(config.names))
    return rows + [(name, ORPHANED) for name in orphans]


def _print_site(
    name: str,
    state: str,
    width: int,
    meta: SiteMeta | None,
    config: Config | None,
) -> None:
    print(f"{name:<{width}}  {state}")
    if meta is None:
        # Distinct from "no such site", and worth its own sentence: the site
        # is configured and simply has not been polled yet, which is what
        # every site looks like for the first five minutes.
        print("  nothing stored yet -- this site has not been polled.")
    else:
        print(f"  property     {meta.property_id}")
        print(f"  stored as    {meta.display_name}")
        print(f"  timezone     {meta.tz_name}")
        print(f"  last written {meta.updated_utc}")
    if state == ORPHANED and config is not None:
        # The path goes on a line of its own rather than inside the sentence:
        # a config file three directories deep is longer than the terminal,
        # and a sentence wrapped around it reads as two broken ones.
        print("  Not in the config file, so nothing polls it:")
        print(f"    {config.source}")
        print("  What it collected stays readable:")
        print(f"    ga4-realtime report --site {name}")
    print()


def _summary(rows: list[tuple[str, str]]) -> str:
    """One line, counting only the states that actually occurred.

    Empty states are left out rather than printed as zeros: "0 orphaned" on
    every healthy run trains the reader to skip the line that matters on the
    one run where it is not zero.
    """
    total = len(rows)
    counts = [
        f"{sum(1 for _, state in rows if state == name)} {name}"
        for name in (ENABLED, DISABLED, ORPHANED, STORED)
        if any(state == name for _, state in rows)
    ]
    noun = "site" if total == 1 else "sites"
    return f"{total} {noun}: " + ", ".join(counts) + "."
