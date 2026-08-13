"""Argument parsing, dispatch and exit codes.

The whole argument surface is declared here, in one parser, and every command
lives in its own module under `commands/`. `main` builds one `Context`, looks
`run` up on the module the subcommand names, and turns whatever comes back
into an exit code: **0** for success, **1** for a `ConfigError`, **2** for an
argument the parser refused, **130** for `Ctrl-C`.

`main` **returns** those codes rather than raising them, argparse's own
`SystemExit` included. That keeps the four in one function where they can be
read together and tested as one call, and leaves `__main__.py` as the only
place a code becomes a process exit.

Two shapes here are load-bearing rather than incidental.

**Every global flag is declared before `add_subparsers()`**, so one spelling
works on the bare invocation -- which is the dashboard, and has no subcommand
word to hang a flag off -- and on every subcommand. This is existing
behaviour, carried over deliberately.

**The bounds are argparse's job.** `--interval 0` and `--window 25` are
argument errors and exit 2, with a message naming the bound. Everything a user
can fix in the *config file* is a `ConfigError` and exits 1. The split is what
keeps `report --date yesterday` at exit 1 with an explanation instead of exit
2 with argparse's own wording.
"""

import argparse
import sys
from pathlib import Path

from . import __version__
from .commands import Context, doctor, init, live, report, sites
from .config import (
    DEFAULT_INTERVAL,
    DEFAULT_REFRESH,
    DEFAULT_WINDOW_HOURS,
    MAX_WINDOW_HOURS,
    Overrides,
)
from .errors import ConfigError

# D16 in one line. It is in the epilog rather than in each flag's help
# because the rule has no exceptions to enumerate: stating it once is the
# accurate way to state it.
PRECEDENCE_NOTE = (
    "A command-line flag overrides [defaults] and every per-site value, for "
    "every site."
)

# The subcommand word, or None for the bare invocation, against the module
# that answers for it. Modules rather than functions on purpose: `run` is
# read at call time, so replacing it -- a test, or the next task filling in
# a stub -- needs no change here.
DISPATCH = {
    None: live,
    "report": report,
    "init": init,
    "doctor": doctor,
    "sites": sites,
}


def _at_least(minimum: int):
    """An argparse type that rejects anything under `minimum`, by name."""

    def _parse(text: str) -> int:
        value = _whole(text)
        if value < minimum:
            raise argparse.ArgumentTypeError(
                f"must be at least {minimum}; got {value}"
            )
        return value

    return _parse


def _between(minimum: int, maximum: int):
    """An argparse type for a closed range, naming both ends when it fails."""

    def _parse(text: str) -> int:
        value = _whole(text)
        if not minimum <= value <= maximum:
            raise argparse.ArgumentTypeError(
                f"must be between {minimum} and {maximum}; got {value}"
            )
        return value

    return _parse


