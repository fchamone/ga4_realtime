"""Where the config file is looked for, and where the database lands.

Two questions, one module. Where should the tool *look* for a config file
nobody named on the command line, and where should it *put* a database nobody
chose a location for? Both answers differ per platform, and a wrong answer is
invisible on the machine that wrote it -- it only shows up when somebody
else's operating system puts its files somewhere else.

So every function here is pure: it takes the platform, the environment and
the home directory as arguments instead of reading them behind the caller's
back. That is what lets the Windows answers be tested from Linux and the XDG
answers be tested from Windows, which is the only way all three conventions
ever get tested at all -- CI runs two platforms and this module claims three.
The arguments default to the live interpreter, so ordinary callers pass
nothing and tests pass everything.

Nothing here reads the working directory or the location of this source file.
The working directory is an argument named `cwd` with **no default**,
precisely so it cannot be picked up by accident: Windows Task Scheduler runs
with the working directory at C:\\Windows\\System32 and a systemd unit runs at
/, so a cwd-relative default would send a scheduled run to a different
database than the interactive one and quietly fragment the history this tool
exists to accumulate. The installed package's own directory is not a
candidate for anything either -- it is inside site-packages and must never be
written to.

Nothing here touches the filesystem, either. These functions answer "where",
never "make it so": no directory is created and no file is stat'd, so
`doctor` can print the database path it resolved without that print being the
thing that creates the database.
"""

import os
import sys
from collections.abc import Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath

# The distribution name, reused verbatim as the directory name on all three
# platforms. One word to remember, and it is the word the user typed to
# install the thing.
APP_NAME = "ga4-realtime"

# Two names for the same file, on purpose. Inside the platform config
# directory the *directory* already says which program owns the file, so the
# file is plainly "config.toml"; in a project directory nothing says it, so
# the file carries the program's name instead.
CONFIG_FILENAME = "config.toml"
LOCAL_CONFIG_FILENAME = "ga4-realtime.toml"

# Underscored where the config file is hyphenated, matching the name the
# pre-package tool wrote and the name the example config still suggests. A
# database is opened by other programs -- `sqlite3 ga4_realtime.db "SELECT
# ..."` is a documented way to use this data -- so renaming it would break a
# habit for no gain.
DATABASE_FILENAME = "ga4_realtime.db"

# The three directory conventions, as plain strings rather than an Enum: they
# are compared, printed in test ids and passed as keyword arguments, and an
# Enum would earn its keep at none of those.
WINDOWS = "windows"
MACOS = "macos"
POSIX = "posix"


def detect_platform(
    *,
    os_name: str | None = None,
    sys_platform: str | None = None,
) -> str:
    """Which of the three directory conventions applies.

    Two inputs rather than one because `os.name` cannot tell macOS from
    Linux -- both are "posix" -- and the two put user files in entirely
    different places. `sys.platform` is only consulted once "nt" has been
    ruled out, so a Windows machine never has to claim to be "win32" for the
    Windows answer to come back.

    Everything that is not Windows and not macOS is treated as XDG. That
    includes the BSDs and every Linux distribution, which is the intent: the
    XDG layout is the fallback, not a fourth special case.
    """
    if os_name is None:
        os_name = os.name
    if sys_platform is None:
        sys_platform = sys.platform
    if os_name == "nt":
        return WINDOWS
    if sys_platform == "darwin":
        return MACOS
    return POSIX


def _is_absolute(value: str, platform: str) -> bool:
    """Absoluteness judged by the *named* platform, not the running one.

    `Path.is_absolute()` answers for the host, which would defeat the whole
    arrangement: "/home/x/.config" is not absolute to a WindowsPath, so a
    POSIX case tested from Windows would see its XDG value rejected as
    relative and silently fall through to the default. The pure flavours ask
    the right question from either machine.
    """
    flavour = PureWindowsPath if platform == WINDOWS else PurePosixPath
    return flavour(value).is_absolute()


def _env_dir(env: Mapping[str, str], key: str, platform: str) -> Path | None:
    """Read a directory out of the environment, ignoring useless answers.

    Unset, empty and relative all mean the same thing here: no answer. An
    empty %APPDATA% would otherwise become Path(""), which is Path("."),
    which is the working directory -- the one place this module has promised
    never to send anything. A relative value is refused for exactly the same
    reason, and the XDG base directory spec says the same thing about
    $XDG_CONFIG_HOME in as many words: a relative path must be ignored.
    """
    value = env.get(key, "").strip()
    if not value or not _is_absolute(value, platform):
        return None
    return Path(value)


