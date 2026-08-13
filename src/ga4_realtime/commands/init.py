"""Write a starter config, and refuse to overwrite one.

Refuses before writing anything if `ga4-realtime.toml` (or `--path`) or the
`.env.example` that goes beside it already exists, naming the file it would
have destroyed; otherwise it writes the pair. The
`[storage] database` line is written commented out: a config created in the
platform config directory that silently claimed a repo-relative path would
point at a directory that does not exist.

**The starter config does not lie**, which is the whole design of this
command. Every key it writes is a key `config.py` reads -- an unknown key is
only a warning, so a template full of near-misses would load perfectly and do
nothing. Every value it writes is one that is actually right for a new
install. And the two values that *cannot* be guessed still demand the
user's hand before the config loads: `property_id` is left visibly empty,
and `credentials` points at a path where nothing is saved yet -- so the
load fails with a message naming what to fix instead of failing at the API
with a permission error.

The two files are a pair, so the refusal is checked against both **before
either is written**. Writing the config and then refusing over `.env.example`
would leave the directory in a state where the next run refuses over the
config the previous run had just created, and the user never gets either file.
"""

from pathlib import Path

from .. import paths
from ..config import ENV_FILENAME
from ..errors import ConfigError
from . import Context

# Named after the file it is a template for, rather than spelled out twice:
# `config.py` reads `.env` from beside the config file, and this is the copy
# the user fills in and renames. If that filename ever moves, this follows it.
ENV_EXAMPLE_FILENAME = f"{ENV_FILENAME}.example"

# A raw string because of the Windows path in the [storage] comment. Wrapped
# at the same width as the rest of the codebase: this is a file people open in
# an editor, and it is the only documentation some of them will read.
CONFIG_TEMPLATE = r"""# ga4-realtime -- configuration
#
# Written by `ga4-realtime init`. Two fields need your input before this
# file loads: `property_id` is empty below, and `credentials` points at a
# path where nothing is saved yet. Sort both out, then run
# `ga4-realtime doctor`, which checks them without polling anything.
#
# Two rules worth knowing before editing:
#
#   * Every relative path in this file resolves against the directory THIS
#     FILE is in -- never against the directory you ran the command from. A
#     config in a project keeps its keys and its database inside that project.
#
#   * A command-line flag beats this file, for every site. `--interval 60`
#     polls every site every 60 seconds whatever is written here.
#
# Any string below may be written as ${VAR} and read from the environment or
# from a `.env` beside this file -- see the `.env.example` written with it. An
# unknown key is a warning naming the nearest valid spelling, not an error.


[defaults]
# Inherited by every site, and overridable inside any [[sites]] block.

# The service account's JSON key: Google Cloud Console -> IAM -> Service
# accounts -> Keys -> Add key -> JSON. Relative to this file, so the key stays
# with the config that uses it. Nothing is saved there yet.
credentials = "secrets/ga4-service-account.json"

# The events that count as conversions, spelled exactly as GA4 reports them.
# Required, and the two below are a guess at yours: there is no sensible
# default, and an empty list would show a conversion count of zero forever
# without ever saying that nothing was configured.
conversions = ["purchase", "sign_up"]

# Seconds between polls, and minutes of realtime history read on each one.
# The window is deliberately wider than one interval, so late-arriving events
# and skipped cycles are re-read and corrected rather than lost. The Realtime
# API reaches back 30 minutes and no further.
interval = 300
poll_window = 10

# The zone every day boundary is computed in, as an IANA name such as
# "America/Sao_Paulo". Empty means "ask the Admin API for the property's own
# timezone", which is almost always right -- and is a different thing from
# "UTC", which is where the tool falls back if the Admin API refuses. It says
# so when it does.
timezone = ""


[ui]
# Global, because the dashboard shows one site at a time.

refresh    = 10   # seconds between redraws; unrelated to `interval` above
window     = 6    # hours of history on the chart's x axis, 1 to 24
top_events = 10   # rows in the events table before it is cropped


[storage]

# Commented out on purpose, and this is the line to read before moving this
# file. Unset, the database goes to the platform data directory:
#
#   %LOCALAPPDATA%\ga4-realtime\                  Windows
#   ~/.local/share/ga4-realtime/                  Linux
#   ~/Library/Application Support/ga4-realtime/   macOS
#
# Uncomment it to keep the database beside this config instead, which is what
# you want when this file lives in a project directory. Either way, the
# rotating log sits next to the database.
# database = "out/ga4_realtime.db"


[[sites]]
# One block per GA4 property. Copy it for a second site.

# The slug this site is stored and addressed by, and the word you type after
# --site: lowercase letters, digits, underscore and hyphen. Treat it as
# immutable -- renaming it starts a new history, and the old one stays
# readable under the old name.
name = "mysite"

# The NUMERIC property ID -- GA4 -> Admin -> Property settings -> Property ID.
# NOT the G-XXXXXXXXXX measurement ID that gtag.js uses in the browser: that
# one produces a PermissionDenied from the API that never mentions the real
# cause, which is why this tool rejects it by name before making any call.
property_id = ""

# What the header calls this site. Optional: without it the tool uses the
# display name the Admin API reports.
# label = "mysite.example"
"""

