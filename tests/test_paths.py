"""Platform config and data directories, all three from either machine.

The point of every test here is that it does not depend on the operating
system running it. `paths` takes the platform, the environment and the home
directory as arguments, so the Windows cases run on Linux and the XDG cases
run on Windows -- which matters because CI has two platforms and the module
claims three, and because the author's machine is only ever one of them.

Paths are compared as `Path` objects, never as strings: `Path("/home/x/y")`
is a WindowsPath on Windows and a PosixPath on Linux, and both join and
compare consistently with themselves. Comparing to a string would test the
host's separator instead of this module's answer.
"""

import ast
import os
import sys
from pathlib import Path

import pytest

from ga4_realtime import paths
from ga4_realtime.paths import (
    APP_NAME,
    CONFIG_FILENAME,
    DATABASE_FILENAME,
    LOCAL_CONFIG_FILENAME,
    MACOS,
    POSIX,
    WINDOWS,
    config_candidates,
    default_database_path,
    detect_platform,
    local_config_path,
    log_path_for,
    user_config_dir,
    user_config_path,
    user_data_dir,
)

POSIX_HOME = Path("/home/tester")
MAC_HOME = Path("/Users/tester")
WIN_HOME = Path("C:/Users/tester")

# The two halves of a real Windows profile, spelled out so the tests below can
# assert that the database lands in one of them and never the other.
WIN_ROAMING = "C:/Users/tester/AppData/Roaming"
WIN_LOCAL = "C:/Users/tester/AppData/Local"
WIN_ENV = {"APPDATA": WIN_ROAMING, "LOCALAPPDATA": WIN_LOCAL}


# --------------------------------------------------------------------------
# detect_platform
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("os_name", "sys_platform", "expected"),
    [
        ("nt", "win32", WINDOWS),
        # os.name is "posix" on macOS too, which is exactly why sys.platform
        # is a second input rather than an implementation detail.
        ("posix", "darwin", MACOS),
        ("posix", "linux", POSIX),
        ("posix", "freebsd14", POSIX),
        ("posix", "openbsd7", POSIX),
    ],
)
def test_detect_platform(os_name, sys_platform, expected):
    assert (
        detect_platform(os_name=os_name, sys_platform=sys_platform) == expected
    )


def test_windows_wins_before_sys_platform_is_consulted():
    """A Windows machine never has to claim to be "win32"."""
    assert detect_platform(os_name="nt", sys_platform="linux") == WINDOWS


def test_detect_platform_defaults_to_the_live_interpreter(monkeypatch):
    """The only test here that patches `os.name`, and it patches it briefly.

    `pathlib.Path` picks its flavour from `os.name` at construction, so while
    that lie is in place *any* Path built anywhere raises -- including inside
    pytest's own failure reporting, which turns a one-line assertion failure
    into an INTERNALERROR. Hence: read the answers, undo, then assert. It is
    also the reason the directory functions below take the platform as an
    argument instead of expecting tests to patch `os.name` around them.
    """
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "darwin")
    as_macos = detect_platform()

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(sys, "platform", "win32")
    as_windows = detect_platform()

    monkeypatch.undo()

    assert as_macos == MACOS
    assert as_windows == WINDOWS


# --------------------------------------------------------------------------
# user_config_dir
# --------------------------------------------------------------------------


def test_posix_config_dir_follows_xdg_config_home():
    env = {"XDG_CONFIG_HOME": "/home/tester/xdg-config"}
    assert (
        user_config_dir(platform=POSIX, env=env, home=POSIX_HOME)
        == Path("/home/tester/xdg-config") / APP_NAME
    )


def test_posix_config_dir_defaults_to_dot_config():
    assert user_config_dir(platform=POSIX, env={}, home=POSIX_HOME) == Path(
        "/home/tester/.config/ga4-realtime"
    )


