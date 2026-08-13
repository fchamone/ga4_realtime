"""Diagnose the config and API access, per site, and write nothing.

Config valid, credentials readable, Data API reachable, Admin API reachable or
the UTC fallback, timezone resolvable -- plus the resolved database and log
paths. Exits 1 when it finds a problem, naming the field and the file to edit.

**Nothing here writes a file, and that costs two deliberate omissions.**
`RealtimeStore` *creates* the database it is pointed at, so this module never
imports it: the path comes from `ctx.database()`, which resolves without
opening, and the only question asked of the filesystem is whether something is
already there. `ctx.start_logging()` is skipped for the same reason -- it
opens the rotating log, and a diagnostic that creates the very file it is
reporting on has told the user something that was not true when they asked.

The Data API is reached, and that is not a contradiction. There is no way to
find out whether a property will answer except to ask it, so each enabled site
is asked for **one minute** of realtime data and the rows are counted and
thrown away. Nothing is stored, nothing is corrected, no `poll_log` row is
written; the store is never opened at all.

**Errors are reported, not raised.** Every other command lets a `ConfigError`
travel up to `cli.main`, which prints it on stderr without a traceback and
exits 1. Here the unfilled field *is* the finding: it is printed inside the
report, in the place it belongs, and the run continues far enough to say what
it could not go on to check. The exit code is the same either way, so what the
catch buys is the report around it.

Everything printed goes to stdout, failures included. The report is one piece
of text meant to be read top to bottom or piped somewhere; half of it on
stderr would interleave into a different order than it was written in.
"""

import tomllib
from pathlib import Path

from ..config import SiteConfig, discover_config_path
from ..errors import ConfigError
from ..ga4 import GA4Client
from ..poller import build_client, diagnose
from . import Context

# The smallest read the Realtime API accepts: the current minute and nothing
# before it. `doctor` is asking whether the property answers at all, not what
# it says, so a wider window would cost the same round-trip and return rows
# nobody looks at.
PROBE_WINDOW = 1

# Plain words rather than tick and cross marks. This report goes to a stdout
# that `rich` never touches, and what a Windows console running a cp1252 code
# page does with a tick mark is raise UnicodeEncodeError -- out of the
# diagnostic command, which is the last place in the tool that helps anybody.
OK = "ok"
FAIL = "FAIL"
WARN = "warn"
# Informational lines: a path, or a state that is neither pass nor fail.
NOTE = ""

# Said once, at the end of every run including the failing ones, because it is
# the promise most easily broken by a later edit and the one a user pointing
# this at a production database needs to be sure of.
WROTE_NOTHING = (
    "Nothing was written: `doctor` reads the API and reports paths. It never "
    "opens\nthe database or the log, so neither is created by running it."
)


def run(ctx: Context) -> int:
    print("ga4-realtime doctor")
    print()

    try:
        config = ctx.config()
    except ConfigError as exc:
        # The one finding that stops the report: with no config there is no
        # site to check, no database path to resolve and nothing further to
        # say. The message names the field and the file to edit -- but
        # `load_config` is fail-fast on purpose, so it names only the FIRST
        # problem, and immediately after `init` there are exactly two. The
        # scan below lists every gap of the two shapes the starter config
        # ships with, so one run of `doctor` names them all; the repetition
        # when the load error is one of them is the price of the list being
        # complete.
        _line(FAIL, "config", "not usable yet")
        _detail(exc)
        for finding in _still_unfilled(ctx):
            _line(FAIL, "unfilled", finding)
        print()
        print(WROTE_NOTHING)
        return 1

    _line(OK, "config", str(config.source))
    # Resolved, never opened -- see the module docstring. `exists()` is a stat
    # and creates nothing, and whether the file is already there is the first
    # thing somebody diagnosing an empty dashboard wants to know.
    _line(NOTE, "database", _where(ctx.database()))
    _line(NOTE, "log", _where(ctx.log_path()))

    problems = 0
    for site in config.sites:
        problems += _check_site(site)

    print()
    checked = sum(1 for site in config.sites if site.enabled)
    print(f"{_count(checked, 'site')} checked, {_problems(problems)}.")
    print(WROTE_NOTHING)
    if problems:
        print(
            "Fix what is marked FAIL above, then run `ga4-realtime doctor` "
            "again."
        )
    return 1 if problems else 0


def _still_unfilled(ctx: Context) -> list[str]:
    """Every field the starter config leaves for the user, best effort.

    `init` writes exactly two gaps: an empty `property_id`, and a
    `credentials` path where nothing is saved yet. This re-reads the raw
    TOML and lists both shapes for every site, so the report after `init`
    is one round rather than fix-one-find-the-next. It is a scan for those
    two gaps, not a second validator -- everything else is `load_config`'s
    message above, and a file this cannot even parse adds nothing to it.
    """
    try:
        path = discover_config_path(ctx.args.config, cwd=ctx.cwd)
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (ConfigError, OSError, tomllib.TOMLDecodeError):
        return []

    defaults = data.get("defaults", {})
    sites = data.get("sites", [])
    if not isinstance(defaults, dict) or not isinstance(sites, list):
        return []

    findings: list[str] = []
    for table in sites:
        if not isinstance(table, dict):
            continue
        merged = {**defaults, **table}
        name = merged.get("name", "?")
        pid = merged.get("property_id")
        if isinstance(pid, str) and not pid.strip():
            findings.append(
                f"site {name!r}: property_id is still empty -- edit {path}"
            )
        creds = merged.get("credentials")
        if isinstance(creds, str) and creds and "${" not in creds:
            # config.py's resolution rule: expanduser, then against the
            # config file's own directory, never against cwd. `${VAR}`
            # values are skipped -- resolving them well is the loader's job,
            # and a guess here could name a file that is not the one the
            # loader would open.
            resolved = Path(creds).expanduser()
            if not resolved.is_absolute():
                resolved = path.parent / resolved
            if not resolved.is_file():
                findings.append(
                    f"site {name!r}: credentials -> {resolved} "
                    "(nothing saved there yet)"
                )
    return findings


