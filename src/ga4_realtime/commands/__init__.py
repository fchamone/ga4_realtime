"""The contract every command module is written against.

`cli.py` owns the parser and nothing else. It builds one `Context`, looks up
the module the subcommand names, and calls `run` on it. Everything a command
needs to reach the config, the database and the log is a method on that
context, so no command re-implements discovery and no two commands disagree
about where a file is.

**The contract, in full.**

Each of `live`, `report`, `init`, `doctor` and `sites` exposes exactly one
callable::

    def run(ctx: Context) -> int: ...

- It receives the `Context` below and nothing else. The parsed namespace is
  on `ctx.args`, subcommand options included (`--date` for `report`, `--path`
  for `init`).
- It returns the process exit code: `0` for success, and `1` where a command
  finds a problem rather than raising one -- `doctor` is the case the spec
  names.
- It raises `ConfigError` for anything the user can fix. `cli.main` prints
  that on stderr without a traceback and exits 1, so a command never prints
  its own fatal error and never calls `sys.exit`.
- It is the only layer that prints to *stdout*. Library modules log; stdout
  belongs to the Live region, and the one line `cli.main` prints itself --
  the fatal `ConfigError` -- goes to stderr.

**What the context carries.**

- `ctx.args` -- the parsed namespace, subcommand options included.
- `ctx.overrides` -- the D16 flags, already bounds-checked by argparse.
- `ctx.cwd` -- the directory a path typed on the command line resolves
  against.
- `ctx.config()` -- the loaded, merged, validated `Config`.
- `ctx.database()` -- the SQLite path, resolved but never opened.
- `ctx.log_path()` -- the rotating log, always beside the database.
- `ctx.start_logging()` -- attach the file handler and the ring buffer.
- `ctx.resolve(value)` -- one path *from a flag*, against `ctx.cwd`.
- `ctx.focus()` -- the configured, enabled site the dashboard opens on.

`config()`, `database()` and the ring buffer are resolved lazily and cached;
`log_path()` and `resolve()` are derived on each call and cheap. That laziness
is a requirement, not an optimisation: `report` and `sites` must work from
`--db` alone, with no config file, no credentials and no network, so a
context that loaded the config eagerly would break them on a machine that has
never run `init`.

`--site` is spelled `ALL_SITES` when the user means every site, and what that
means is the command's decision: every section for `report` and `sites`, and
"same as omitted" for the dashboard, which shows one site at a time (D4).

Nothing here opens the database. `RealtimeStore` *creates* the file it is
pointed at, and `doctor` has to report the resolved path without bringing one
into existence -- so `database()` returns a path and the command decides
whether opening it is allowed.

Nothing here configures logging either. `start_logging()` exists but is
opt-in: the dashboard needs it for the `--verbose` panel's ring buffer, while
a read-only `report` writing a log file would be a side effect nobody asked
for.
"""

import argparse
from dataclasses import dataclass, field
from pathlib import Path

from .. import paths
from ..config import Config, Overrides, SiteConfig, load_config
from ..errors import ConfigError
from ..logging_setup import RingBufferHandler, setup_logging

# What `--site` is spelled when the user means every site. A constant rather
# than the literal in five places: the word means something slightly
# different per command, and its spelling is the one part that must not.
ALL_SITES = "all"


@dataclass
class Context:
    """Everything a command is handed, and the lazy work behind it.

    `cwd` is a field rather than a `Path.cwd()` call inside the methods, for
    the reason `paths` and `config` both give: Windows Task Scheduler runs
    with the working directory at ``C:\\Windows\\System32`` and a systemd unit
    runs at ``/``. Passing it in keeps the one place that reads the real
    working directory -- `cli.main` -- visible, and lets a test point a
    command at a directory without chdir'ing the whole process.
    """

    args: argparse.Namespace
    overrides: Overrides
    cwd: Path

    # Caches, not inputs. Two commands, or one command twice, must not read
    # two different config files in one run.
    _config: Config | None = field(default=None, init=False, repr=False)
    _database: Path | None = field(default=None, init=False, repr=False)
    _ring: RingBufferHandler | None = field(
        default=None, init=False, repr=False
    )

    # -- the file the user edits -------------------------------------------

    def config(self) -> Config:
        """Discover, read, merge and validate the config file.

        Every command-line flag is already on `overrides`, so this is where
        D16 takes effect: the flag beats `[defaults]` and every per-site
        value, for every site.
        """
        if self._config is None:
            self._config = load_config(
                self.args.config, cwd=self.cwd, overrides=self.overrides
            )
        return self._config

    # -- the files the tool writes -----------------------------------------

    def database(self) -> Path:
        """Where the SQLite file is, without opening or creating it.

        A `--db` typed at a shell resolves against that shell's working
        directory, because that is what the person typing it meant --
        `[storage] database` inside the config file is the one that resolves
        against the config file's own directory. Reaching this through the
        flag consults no config file at all, which is what makes `report`
        and `sites` usable with `--db` alone.
        """
        if self._database is None:
            if self.args.db is not None:
                self._database = self.resolve(self.args.db)
            else:
                self._database = self.config().database
        return self._database

    def log_path(self) -> Path:
        """The rotating log, which always lives beside the database."""
        return paths.log_path_for(self.database())

    def start_logging(self) -> RingBufferHandler:
        """Attach the file handler and the ring buffer, once.

        Opt-in, and called by the commands that log rather than by
        `cli.main`, because the log path is only known after the database has
        been resolved -- and resolving the database is exactly what `report`
        and `sites` are allowed to do without a config file.
        """
        if self._ring is None:
            self._ring = setup_logging(self.args.verbose, self.log_path())
        return self._ring

    def resolve(self, value: str | Path) -> Path:
        """One relative path from the command line, against `cwd`.

        The rule for *flags*, and only for flags. Paths that came out of the
        config file resolve against the config file's directory and have
        already been resolved by the time `Config` exists.
        """
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.cwd / path
        return path.resolve()

    # -- which site is on screen -------------------------------------------

    def focus(self) -> SiteConfig:
        """The configured, enabled site the dashboard opens on.

        `--site all` is the same as omitting it here: the dashboard shows one
        site at a time (D4), so there is no all-sites frame for `all` to
        mean. `report` and `sites` read the word differently, which is why
        this method belongs to the dashboard's path rather than to every
        command -- and why those two accept any name in `site_meta`, orphans
        included, while this one accepts only names in the file.
        """
        config = self.config()
        wanted = self.args.site
        if wanted is None or wanted == ALL_SITES:
            # Safe to index: `load_config` refuses a file with no enabled
            # site, naming that state rather than reporting it as an empty
            # dashboard. Order is the file's, so moving a block is how the
            # user chooses which site opens first.
            return config.enabled_sites()[0]

        # Raises naming every spelling that would have worked, which is the
        # cheapest possible fix for a typo.
        site = config.site(wanted)
        if not site.enabled:
            # Distinct from "no such site", and worth its own message: the
            # name is in the file, so a listing of valid names would send the
            # user looking at the one line that is already right.
            raise ConfigError(
                f"site {site.name!r} in {config.source} has enabled = "
                "false, so it is never polled and the dashboard has nothing "
                "to show for it.\n"
                "Set enabled = true on that [[sites]] block, or read what "
                "was already collected with\n"
                f"  ga4-realtime report --site {site.name}"
            )
        return site
