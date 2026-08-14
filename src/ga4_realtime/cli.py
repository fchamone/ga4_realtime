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

**And declared a second time on every subparser**, so the word order is the
user's choice rather than the parser's: `ga4-realtime --site clientb report`
and `ga4-realtime report --site clientb` both work, and typing the flag on
both sides resolves to the one nearest the end of the line. The second
registration defaults to `argparse.SUPPRESS`, which is load-bearing and
explained on `_add_global_flags`. Only the first order used to parse, which
put three of this tool's own messages in the position of printing a command
that exits 2.

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


def _add_global_flags(
    parser: argparse.ArgumentParser, *, suppress: bool
) -> None:
    """Declare the global flags on `parser`, once with defaults and once not.

    They are registered twice: on the top-level parser, before
    `add_subparsers()`, which is what makes them work on the bare
    invocation -- the dashboard, which has no subcommand word to hang a flag
    off -- and again on every subparser, so `report --site clientb` means
    what it looks like it means rather than exiting 2.

    `suppress` is what makes the second registration safe, and it is not
    optional. A subparser parses into a *fresh* namespace and then copies
    every key that namespace holds onto the one the top level has already
    filled in, so a second `default=None` would wipe
    `ga4-realtime --site clientb report` back to None on its way past --
    breaking the word order that used to be the only one that worked, in
    silence. `argparse.SUPPRESS` keeps an untyped flag out of the subparser's
    namespace entirely: the top-level value survives, a flag typed after the
    subcommand wins because it is the only one there, and a flag typed on
    both sides resolves to the one nearest the end of the line.
    """

    def unset(value):
        """What a flag nobody typed defaults to, on this registration."""
        return argparse.SUPPRESS if suppress else value

    parser.add_argument(
        "--config",
        type=Path,
        default=unset(None),
        metavar="PATH",
        help=(
            "config file to read; otherwise ./ga4-realtime.toml, then the "
            "platform config directory"
        ),
    )
    parser.add_argument(
        "--site",
        default=unset(None),
        metavar="NAME",
        help=(
            "site to act on; 'all' means every site, and the dashboard "
            "opens on the first enabled one"
        ),
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=unset(None),
        metavar="PATH",
        help=(
            "SQLite file to use, relative to the current directory; "
            "report and sites need nothing else"
        ),
    )
    parser.add_argument(
        "--interval",
        type=_at_least(1),
        default=unset(None),
        metavar="SECONDS",
        help=f"seconds between polls (config default: {DEFAULT_INTERVAL})",
    )
    parser.add_argument(
        "--refresh",
        type=_at_least(1),
        default=unset(None),
        metavar="SECONDS",
        help=f"seconds between redraws (config default: {DEFAULT_REFRESH})",
    )
    parser.add_argument(
        "--window",
        type=_between(1, MAX_WINDOW_HOURS),
        default=unset(None),
        metavar="HOURS",
        help=(
            "hours of history on the chart, 1 to "
            f"{MAX_WINDOW_HOURS} (config default: {DEFAULT_WINDOW_HOURS})"
        ),
    )
    parser.add_argument(
        "--timezone",
        default=unset(None),
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
        default=unset(False),
        help="plain ASCII plots and bars, for terminals without braille",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=unset(False),
        help="add a log panel to the dashboard and log at debug level",
    )
    # Bare "0.1.0" rather than "ga4-realtime 0.1.0": the value is read back
    # from the installed distribution metadata, so it is also what a script
    # comparing versions would want to parse.
    #
    # No `unset` here: `version` prints and exits without storing anything,
    # so it has no default to protect. It is registered on the subparsers
    # with the rest because "every global flag, on either side of the
    # subcommand word" is a rule worth having no exceptions to.
    parser.add_argument(
        "--version",
        action="version",
        version=__version__,
    )


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

    # Before add_subparsers(), carrying the real defaults: this registration
    # is the one that answers for the bare invocation.
    _add_global_flags(parser, suppress=False)

    # And the same flags again, as a parent every subparser inherits, so
    # they can be typed after the subcommand word too. `add_help=False`
    # because each subparser adds its own -h.
    global_flags = argparse.ArgumentParser(add_help=False)
    _add_global_flags(global_flags, suppress=True)

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    report_parser = subparsers.add_parser(
        "report",
        parents=[global_flags],
        help="print a daily summary from the local database",
    )
    # A plain string, not a date type. A malformed date is something the user
    # can fix and gets a ConfigError naming the format -- exit 1 -- rather
    # than argparse's exit 2, which would read like a usage mistake.
    report_parser.add_argument(
        "--date", default=None, metavar="YYYY-MM-DD", help="default: today"
    )

    init_parser = subparsers.add_parser(
        "init",
        parents=[global_flags],
        help="write a starter config here",
    )
    init_parser.add_argument(
        "--path",
        type=Path,
        default=None,
        metavar="PATH",
        help="where to write it (default: ./ga4-realtime.toml)",
    )

    subparsers.add_parser(
        "doctor",
        parents=[global_flags],
        help="check the config and API access, per site",
    )
    subparsers.add_parser(
        "sites",
        parents=[global_flags],
        help="list configured, disabled and orphaned sites",
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