ENV_TEMPLATE = """# ga4-realtime -- secrets, and nothing else
#
# Written by `ga4-realtime init`. Copy this file to `.env`, in this same
# directory, beside the config it belongs to. Settings do not go here: this
# file exists for one job.
#
# Any string value in the config may be written as ${VAR}, and the value is
# read from here or from the environment:
#
#   [[sites]]
#   name        = "mysite"
#   property_id = "${GA4_PROPERTY_ID_MYSITE}"
#   credentials = "${GA4_CREDENTIALS}"
#
# That is what keeps a property ID, or a key path, out of a file you might
# paste into an issue. The names are yours to choose; the two below are only a
# suggestion. An unset or empty variable is an error naming both the variable
# and the config key that wanted it -- it is never quietly substituted with an
# empty string. The real environment wins over this file, so
# `GA4_CREDENTIALS=... ga4-realtime doctor` is a one-off override that edits
# nothing.
#
# Keep `.env` out of version control. This template is safe to commit.

GA4_CREDENTIALS=secrets/ga4-service-account.json
GA4_PROPERTY_ID_MYSITE=
"""

# Printed after both files land. The command has to say which two fields
# still need the user: without this they meet a config that refuses to load
# and no hint that refusing was the plan.
NEXT_STEPS = """
Next, fill in the two fields that cannot be guessed:
  property_id   the NUMERIC id -- GA4 -> Admin -> Property settings
  credentials   where you saved the service account's JSON key
Then check both, without polling anything:
  ga4-realtime doctor"""


def run(ctx: Context) -> int:
    target = _target(ctx)
    env_example = target.parent / ENV_EXAMPLE_FILENAME

    # Both, before either is written. See the module docstring.
    for existing in (target, env_example):
        if existing.exists():
            _refuse(existing)

    _write(target, CONFIG_TEMPLATE)
    _write(env_example, ENV_TEMPLATE)

    print(f"wrote {target}")
    print(f"wrote {env_example}")
    print(NEXT_STEPS)
    return 0


def _target(ctx: Context) -> Path:
    """The config file to write: `--path`, or one in the working directory.

    `ctx.resolve` for both, because both are the command line talking: the
    flag is a flag, and the default is the directory the user ran the command
    in. Neither resolves against a config file's directory -- there is no
    config file yet, which is the point of this command.
    """
    wanted = ctx.args.path
    target = ctx.resolve(
        paths.LOCAL_CONFIG_FILENAME if wanted is None else wanted
    )
    if target.is_dir():
        # Checked before the overwrite refusal so the message names the real
        # mistake. Left to write_text this is an OSError whose wording says
        # nothing about which argument was wrong.
        raise ConfigError(
            f"{target} is a directory.\n"
            "--path names the config file to write, not the directory to "
            "write it in:\n"
            "  ga4-realtime init --path "
            f"{target / paths.LOCAL_CONFIG_FILENAME}"
        )
    return target


def _refuse(existing: Path) -> None:
    """Stop, naming the file that would have been destroyed.

    A `ConfigError`, so `cli.main` prints it alone and exits 1. Never a
    prompt: `init` has to behave the same in a script and at a terminal, and
    the safe answer to "overwrite the file holding every site's property ID
    and key path?" is the only one worth having.
    """
    raise ConfigError(
        f"{existing} already exists, and init never overwrites.\n"
        "That file may hold the property IDs, key paths and site names of "
        "everything you have set up.\n"
        "Move it aside, or write the starter pair somewhere else:\n"
        "  ga4-realtime init --path PATH"
    )


def _write(path: Path, text: str) -> None:
    """Create the parent directory and write one file, or say why not.

    `--path ~/.config/ga4-realtime/config.toml` is a documented invocation,
    and on a machine that has never run this tool that directory does not
    exist yet. Creating it here is the difference between one command and a
    mkdir the user has to work out for themselves.

    A failure part-way through leaves the config written and `.env.example`
    not, which is untidy and correct: the alternative is deleting a file after
    the filesystem has already said something is wrong with the directory.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise ConfigError(
            f"cannot write {path}: {exc.strerror or exc}."
        ) from None