def _whole(text: str) -> int:
    # argparse's own message for a failed int() names the type ("invalid int
    # value") rather than the mistake. "must be a whole number" is the same
    # wording config.py uses for the same error in the file, so a user who
    # has seen one recognises the other.
    try:
        return int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"must be a whole number, not {text!r}"
        ) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ga4-realtime",
        description=(
            "Poll the GA4 Realtime API on a schedule and render a live "
            '"today so far" terminal dashboard.\n'
            "With no subcommand, that dashboard is what runs."
        ),
        epilog=PRECEDENCE_NOTE,
        # Raw, so the description and the epilog keep the line breaks they
        # were written with. Option help is still rewrapped to the terminal.
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Global flags are declared before add_subparsers() so that they also
    # work on the bare invocation, which is the live dashboard.
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "config file to read; otherwise ./ga4-realtime.toml, then the "
            "platform config directory"
        ),
    )
    parser.add_argument(
        "--site",
        default=None,
        metavar="NAME",
        help=(
            "site to act on; 'all' means every site, and the dashboard "
            "opens on the first enabled one"
        ),
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "SQLite file to use, relative to the current directory; "
            "report and sites need nothing else"
        ),
    )
    parser.add_argument(
        "--interval",
        type=_at_least(1),
        default=None,
        metavar="SECONDS",
        help=f"seconds between polls (config default: {DEFAULT_INTERVAL})",
    )
    parser.add_argument(
        "--refresh",
        type=_at_least(1),
        default=None,
        metavar="SECONDS",
        help=f"seconds between redraws (config default: {DEFAULT_REFRESH})",
    )
    parser.add_argument(
        "--window",
        type=_between(1, MAX_WINDOW_HOURS),
        default=None,
        metavar="HOURS",
        help=(
            "hours of history on the chart, 1 to "
            f"{MAX_WINDOW_HOURS} (config default: {DEFAULT_WINDOW_HOURS})"
        ),
    )
    parser.add_argument(
        "--timezone",
        default=None,
        metavar="ZONE",
        help=(
            "debugging override that forces every site's day boundaries "
            "into this zone, e.g. America/Sao_Paulo; the per-site timezone "
            "key is the place to set one for good"
        ),
    )
    parser.add_argument(
        "--ascii",
        action="store_true",
        help="plain ASCII plots and bars, for terminals without braille",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="add a log panel to the dashboard and log at debug level",
    )
    # Bare "0.1.0" rather than "ga4-realtime 0.1.0": the value is read back
    # from the installed distribution metadata, so it is also what a script
    # comparing versions would want to parse.
    parser.add_argument(
        "--version",
        action="version",
        version=__version__,
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    report_parser = subparsers.add_parser(
        "report", help="print a daily summary from the local database"
    )
    # A plain string, not a date type. A malformed date is something the user
    # can fix and gets a ConfigError naming the format -- exit 1 -- rather
    # than argparse's exit 2, which would read like a usage mistake.
    report_parser.add_argument(
        "--date", default=None, metavar="YYYY-MM-DD", help="default: today"
    )

    init_parser = subparsers.add_parser(
        "init", help="write a starter config here"
    )
    init_parser.add_argument(
        "--path",
        type=Path,
        default=None,
        metavar="PATH",
        help="where to write it (default: ./ga4-realtime.toml)",
    )

    subparsers.add_parser(
        "doctor", help="check the config and API access, per site"
    )
    subparsers.add_parser(
        "sites", help="list configured, disabled and orphaned sites"
    )
    return parser


def overrides_from(args: argparse.Namespace) -> Overrides:
    """The flags that outrank the file, as data. D16.

    `ascii` is True or None and never False: it is a store_true, so a flag
    that was never typed and a flag explicitly turned off are the same thing
    to argparse, and False would claim the user asked for colour blocks.
    """
    return Overrides(
        database=args.db,
        interval=args.interval,
        refresh=args.refresh,
        window=args.window,
        timezone=args.timezone,
        ascii=True if args.ascii else None,
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point for the console script and for ``python -m``."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse ends the process itself for --help, --version and every
        # usage error: 0 for the first two, 2 for a usage error. Catching it
        # here -- around the parse, never around a command -- is what keeps
        # main() total, so every exit code this tool can produce is a value
        # it returned rather than one of two ways out. SystemExit.code may
        # also be None or a message by its own contract; argparse is not
        # known to use either, and the fallback maps them anyway because
        # "not known to" is not a contract.
        code = exc.code
        return code if isinstance(code, int) else (0 if code is None else 2)

    context = Context(
        args=args, overrides=overrides_from(args), cwd=Path.cwd()
    )
    try:
        if args.command is None:
            # The dashboard is the only command where --site has to name a
            # site the *config* knows: report and sites accept any name in
            # site_meta, orphans included (D17). Resolving it here rather
            # than inside live.run means a typo fails before a poller thread
            # or a Live region exists, while stdout is still a terminal.
            context.focus()
        return DISPATCH[args.command].run(context)
    except ConfigError as exc:
        # Without a traceback, always. Every ConfigError names the file to
        # edit and the fix; a stack trace above it would bury that under
        # frames of this tool's own internals.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
