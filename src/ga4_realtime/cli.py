"""Argument parsing, dispatch and exit codes.

Minimal for now: ``--version`` is the whole surface. The global flags, the
init/doctor/sites/report subcommands and the ConfigError-to-exit-1 handling
land together in one later task, because the global flags have to be declared
*before* ``add_subparsers()`` for them to apply to the bare invocation -- which
is the dashboard -- and splitting that across tasks would serialise them for
no reason.
"""

import argparse

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ga4-realtime",
        description=(
            "Poll the GA4 Realtime API on a schedule and render a live "
            '"today so far" terminal dashboard.'
        ),
    )
    # Bare "0.1.0" rather than "ga4-realtime 0.1.0": the value is read back
    # from the installed distribution metadata, so it is also what a script
    # comparing versions would want to parse.
    parser.add_argument(
        "--version",
        action="version",
        version=__version__,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the console script and for ``python -m``."""
    parser = build_parser()
    parser.parse_args(argv)

    # --version is an argparse "version" action: it prints and exits 0 before
    # ever reaching here. Anything else that gets this far is a command that
    # has not landed yet, and parser.error's exit 2 says so loudly. A silent
    # exit 0 would look exactly like success.
    parser.error("no command yet -- only --version is implemented")