@pytest.mark.parametrize("value", ["", "   ", "relative/config", "./cfg"])
def test_posix_config_dir_ignores_an_empty_or_relative_xdg_value(value):
    """Set-but-useless is the same as unset.

    An empty value would become Path(""), which is Path("."), which is the
    working directory -- the one place this module promises never to send
    anything. The XDG spec says the same about a relative path in as many
    words.
    """
    env = {"XDG_CONFIG_HOME": value}
    assert user_config_dir(platform=POSIX, env=env, home=POSIX_HOME) == Path(
        "/home/tester/.config/ga4-realtime"
    )


def test_macos_config_dir_is_application_support():
    assert user_config_dir(platform=MACOS, env={}, home=MAC_HOME) == Path(
        "/Users/tester/Library/Application Support/ga4-realtime"
    )


def test_macos_ignores_xdg_config_home():
    """XDG is not macOS's convention, and honouring it would split the tree.

    Half the tool's files under ~/Library and half under a variable some
    other program exported is worse than either choice made consistently.
    """
    env = {"XDG_CONFIG_HOME": "/Users/tester/xdg"}
    assert user_config_dir(platform=MACOS, env=env, home=MAC_HOME) == Path(
        "/Users/tester/Library/Application Support/ga4-realtime"
    )


def test_windows_config_dir_is_appdata():
    assert (
        user_config_dir(platform=WINDOWS, env=WIN_ENV, home=WIN_HOME)
        == Path(WIN_ROAMING) / APP_NAME
    )


def test_windows_config_dir_falls_back_to_the_profile_layout():
    """%APPDATA% can genuinely be missing under a service account."""
    assert user_config_dir(platform=WINDOWS, env={}, home=WIN_HOME) == Path(
        "C:/Users/tester/AppData/Roaming/ga4-realtime"
    )


def test_windows_ignores_a_relative_appdata():
    env = {"APPDATA": "AppData\\Roaming"}
    assert user_config_dir(platform=WINDOWS, env=env, home=WIN_HOME) == Path(
        "C:/Users/tester/AppData/Roaming/ga4-realtime"
    )


# --------------------------------------------------------------------------
# user_data_dir -- and the %LOCALAPPDATA% decision it exists for
# --------------------------------------------------------------------------


def test_windows_data_dir_is_localappdata():
    assert (
        user_data_dir(platform=WINDOWS, env=WIN_ENV, home=WIN_HOME)
        == Path(WIN_LOCAL) / APP_NAME
    )


def test_windows_data_is_not_under_appdata():
    """The decision the module exists to hold, asserted directly.

    %APPDATA% roams -- a domain profile copies it at logon and logoff, and a
    sync client can be pointed at it. A SQLite database in WAL mode is three
    files that are only consistent with each other at one instant, so a
    database that roams is a corruption report waiting to happen.
    """
    data = user_data_dir(platform=WINDOWS, env=WIN_ENV, home=WIN_HOME)
    config = user_config_dir(platform=WINDOWS, env=WIN_ENV, home=WIN_HOME)

    assert data != config
    assert Path(WIN_ROAMING) not in data.parents
    assert "Roaming" not in data.parts
    assert Path(WIN_LOCAL) in data.parents


def test_windows_data_dir_falls_back_to_the_local_profile_layout():
    assert user_data_dir(platform=WINDOWS, env={}, home=WIN_HOME) == Path(
        "C:/Users/tester/AppData/Local/ga4-realtime"
    )


def test_windows_data_dir_ignores_appdata_entirely():
    """Only %LOCALAPPDATA% is read, so a set %APPDATA% changes nothing."""
    env = {"APPDATA": WIN_ROAMING}
    assert user_data_dir(platform=WINDOWS, env=env, home=WIN_HOME) == Path(
        "C:/Users/tester/AppData/Local/ga4-realtime"
    )


def test_posix_data_dir_defaults_to_local_share():
    assert user_data_dir(platform=POSIX, env={}, home=POSIX_HOME) == Path(
        "/home/tester/.local/share/ga4-realtime"
    )


def test_posix_data_dir_follows_xdg_data_home():
    env = {"XDG_DATA_HOME": "/home/tester/xdg-data"}
    assert user_data_dir(platform=POSIX, env=env, home=POSIX_HOME) == Path(
        "/home/tester/xdg-data/ga4-realtime"
    )


