"""What `init` writes, and what it refuses to destroy.

Three things are pinned here, and only three.

**The starter config does not lie.** Every key it writes is a key the loader
reads, the `[storage] database` line is commented out rather than claiming a
repo-relative path that a config in the platform config directory would not
have, and the two fields only the user can supply are written to fail the
load -- `property_id` visibly empty, `credentials` pointing at a path where
nothing is saved yet -- so `doctor` can name them. The load test fills
exactly those two -- a numeric property ID and a key file that exists -- and
nothing else, because anything else it had to fill would be a field `init`
should have written correctly.

**Both files, or neither.** The refusal is checked before either file is
written: writing the config and then refusing over `.env.example` would leave
the directory in a state where the next run refuses over the config the
previous run had just created.

**Refusing is exit 1 with the path in it.** A `ConfigError`, so `cli.main`
prints it without a traceback; the assertion is on the path it names, because
"a file already exists" without saying which one is not a message anyone can
act on.
"""

import logging
import tomllib
from pathlib import Path

import pytest

from ga4_realtime import cli, paths
from ga4_realtime.commands import Context, init
from ga4_realtime.config import (
    DEFAULT_INTERVAL,
    DEFAULT_POLL_WINDOW,
    DEFAULT_REFRESH,
    DEFAULT_WINDOW_HOURS,
    load_config,
)
from ga4_realtime.errors import ConfigError

# The two files, by the names the spec promises. Spelled out rather than
# imported from the module under test: these names are the user-facing
# contract, and a test that read them back from the implementation would keep
# passing if the implementation renamed them.
CONFIG_NAME = "ga4-realtime.toml"
ENV_EXAMPLE_NAME = ".env.example"

# The key path the starter config points at, and the numeric property ID a
# user would type in. The only two edits between "just written" and "loads".
KEY_RELATIVE = "secrets/ga4-service-account.json"
PROPERTY_ID = "123456789"


def make_context(cwd: Path, *argv: str) -> Context:
    """The `Context` `cli.main` would build for `init` running in `cwd`.

    Built through the real parser rather than by hand, so a test that passes
    here is a test of the arguments a user can actually type.
    """
    args = cli.build_parser().parse_args(["init", *argv])
    return Context(args=args, overrides=cli.overrides_from(args), cwd=cwd)


def fill_in(written: Path, key: Path) -> None:
    """Do the two edits `init` leaves to the user, and prove both happened.

    The key file is created where the config already points, rather than the
    config being pointed somewhere new: a starter config whose `credentials`
    line needed rewriting to work would be one more field `doctor` has to
    explain.
    """
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text("{}", encoding="utf-8")

    text = written.read_text(encoding="utf-8")
    filled = text.replace('property_id = ""', f'property_id = "{PROPERTY_ID}"')
    assert filled != text, "property_id is not written as an empty string"
    written.write_text(filled, encoding="utf-8")


@pytest.fixture
def project(tmp_path) -> Path:
    """An empty directory, which is where `init` is meant to be run.

    Deliberately not `tmp_path` itself: `decoy_cwd` lives there too, and a
    test cannot tell "written beside ctx.cwd" from "written in the process's
    working directory" when the two are the same directory.
    """
    home = tmp_path / "project"
    home.mkdir()
    return home


@pytest.fixture
def decoy_cwd(tmp_path, monkeypatch) -> Path:
    """A working directory nothing should ever be written into.

    The process is chdir'd into it for every test in this module. `Context`
    carries the directory a path resolves against; the chdir is what would
    catch a lapse back to `Path.cwd()` or a bare relative open.
    """
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    monkeypatch.chdir(decoy)
    return decoy


@pytest.fixture
def home(tmp_path) -> Path:
    """A home directory for the platform-default database, as `paths` takes.

    Passed to `load_config` so the assertion about where an unset `[storage]
    database` lands is the same on all three platforms, instead of being an
    assertion about the machine the suite happens to run on.
    """
    return tmp_path / "home"


@pytest.fixture
def written(project, decoy_cwd) -> Path:
    """A freshly initialised directory, and the config file in it."""
    assert init.run(make_context(project)) == 0
    return project / CONFIG_NAME