def user_config_dir(
    *,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """The directory a config file lives in when nobody named one.

    Linux/BSD follows the XDG base directory spec. macOS follows Apple's own
    convention, Application Support -- deliberately not ~/Library/Preferences,
    which is for plists written by NSUserDefaults and not for a TOML file a
    person opens in an editor. Windows uses %APPDATA%, the roaming half of
    the profile, which is right for a config file for the same reason it is
    wrong for a database: a small hand-edited file *should* follow the user
    between machines.

    The %APPDATA% fallback exists because a service account or a stripped
    environment can be missing it; AppData\\Roaming under the home directory
    is the same place Windows itself would have named.
    """
    platform = detect_platform() if platform is None else platform
    env = os.environ if env is None else env
    home = Path.home() if home is None else home

    if platform == WINDOWS:
        base = _env_dir(env, "APPDATA", platform)
        if base is None:
            base = home / "AppData" / "Roaming"
    elif platform == MACOS:
        base = home / "Library" / "Application Support"
    else:
        base = _env_dir(env, "XDG_CONFIG_HOME", platform)
        if base is None:
            base = home / ".config"
    return base / APP_NAME


def user_data_dir(
    *,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """The directory the database lands in when the config does not say.

    Windows takes %LOCALAPPDATA%, **not** %APPDATA%, and that difference is
    the entire reason this is a separate function from `user_config_dir`.
    %APPDATA% is the roaming half of the profile: a domain roaming profile
    copies it between machines at logon and logoff, and a sync client
    (OneDrive being the one people actually hit) can be pointed at it too. A
    SQLite database in WAL mode is three files -- .db, .db-wal, .db-shm --
    that are only consistent with one another at a single instant, and a
    poller holds them open for hours. Copying them out of step, or letting a
    sync client take a lock on one of them mid-write, is a corruption report
    waiting to happen. %LOCALAPPDATA% is per-machine and is never synced,
    which is also the honest description of what this data is: one machine's
    local record of its own polling, not a document that should follow you.

    macOS has no such split to make -- Application Support holds both, so the
    answer is the same as `user_config_dir`'s and that is Apple's convention,
    not an oversight. Linux keeps the XDG separation it was given: config in
    ~/.config, data in ~/.local/share.
    """
    platform = detect_platform() if platform is None else platform
    env = os.environ if env is None else env
    home = Path.home() if home is None else home

    if platform == WINDOWS:
        base = _env_dir(env, "LOCALAPPDATA", platform)
        if base is None:
            base = home / "AppData" / "Local"
    elif platform == MACOS:
        base = home / "Library" / "Application Support"
    else:
        # $XDG_DATA_HOME is honoured as the counterpart of $XDG_CONFIG_HOME
        # above. A user who has moved one and not the other is telling us
        # something specific, and reading only the config half would put the
        # database somewhere they did not ask for.
        base = _env_dir(env, "XDG_DATA_HOME", platform)
        if base is None:
            base = home / ".local" / "share"
    return base / APP_NAME


def user_config_path(
    *,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """The config file inside the platform config directory."""
    return (
        user_config_dir(platform=platform, env=env, home=home)
        / CONFIG_FILENAME
    )


def local_config_path(cwd: Path) -> Path:
    """The config file a project directory carries beside its own files.

    `cwd` is required rather than defaulted for the reason the module
    docstring gives: the caller knows which directory it means, and a default
    would be this module reading the working directory it promised not to.
    """
    return Path(cwd) / LOCAL_CONFIG_FILENAME


def config_candidates(
    cwd: Path,
    *,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> tuple[Path, ...]:
    """The places a config file is looked for, in the order they are tried.

    An explicit --config is not one of them. It is not a search but an
    answer: given one, the file either exists or the run fails, and falling
    through to these after a typo'd path would be a worse outcome than the
    error.

    The project directory beats the user's own default so a checkout carrying
    its own ga4-realtime.toml behaves the same for everyone who clones it,
    and the platform directory exists so a scheduled run -- which has no
    useful working directory at all -- can find a config without every
    invocation spelling out an absolute path.

    Returned whether or not anything exists at either location, because the
    "no config found" error has to name every place that was tried: an error
    that says only "not found" leaves the user with nowhere to put the file.
    """
    return (
        local_config_path(cwd),
        user_config_path(platform=platform, env=env, home=home),
    )


def default_database_path(
    *,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Where the database goes when [storage] database is unset.

    Not beside the config file. A config in the platform config directory
    that implied a database next to it would put a growing binary in a
    directory Windows roams, which is the failure `user_data_dir` exists to
    avoid.
    """
    return (
        user_data_dir(platform=platform, env=env, home=home)
        / DATABASE_FILENAME
    )


def log_path_for(database: Path) -> Path:
    """The rotating log always lives beside the database.

    One rule in one place, because two places are what let them drift apart.
    A --db pointing somewhere else has to take its log with it: a log that
    describes a run while sitting beside a database that run never wrote to
    is worse than no log, since it is the file somebody will read first when
    the numbers look wrong.
    """
    database = Path(database)
    return database.with_suffix(".log")