def test_posix_data_dir_ignores_xdg_config_home():
    """The two XDG variables are separate, and so are the two directories."""
    env = {"XDG_CONFIG_HOME": "/home/tester/xdg-config"}
    assert user_data_dir(platform=POSIX, env=env, home=POSIX_HOME) == Path(
        "/home/tester/.local/share/ga4-realtime"
    )


def test_posix_config_and_data_are_different_directories():
    assert user_config_dir(
        platform=POSIX, env={}, home=POSIX_HOME
    ) != user_data_dir(platform=POSIX, env={}, home=POSIX_HOME)


def test_macos_data_dir_is_application_support_too():
    """Not an oversight: Apple's convention puts both in one place."""
    assert user_data_dir(platform=MACOS, env={}, home=MAC_HOME) == Path(
        "/Users/tester/Library/Application Support/ga4-realtime"
    )
    assert user_data_dir(
        platform=MACOS, env={}, home=MAC_HOME
    ) == user_config_dir(platform=MACOS, env={}, home=MAC_HOME)


# --------------------------------------------------------------------------
# The files inside those directories
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "home"),
    [(POSIX, POSIX_HOME), (MACOS, MAC_HOME), (WINDOWS, WIN_HOME)],
)
def test_user_config_path_is_config_toml_in_the_config_dir(platform, home):
    directory = user_config_dir(platform=platform, env=WIN_ENV, home=home)
    path = user_config_path(platform=platform, env=WIN_ENV, home=home)
    assert path == directory / CONFIG_FILENAME
    assert path.name == "config.toml"


def test_local_config_path_carries_the_program_name(tmp_path):
    assert local_config_path(tmp_path) == tmp_path / LOCAL_CONFIG_FILENAME
    assert local_config_path(tmp_path).name == "ga4-realtime.toml"


def test_local_config_path_takes_the_directory_it_is_given(tmp_path):
    """Never the process's working directory, which is not tmp_path."""
    assert local_config_path(tmp_path).parent == tmp_path
    assert local_config_path(tmp_path) != local_config_path(
        tmp_path / "elsewhere"
    )


def test_config_candidates_put_the_project_directory_first(tmp_path):
    candidates = config_candidates(
        tmp_path, platform=POSIX, env={}, home=POSIX_HOME
    )
    assert candidates == (
        tmp_path / LOCAL_CONFIG_FILENAME,
        Path("/home/tester/.config/ga4-realtime/config.toml"),
    )


def test_config_candidates_are_returned_even_when_nothing_exists(tmp_path):
    """The "no config found" error has to name every place it looked."""
    candidates = config_candidates(
        tmp_path, platform=WINDOWS, env=WIN_ENV, home=WIN_HOME
    )
    assert len(candidates) == 2
    assert not any(path.exists() for path in candidates)


@pytest.mark.parametrize(
    ("platform", "home", "expected"),
    [
        (POSIX, POSIX_HOME, "/home/tester/.local/share/ga4-realtime"),
        (
            MACOS,
            MAC_HOME,
            "/Users/tester/Library/Application Support/ga4-realtime",
        ),
        (WINDOWS, WIN_HOME, WIN_LOCAL + "/ga4-realtime"),
    ],
)
def test_default_database_path_sits_in_the_data_dir(platform, home, expected):
    path = default_database_path(platform=platform, env=WIN_ENV, home=home)
    assert path == Path(expected) / DATABASE_FILENAME
    assert path.name == "ga4_realtime.db"


# --------------------------------------------------------------------------
# log_path_for
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("database", "expected"),
    [
        ("/var/lib/ga4/ga4_realtime.db", "/var/lib/ga4/ga4_realtime.log"),
        ("/srv/site.db", "/srv/site.log"),
        ("/srv/two.dots.db", "/srv/two.dots.log"),
        ("/srv/plain", "/srv/plain.log"),
    ],
)
def test_log_lives_beside_the_database(database, expected):
    assert log_path_for(Path(database)) == Path(expected)


def test_log_follows_the_database_when_db_is_overridden(tmp_path):
    """A --db somewhere else takes its log with it."""
    elsewhere = tmp_path / "elsewhere" / "other.db"
    assert log_path_for(elsewhere).parent == elsewhere.parent