# --------------------------------------------------------------------------
# What it writes
# --------------------------------------------------------------------------


def test_it_writes_both_files_beside_each_other(
    project: Path, decoy_cwd: Path
) -> None:
    assert init.run(make_context(project)) == 0

    assert (project / CONFIG_NAME).is_file()
    assert (project / ENV_EXAMPLE_NAME).is_file()
    # Nothing in the directory the process is actually sitting in: the target
    # comes from the context, not from the working directory.
    assert list(decoy_cwd.iterdir()) == []


def test_it_names_both_files_and_the_next_command(
    project: Path, decoy_cwd: Path, capsys
) -> None:
    assert init.run(make_context(project)) == 0

    out = capsys.readouterr().out
    assert str(project / CONFIG_NAME) in out
    assert str(project / ENV_EXAMPLE_NAME) in out
    # The two fields only the user can supply, and the command that checks
    # them. Without this the user is left with a file that fails to load and
    # no idea that failing was the plan.
    assert "property_id" in out
    assert "credentials" in out
    assert "doctor" in out


def test_the_path_flag_says_where_and_creates_the_directory(
    project: Path, decoy_cwd: Path
) -> None:
    # `--path ~/.config/ga4-realtime/config.toml` is documented, and on a
    # machine that has never run this tool that directory does not exist yet.
    target = project / "elsewhere" / paths.CONFIG_FILENAME

    assert init.run(make_context(project, "--path", str(target))) == 0

    assert target.is_file()
    # Beside the config, whatever the config is called: `.env` is read from
    # the config file's own directory.
    assert (target.parent / ENV_EXAMPLE_NAME).is_file()
    assert not (project / CONFIG_NAME).exists()


def test_the_env_example_is_about_interpolation_and_nothing_else(
    written: Path,
) -> None:
    text = (written.parent / ENV_EXAMPLE_NAME).read_text(encoding="utf-8")

    assert "${" in text
    # The two variables that were process-global and are gone. Naming either
    # as something to set would send the user back to a mechanism that cannot
    # hold two sites' credentials at once.
    assert "GOOGLE_APPLICATION_CREDENTIALS=" not in text
    assert "\nGA4_PROPERTY_ID=" not in text


# --------------------------------------------------------------------------
# The starter config does not lie
# --------------------------------------------------------------------------


def test_the_written_config_loads_once_the_two_fields_are_filled(
    written: Path, project: Path, decoy_cwd: Path, home: Path
) -> None:
    fill_in(written, project / KEY_RELATIVE)

    config = load_config(
        written, cwd=decoy_cwd, platform=paths.POSIX, env={}, home=home
    )

    assert config.names == ["mysite"]
    site = config.sites[0]
    assert site.property_id == PROPERTY_ID
    assert site.credentials == (project / KEY_RELATIVE).resolve()
    assert site.enabled is True
    # Empty in the file means "ask the Admin API for the property's own
    # timezone", which is a different thing from UTC and has to survive the
    # round trip as None rather than as "".
    assert site.timezone is None
    assert site.conversions
    assert site.interval == DEFAULT_INTERVAL
    assert site.poll_window == DEFAULT_POLL_WINDOW
    assert config.ui.refresh == DEFAULT_REFRESH
    assert config.ui.window == DEFAULT_WINDOW_HOURS
    # env={} on purpose: a starter config that needed a variable set before it
    # would load would fail on the machine it was just written on.


def test_it_needs_no_key_the_loader_does_not_read(
    written: Path, project: Path, decoy_cwd: Path, home: Path, caplog
) -> None:
    """Every key it writes is a key `config.py` reads.

    An unknown key is a warning rather than an error, so a starter config full
    of misspelled keys would load perfectly and do nothing -- which is the
    exact failure the warning exists to catch.
    """
    fill_in(written, project / KEY_RELATIVE)

    with caplog.at_level(logging.WARNING):
        load_config(
            written, cwd=decoy_cwd, platform=paths.POSIX, env={}, home=home
        )

    assert [record.getMessage() for record in caplog.records] == []


