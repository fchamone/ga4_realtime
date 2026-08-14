"""The argument surface, the dispatch contract, and the four exit codes.

Three things are being pinned here, and only three.

**The parser.** Every global flag is declared before `add_subparsers()`, so
one spelling works on the bare invocation -- which is the dashboard -- and on
every subcommand. The bounds argparse enforces (`--interval`, `--refresh`,
`--window`) exit 2 the way argparse always has, and their messages name the
bound rather than only rejecting the value.

**The dispatch.** `cli.main` looks `run` up on the command *module* at call
time, which is what lets these tests replace it. They deliberately do not
assert what the stubs currently do: the five modules under `commands/` are
filled in immediately after this task, and a test pinning `NotImplementedError`
would have to be deleted the moment it started being useful. What is pinned is
the contract those modules are written against -- one `run(ctx) -> int`, one
`Context`, and the resolved objects it hands over.

**The exit codes.** 0, 1, 2 and 130, from §4 of the spec. `main` returns them
rather than raising, argparse's own `SystemExit` included, so the table is
literally true of one function.
"""

import inspect
import logging
from pathlib import Path

import pytest

from ga4_realtime import __version__, cli, commands, logging_setup
from ga4_realtime.commands import Context, doctor, init, live, report, sites
from ga4_realtime.config import Overrides
from ga4_realtime.errors import ConfigError

# The subcommand word, or None for the bare invocation, against the module
# that must answer for it. The bare invocation is the dashboard: that mapping
# is the reason the global flags are declared where they are.
COMMAND_MODULES = {
    None: live,
    "report": report,
    "init": init,
    "doctor": doctor,
    "sites": sites,
}

# A config with one site polled and one paused, for the focus rules. The
# two-site `tmp_config` fixture has both enabled, so it cannot tell the
# "not in the file" error from the "in the file but switched off" one.
PAUSED_SITE_CONFIG = """
    [defaults]
    credentials = "secrets/ga4-service-account.json"
    conversions = ["purchase"]

    [[sites]]
    name        = "mysite"
    property_id = "123456789"

    [[sites]]
    name        = "paused"
    property_id = "987654321"
    enabled     = false
"""