def test_default_log_sits_in_the_data_dir():
    database = default_database_path(
        platform=WINDOWS, env=WIN_ENV, home=WIN_HOME
    )
    expected = Path(WIN_LOCAL) / APP_NAME / "ga4_realtime.log"
    assert log_path_for(database) == expected


# --------------------------------------------------------------------------
# The promises the module makes about itself
# --------------------------------------------------------------------------


def _module_tree() -> ast.Module:
    source = Path(paths.__file__).read_text(encoding="utf-8")
    return ast.parse(source)


def test_the_module_never_reads_the_working_directory_or_its_own_location():
    """Asserted against the source, because a comment is not enforcement.

    Parsed rather than grepped so the docstrings, which discuss both of these
    at length, cannot make the test pass or fail by accident.
    """
    tree = _module_tree()
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attributes = {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }

    assert "__file__" not in names
    assert not {"cwd", "getcwd", "getcwdb", "chdir"} & attributes


def test_explicit_arguments_ignore_the_live_environment(monkeypatch):
    """Purity, stated as the failure it prevents.

    If an explicit argument could be overruled by the process environment, a
    test would answer for the machine running it and every platform but that
    one would go unchecked. The environment is set to values that are wrong
    for the answer expected, on both conventions at once, so a leak in either
    direction shows up here.
    """
    monkeypatch.setenv("APPDATA", "C:/wrong/Roaming")
    monkeypatch.setenv("LOCALAPPDATA", "C:/wrong/Local")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/wrong/config")
    monkeypatch.setenv("XDG_DATA_HOME", "/wrong/data")

    assert user_config_dir(platform=POSIX, env={}, home=POSIX_HOME) == Path(
        "/home/tester/.config/ga4-realtime"
    )
    assert user_data_dir(platform=POSIX, env={}, home=POSIX_HOME) == Path(
        "/home/tester/.local/share/ga4-realtime"
    )
    assert (
        user_config_dir(platform=WINDOWS, env=WIN_ENV, home=WIN_HOME)
        == Path(WIN_ROAMING) / APP_NAME
    )


def test_defaults_do_read_the_live_platform_and_environment(monkeypatch):
    """The other half: omit the arguments and this machine answers.

    Whichever machine that is -- the expectation branches on the live
    platform rather than pretending to be one, because `os.name` cannot be
    patched around a call that builds a Path (see detect_platform above).
    Each branch is exercised by whichever CI cell is that platform.
    """
    monkeypatch.setenv("APPDATA", "C:/live/Roaming")
    monkeypatch.setenv("LOCALAPPDATA", "C:/live/Local")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/live/config")
    monkeypatch.setenv("XDG_DATA_HOME", "/live/data")
    home = Path("/live/home")

    config = user_config_dir(home=home)
    data = user_data_dir(home=home)
    live = detect_platform()

    if live == WINDOWS:
        assert config == Path("C:/live/Roaming") / APP_NAME
        assert data == Path("C:/live/Local") / APP_NAME
    elif live == MACOS:
        support = home / "Library" / "Application Support"
        assert config == support / APP_NAME
        assert data == support / APP_NAME
    else:
        assert config == Path("/live/config") / APP_NAME
        assert data == Path("/live/data") / APP_NAME


def test_resolving_a_path_creates_nothing(tmp_path):
    """`doctor` prints the resolved database path and must not create it.

    Opening a RealtimeStore creates the file, so the path has to be
    answerable without opening anything -- otherwise the diagnosis is also
    the side effect.
    """
    for platform in (POSIX, MACOS, WINDOWS):
        user_config_dir(platform=platform, env={}, home=tmp_path)
        user_data_dir(platform=platform, env={}, home=tmp_path)
        user_config_path(platform=platform, env={}, home=tmp_path)
        default_database_path(platform=platform, env={}, home=tmp_path)
        config_candidates(tmp_path, platform=platform, env={}, home=tmp_path)
        log_path_for(tmp_path / "ga4_realtime.db")

    assert list(tmp_path.iterdir()) == []