def test_the_database_line_is_written_commented_out(
    written: Path, project: Path, decoy_cwd: Path, home: Path
) -> None:
    """So a config in the user config dir claims no repo-relative path.

    Both halves matter: the line has to be *there*, commented, or the user
    never learns the key exists; and it has to be commented, or a config
    written into the platform config directory silently points its database at
    an `out/` beside itself that nobody created.
    """
    text = written.read_text(encoding="utf-8")
    assert "# database = " in text
    assert "database" not in tomllib.loads(text).get("storage", {})

    fill_in(written, project / KEY_RELATIVE)
    config = load_config(
        written, cwd=decoy_cwd, platform=paths.POSIX, env={}, home=home
    )

    assert config.database == paths.default_database_path(
        platform=paths.POSIX, env={}, home=home
    )
    assert config.log_path == config.database.with_suffix(".log")


def test_the_unfilled_config_fails_naming_property_id(
    written: Path, decoy_cwd: Path, home: Path
) -> None:
    # The state `doctor` is written against: loaded as written, the file is
    # invalid, and it is invalid in a way that names the field to fill in
    # rather than one that reads like a bug in the tool.
    with pytest.raises(ConfigError, match="property_id"):
        load_config(
            written, cwd=decoy_cwd, platform=paths.POSIX, env={}, home=home
        )


def test_it_does_not_hardcode_one_property_s_conversion_event(
    written: Path,
) -> None:
    # `generate_lead` is the author's own lead event, which no other property
    # fires. The starter config names events the user is expected to replace,
    # and says so; it must never ship that constant.
    assert "generate_lead" not in written.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# It never overwrites
# --------------------------------------------------------------------------


def test_a_second_run_refuses_and_names_the_config(
    written: Path, project: Path, decoy_cwd: Path
) -> None:
    with pytest.raises(ConfigError) as caught:
        init.run(make_context(project))

    assert str(written) in str(caught.value)


def test_a_second_run_changes_nothing_it_would_have_overwritten(
    written: Path, project: Path, decoy_cwd: Path
) -> None:
    # The whole point of the refusal: that file may hold every site's property
    # ID and key path, and a starter config on top of it is unrecoverable.
    written.write_text("# mine, edited\n", encoding="utf-8")
    (project / ENV_EXAMPLE_NAME).write_text("MINE=1\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        init.run(make_context(project))

    assert written.read_text(encoding="utf-8") == "# mine, edited\n"
    assert (project / ENV_EXAMPLE_NAME).read_text(
        encoding="utf-8"
    ) == "MINE=1\n"


def test_an_existing_env_example_stops_the_config_being_written(
    project: Path, decoy_cwd: Path
) -> None:
    """Both files are checked before either is written.

    Writing the config first and refusing on `.env.example` afterwards would
    leave a directory where the next run refuses over the config the previous
    run had just created -- and the user never gets either file.
    """
    (project / ENV_EXAMPLE_NAME).write_text("MINE=1\n", encoding="utf-8")

    with pytest.raises(ConfigError) as caught:
        init.run(make_context(project))

    assert str(project / ENV_EXAMPLE_NAME) in str(caught.value)
    assert not (project / CONFIG_NAME).exists()


def test_a_directory_path_is_refused_naming_the_file_to_write(
    project: Path, decoy_cwd: Path
) -> None:
    # --path names the file, not the directory to put it in. Left to
    # write_text this is an OSError whose message says nothing about which
    # argument was wrong.
    target = project / "config-dir"
    target.mkdir()

    with pytest.raises(ConfigError) as caught:
        init.run(make_context(project, "--path", str(target)))

    message = str(caught.value)
    assert str(target) in message
    assert paths.LOCAL_CONFIG_FILENAME in message


# --------------------------------------------------------------------------
# Through the CLI -- exit 0, then exit 1
# --------------------------------------------------------------------------


def test_a_first_run_exits_zero(project: Path, monkeypatch) -> None:
    monkeypatch.chdir(project)

    assert cli.main(["init"]) == 0
    assert (project / CONFIG_NAME).is_file()


def test_a_second_run_exits_one_without_a_traceback(
    project: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(project)
    assert cli.main(["init"]) == 0
    capsys.readouterr()

    assert cli.main(["init"]) == 1

    error = capsys.readouterr().err
    assert str(project / CONFIG_NAME) in error
    assert "Traceback" not in error