class Dispatched:
    """Which command module ran, with which `Context`, and what it returned.

    One recorder standing in for all five modules rather than a mock each,
    because almost every assertion below is about the *context* that arrived
    and not about the call itself. `code` and `error` are set by the test that
    cares what the command does with it.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, Context]] = []
        self.code = 0
        self.error: BaseException | None = None

    def stub(self, name: str):
        def _run(ctx: Context) -> int:
            self.calls.append((name, ctx))
            if self.error is not None:
                raise self.error
            return self.code

        return _run

    @property
    def name(self) -> str:
        assert len(self.calls) == 1, self.calls
        return self.calls[0][0]

    @property
    def ctx(self) -> Context:
        assert len(self.calls) == 1, self.calls
        return self.calls[0][1]


@pytest.fixture
def dispatched(monkeypatch) -> Dispatched:
    """Replace every command's `run` with a recorder.

    Patched on the module rather than passed in, because that is exactly how
    `cli` reaches it: the dispatch table holds modules and reads `.run` at
    call time, so a command that has not been written yet is still reachable
    and a command that has been rewritten needs no change here.
    """
    recorder = Dispatched()
    for name, module in COMMAND_MODULES.items():
        monkeypatch.setattr(module, "run", recorder.stub(name or "live"))
    return recorder


@pytest.fixture
def restore_logger():
    """Leave the package logger exactly as it was found.

    The logger is process-wide and `start_logging` attaches a rotating file
    handler to it. Left in place, that handler writes the rest of the suite's
    lines into this test's tmp_path -- and on Windows the open handle fails
    the directory's cleanup rather than the assertion.
    """
    log = logging_setup.log
    before = list(log.handlers)
    yield
    for handler in list(log.handlers):
        log.removeHandler(handler)
        if handler not in before:
            handler.close()
    for handler in before:
        log.addHandler(handler)


@pytest.fixture
def paused_config(write_config) -> Path:
    """A config whose second site is switched off."""
    return write_config(PAUSED_SITE_CONFIG)


def _run(*argv: str) -> int:
    return cli.main(list(argv))


def _option_help(parser, flag: str) -> str:
    for action in parser._actions:
        if flag in action.option_strings:
            return action.help or ""
    raise AssertionError(f"{flag} is not declared on the parser")


# --------------------------------------------------------------------------
# --version and --help
# --------------------------------------------------------------------------


def test_version_prints_the_version_and_exits_zero(capsys) -> None:
    assert _run("--version") == 0
    assert capsys.readouterr().out.strip() == __version__


def test_every_global_flag_is_on_the_top_level_parser() -> None:
    """Declared before add_subparsers(), which is what makes them global.

    The structural half of the two tests below it: those prove the flags
    *work* on the bare invocation and before a subcommand, this one proves
    none of them has quietly moved onto a subparser, where the dashboard --
    which has no subcommand word -- could never reach it.
    """
    declared = {
        option
        for action in cli.build_parser()._actions
        for option in action.option_strings
    }

    assert {
        "--config",
        "--site",
        "--db",
        "--interval",
        "--refresh",
        "--window",
        "--timezone",
        "--ascii",
        "--verbose",
        "--version",
    } <= declared


def test_help_lists_every_subcommand() -> None:
    text = cli.build_parser().format_help()

    for command in ("init", "doctor", "sites", "report"):
        assert command in text


def test_help_states_the_flag_precedence_rule() -> None:
    # D16 in one line, verbatim: a flag beats [defaults] and every per-site
    # value. It is asserted as one string because the whole point of the rule
    # is that there are no exceptions to enumerate.
    text = cli.build_parser().format_help()

    assert "overrides [defaults] and every per-site value" in text


def test_help_calls_timezone_a_debugging_override() -> None:
    # Read off the action rather than out of format_help(): argparse rewraps
    # option help to the terminal width, so the assertion would otherwise
    # depend on which column the test runner happened to break the line at.
    help_text = _option_help(cli.build_parser(), "--timezone")

    assert "debugging" in help_text
    assert "timezone" in help_text


def test_help_exits_zero() -> None:
    assert cli.main(["--help"]) == 0


# --------------------------------------------------------------------------
# Argument validation -- exit 2, and the message names the bound
# --------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["--interval", "--refresh"])
def test_below_one_exits_two_naming_the_bound(capsys, flag: str) -> None:
    assert _run(flag, "0") == 2

    error = capsys.readouterr().err
    assert flag in error
    assert "at least 1" in error


@pytest.mark.parametrize("value", ["0", "25"])
def test_window_outside_the_range_exits_two(capsys, value: str) -> None:
    assert _run("--window", value) == 2

    error = capsys.readouterr().err
    assert "--window" in error
    assert "between 1 and 24" in error


def test_a_non_numeric_bound_exits_two(capsys) -> None:
    assert _run("--interval", "soon") == 2
    assert "whole number" in capsys.readouterr().err


def test_an_unknown_subcommand_exits_two() -> None:
    assert _run("export") == 2


def test_a_valid_window_is_accepted(dispatched, tmp_config) -> None:
    assert _run("--config", str(tmp_config), "--window", "24") == 0
    assert dispatched.ctx.overrides.window == 24


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


def test_the_bare_invocation_is_the_dashboard(
    dispatched, tmp_config: Path
) -> None:
    assert _run("--config", str(tmp_config)) == 0
    assert dispatched.name == "live"


@pytest.mark.parametrize("command", ["report", "init", "doctor", "sites"])
def test_each_subcommand_reaches_its_own_module(
    dispatched, command: str
) -> None:
    assert _run(command) == 0
    assert dispatched.name == command


def test_the_commands_return_value_is_the_exit_code(dispatched) -> None:
    dispatched.code = 7

    assert _run("sites") == 7


def test_global_flags_apply_to_the_bare_invocation(
    dispatched, tmp_config: Path
) -> None:
    # The reason every global flag is declared before add_subparsers(): the
    # dashboard has no subcommand word to hang them off.
    assert _run("--config", str(tmp_config), "--verbose", "--ascii") == 0

    args = dispatched.ctx.args
    assert args.command is None
    assert args.verbose is True
    assert args.ascii is True


def test_global_flags_apply_before_a_subcommand(dispatched) -> None:
    assert _run("--interval", "60", "--verbose", "doctor") == 0

    args = dispatched.ctx.args
    assert args.command == "doctor"
    assert args.interval == 60
    assert args.verbose is True


@pytest.mark.parametrize("command", ["report", "init", "doctor", "sites"])
def test_global_flags_apply_after_a_subcommand(
    dispatched, command: str
) -> None:
    # The order every user reaches for first, and the order three of the
    # tool's own messages print. Every subcommand re-registers the global
    # flags, so it works on all four rather than on the two that happen to
    # have a message pointing at them.
    assert _run(command, "--interval", "60", "--verbose") == 0

    args = dispatched.ctx.args
    assert args.command == command
    assert args.interval == 60
    assert args.verbose is True


def test_a_subcommand_does_not_reset_a_flag_typed_before_it(
    dispatched,
) -> None:
    """The reason the re-registered copies default to `argparse.SUPPRESS`.

    A subparser parses into a *fresh* namespace and then copies every key
    that namespace holds onto the one the top level already filled in, so a
    re-registered `--site` defaulting to None would wipe this line's
    `clientb` back to None on its way past -- silently, and only for the
    word order that used to be the one that worked.
    """
    assert _run("--site", "clientb", "--ascii", "report") == 0

    args = dispatched.ctx.args
    assert args.site == "clientb"
    assert args.ascii is True


def test_a_flag_typed_on_both_sides_resolves_to_the_last_one(
    dispatched,
) -> None:
    # Nearest the end of the line wins, which is what a shell user expects
    # of a repeated flag and what argparse does everywhere else.
    assert _run("--site", "clienta", "report", "--site", "clientb") == 0

    assert dispatched.ctx.args.site == "clientb"


def test_a_subcommand_alone_leaves_every_global_flag_at_its_default(
    dispatched,
) -> None:
    # The other half of SUPPRESS: with nothing typed, the top level's
    # defaults have to survive the subparser rather than be replaced by a
    # second set of them.
    assert _run("sites") == 0

    args = dispatched.ctx.args
    assert args.site is None
    assert args.db is None
    assert args.config is None
    assert args.interval is None
    assert args.refresh is None
    assert args.window is None
    assert args.timezone is None
    assert args.ascii is False
    assert args.verbose is False


def test_the_command_the_tool_prints_is_a_command_that_parses() -> None:
    """`report --site NAME` is printed at three places when a site is idle.

    `commands/__init__.py`, `commands/sites.py` and `commands/doctor.py`
    each tell the user to type it. Until the flags were registered on the
    subparsers too it exited 2 with `unrecognized arguments`, which is the
    worst possible moment to be wrong: the message is already an apology
    for something else.
    """
    args = cli.build_parser().parse_args(["report", "--site", "clientb"])

    assert args.command == "report"
    assert args.site == "clientb"


def test_report_takes_a_date_argparse_does_not_validate(dispatched) -> None:
    # --date is a plain string on purpose. A bad date is a ConfigError from
    # the command -- exit 1, with a message saying what the format is -- and
    # an argparse type would make it exit 2 with argparse's own wording.
    assert _run("report", "--date", "yesterday") == 0
    assert dispatched.ctx.args.date == "yesterday"


def test_init_takes_a_path(dispatched, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere" / "ga4-realtime.toml"

    assert _run("init", "--path", str(target)) == 0
    assert dispatched.ctx.args.path == target


# --------------------------------------------------------------------------
# Exit codes 1 and 130
# --------------------------------------------------------------------------


def test_a_config_error_prints_without_a_traceback(dispatched, capsys) -> None:
    dispatched.error = ConfigError("the key file is missing")

    assert _run("doctor") == 1

    captured = capsys.readouterr()
    assert "the key file is missing" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_a_keyboard_interrupt_exits_130(dispatched) -> None:
    dispatched.error = KeyboardInterrupt()

    assert _run("sites") == 130


def test_an_unknown_site_exits_one_naming_the_sites_that_exist(
    dispatched, capsys, tmp_config: Path
) -> None:
    # Resolved before the command runs, so a typo fails before a poller
    # thread or a Live region exists. The listing is the whole value of the
    # error: the cheapest fix for a typo is being shown the right spelling.
    assert _run("--config", str(tmp_config), "--site", "nope") == 1

    error = capsys.readouterr().err
    assert "nope" in error
    assert "mysite" in error
    assert "clientb" in error
    assert dispatched.calls == []


# --------------------------------------------------------------------------
# The Context every command is handed
# --------------------------------------------------------------------------


def test_no_flags_means_no_overrides(dispatched) -> None:
    assert _run("sites") == 0
    assert dispatched.ctx.overrides == Overrides()


def test_the_flags_arrive_as_overrides(
    dispatched, tmp_config: Path, tmp_path: Path
) -> None:
    database = tmp_path / "other.db"

    assert (
        _run(
            "--config",
            str(tmp_config),
            "--db",
            str(database),
            "--interval",
            "60",
            "--refresh",
            "5",
            "--window",
            "12",
            "--timezone",
            "Asia/Tokyo",
            "--ascii",
            "sites",
        )
        == 0
    )

    assert dispatched.ctx.overrides == Overrides(
        database=database,
        interval=60,
        refresh=5,
        window=12,
        timezone="Asia/Tokyo",
        # True, never False: --ascii is a store_true, so an untyped flag and
        # a flag explicitly turned off are the same thing to argparse.
        ascii=True,
    )


def test_the_db_flag_resolves_against_the_working_directory(
    dispatched, tmp_path: Path, monkeypatch
) -> None:
    # Against the shell's cwd, not the config file's directory: a --db typed
    # at a prompt means the directory the prompt is in.
    monkeypatch.chdir(tmp_path)

    assert _run("--db", "data/other.db", "sites") == 0
    assert dispatched.ctx.database() == (tmp_path / "data/other.db").resolve()


def test_a_db_flag_needs_no_config_file(
    dispatched, tmp_path: Path, monkeypatch
) -> None:
    """`report` and `sites` work from --db alone -- spec §6, D15."""
    monkeypatch.chdir(tmp_path)

    def _refuse(*args, **kwargs):
        pytest.fail("the database was resolved through the config file")

    monkeypatch.setattr(commands, "load_config", _refuse)

    assert _run("--db", str(tmp_path / "x.db"), "report") == 0
    assert dispatched.ctx.database() == (tmp_path / "x.db").resolve()


def test_without_a_db_flag_the_config_says_where(
    dispatched, tmp_config: Path
) -> None:
    assert _run("--config", str(tmp_config), "sites") == 0

    expected = (tmp_config.parent / "out" / "ga4_realtime.db").resolve()
    assert dispatched.ctx.database() == expected


def test_the_log_sits_beside_the_database(
    dispatched, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)

    assert _run("--db", str(tmp_path / "data" / "x.db"), "sites") == 0

    ctx = dispatched.ctx
    assert ctx.log_path() == ctx.database().with_suffix(".log")


def test_start_logging_opens_the_file_beside_the_database(
    dispatched, tmp_path: Path, monkeypatch, restore_logger
) -> None:
    monkeypatch.chdir(tmp_path)

    argv = ["--db", str(tmp_path / "data" / "x.db"), "--verbose", "sites"]
    assert cli.main(argv) == 0

    ctx = dispatched.ctx
    ring = ctx.start_logging()

    assert isinstance(ring, logging_setup.RingBufferHandler)
    assert ctx.log_path().exists()
    assert logging_setup.log.level == logging.DEBUG
    # Cached: a second call must not stack a second pair of handlers, which
    # would double every line the --verbose panel shows.
    assert ctx.start_logging() is ring


def test_the_context_config_applies_the_flags(
    dispatched, tmp_config: Path
) -> None:
    # D16, end to end: clientb's own block says interval = 120 and the flag
    # beats it anyway, for that site and for the one that inherited 300.
    assert _run("--config", str(tmp_config), "--interval", "60", "sites") == 0

    config = dispatched.ctx.config()
    assert [site.interval for site in config.sites] == [60, 60]
    # Cached, so two commands reading it never read two different files.
    assert dispatched.ctx.config() is config


def test_a_missing_config_is_a_config_error(
    dispatched, tmp_path: Path
) -> None:
    assert _run("--config", str(tmp_path / "nope.toml"), "sites") == 0

    with pytest.raises(ConfigError, match="does not exist"):
        dispatched.ctx.config()


# --------------------------------------------------------------------------
# focus() -- which site the dashboard opens on
# --------------------------------------------------------------------------


def test_focus_defaults_to_the_first_enabled_site(
    dispatched, tmp_config: Path
) -> None:
    assert _run("--config", str(tmp_config)) == 0
    assert dispatched.ctx.focus().name == "mysite"


def test_site_all_focuses_the_first_site_too(
    dispatched, tmp_config: Path
) -> None:
    # "all" is the same as omitted for the dashboard: one site is on screen
    # at a time (D4), so there is no all-sites frame to show.
    assert _run("--config", str(tmp_config), "--site", "all") == 0
    assert dispatched.ctx.focus().name == "mysite"


def test_focus_takes_the_site_that_was_asked_for(
    dispatched, tmp_config: Path
) -> None:
    assert _run("--config", str(tmp_config), "--site", "clientb") == 0
    assert dispatched.ctx.focus().name == "clientb"


def test_focus_skips_a_disabled_site_when_choosing_a_default(
    dispatched, paused_config: Path
) -> None:
    assert _run("--config", str(paused_config)) == 0
    assert dispatched.ctx.focus().name == "mysite"


def test_focusing_a_disabled_site_exits_one(
    dispatched, capsys, paused_config: Path
) -> None:
    assert _run("--config", str(paused_config), "--site", "paused") == 1

    error = capsys.readouterr().err
    assert "paused" in error
    assert "enabled" in error
    assert dispatched.calls == []


# --------------------------------------------------------------------------
# The stubs the four following tasks replace
# --------------------------------------------------------------------------


def test_every_command_module_exposes_run() -> None:
    """The dispatch contract, asserted on the modules themselves.

    Not on what they do -- these are stubs until the four command tasks land
    -- but on the shape `cli` calls: one module attribute named `run` taking
    one positional argument, the `Context`.
    """
    for module in COMMAND_MODULES.values():
        run = module.run
        assert callable(run)
        assert list(inspect.signature(run).parameters) == ["ctx"]