# --------------------------------------------------------------------------
# One site
# --------------------------------------------------------------------------


def _check_site(site: SiteConfig) -> int:
    """Check one site, printing as it goes; return the problems found."""
    print()
    print(f"site {site.name!r} -- property {site.property_id}")

    if not site.enabled:
        # Not checked, and not counted as a problem: `enabled = false` is a
        # choice, and spending a round-trip to report on a property nobody
        # polls would make `doctor` slower the more sites you had switched
        # off. What was collected before it was switched off stays readable,
        # which is the half of D17 worth repeating here.
        _line(NOTE, "disabled", "not polled, and nothing here is checked")
        _detail(
            "What was already collected stays readable:\n"
            f"  ga4-realtime report --site {site.name}"
        )
        return 0

    try:
        client = build_client(site)
    except ConfigError as exc:
        # `config.py` has already proved the key file exists and can be
        # opened, so what fails at construction is the JSON inside it:
        # truncated, or not a service account key at all. The message names
        # both the file and this site.
        _line(FAIL, "credentials", str(site.credentials))
        _detail(exc)
        # Everything below needs a client, so this site's remaining checks are
        # not attempted -- reporting them as failures too would turn one
        # broken key into three problems to fix.
        return 1

    _line(OK, "credentials", str(site.credentials))
    return _check_data_api(client, site) + _check_property(client, site)


def _check_data_api(client: GA4Client, site: SiteConfig) -> int:
    try:
        rows = client.poll_realtime(PROBE_WINDOW)
    except Exception as exc:  # noqa: BLE001 - diagnose, never traceback
        # Broad on purpose, the way priming is (`PollerSet._prime_site`):
        # what arrives here is not only the API's own refusals but everything
        # the auth and transport layers throw before the API answers -- a
        # revoked key's RefreshError, an offline machine's TransportError --
        # none of which is a GoogleAPICallError, and a diagnostic command
        # that met one with a traceback would be failing at its one job.
        # `diagnose` is the poller's, reused rather than reworded: it names
        # the site, the property and the GA4 screen to fix for the refusals
        # it knows, and still names the class and the site for the rest.
        _line(FAIL, "Data API", "did not answer")
        _detail(diagnose(site, exc))
        return 1
    _line(OK, "Data API", f"answered, {_count(len(rows), 'row')} this minute")
    return 0


def _check_property(client: GA4Client, site: SiteConfig) -> int:
    """The Admin API and the timezone, which are one call and two answers."""
    try:
        info = client.fetch_property(site.timezone)
    except ConfigError as exc:
        # A zone name that will not resolve, which on Windows means tzdata
        # is not installed. `fetch_property` turns the Admin API's own
        # refusals into the UTC-fallback warning rather than raising them,
        # so this branch is the config's failure, not Google's.
        _line(FAIL, "timezone", "could not be resolved")
        _detail(exc)
        return 1
    except Exception as exc:  # noqa: BLE001 - diagnose, never traceback
        # What `fetch_property` does *not* catch: auth and transport failures
        # raised before the Admin API answers at all -- the same classes the
        # Data API probe above has to survive. At run time these mark the
        # site failed at priming rather than degrading it to UTC, so exit 1
        # here mirrors what the dashboard would do with the same key.
        _line(FAIL, "Admin API", "did not answer")
        _detail(diagnose(site, exc))
        return 1

    if site.timezone is not None:
        # The Admin API is not consulted at run time either when a site names
        # its own zone, so checking it here would report a failure the
        # dashboard would never meet -- and a green line for a call the tool
        # does not make is worse than no line.
        _line(NOTE, "Admin API", "not consulted: this site sets its timezone")
        _line(OK, "timezone", f"{info.tz_name} (set for this site)")
        return 0

    if info.warning is not None:
        # A warning, not a failure, and the exit code stays 0. This is a
        # degradation the tool is built to survive: the site is polled, the
        # numbers are right, and only the day boundaries move to UTC. It is
        # also fixed in the Google console rather than in a field of a file,
        # which is what exit 1 is reserved for.
        _line(WARN, "Admin API", "refused; day boundaries fall back to UTC")
        _detail(info.warning)
        _line(OK, "timezone", f"{info.tz_name} (the fallback)")
        return 0

    _line(OK, "Admin API", info.display_name)
    _line(OK, "timezone", f"{info.tz_name} (from the Admin API)")
    return 0


# --------------------------------------------------------------------------
# Printing
# --------------------------------------------------------------------------


def _line(state: str, label: str, detail: str) -> None:
    """One check as three fixed columns, so the states read down the page."""
    print(f"  {state:<4}  {label:<12}  {detail}")


def _detail(text: object) -> None:
    """A multi-line explanation, indented under the check it belongs to.

    Indented rather than reflowed: these messages arrive already broken into
    lines that mean something -- a path on its own, a command to copy -- and
    rewrapping them to the terminal would join the two.
    """
    for line in str(text).splitlines():
        print(f"        {line}")


def _where(path: Path) -> str:
    state = "exists" if path.exists() else "not created yet"
    return f"{path}  ({state})"


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _problems(number: int) -> str:
    if number == 0:
        return "no problems found"
    return f"{_count(number, 'problem')} found"
