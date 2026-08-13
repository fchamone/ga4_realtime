"""The configuration file: discovery, ``${VAR}``, merging, validation.

The widest-reaching module in the package. `SiteConfig` is the shape every
signature downstream is written against, so what this module decides is what
the rest of the tool is able to express.

Four jobs, in the order they happen.

**Discovery** is three steps and no more: ``--config``, then
``./ga4-realtime.toml``, then the platform config directory. No environment
variable, and no walking up parent directories. Both of those turn "which
file did it actually read?" into a question, and that question is the first
thing a support conversation needs answered.

**Interpolation** replaces ``${VAR}`` in *any* string value, from the process
environment and from a ``.env`` sitting beside the config file. An unset
variable is an error naming both the variable and the key that wanted it --
never an empty string, because an empty property ID reaches the API as a
permission error and an empty credentials path reaches the filesystem as the
working directory.

**Merging** layers a built-in default under ``[defaults]`` under each
``[[sites]]`` entry, and then puts every command-line flag on top of all
three. That last step is D16 and it is decided in one place, `merge_site`,
so there is exactly one function to read and one function to test.

**Validation** happens here, at load time, before a single network call. Every
mistake it catches is one the user can fix in a text file, and none of them
are worth an API round-trip to discover.

Every failure raises `ConfigError` naming the file to edit and the key inside
it. Nothing in here prints: ``cli.main()`` turns the exception into one line
on stderr and exit 1.
"""

import difflib
import logging
import os
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values

from . import paths
from .errors import ConfigError

log = logging.getLogger(__name__)

# The secrets file, read from beside the config file rather than from the
# working directory. A config that has been found by step 3 of discovery is
# usually nowhere near the shell's cwd, and its secrets belong with it.
ENV_FILENAME = ".env"

# The polled window is deliberately wider than the default interval so that
# late-arriving events and skipped cycles are re-read and corrected rather
# than lost. Widening it costs nothing but a slightly larger response.
DEFAULT_POLL_WINDOW = 10
DEFAULT_INTERVAL = 300
DEFAULT_REFRESH = 10

# Hours of history the chart shows. Unrelated to DEFAULT_POLL_WINDOW above:
# that one is how far back each request reads, this one is how much of the
# accumulated day is on screen at once.
DEFAULT_WINDOW_HOURS = 6
DEFAULT_TOP_EVENTS = 10
DEFAULT_COLOR = "cyan"
DEFAULT_CONVERSION_COLOR = "green"

# 30 is the Realtime API's own reach: it cannot answer for a minute further
# back than that, so a larger number would ask for rows that do not exist.
MAX_POLL_WINDOW = 30
# Capped at a day because past that the chart's axis labels wrap onto clock
# times already on it, and the same tick reads as two different moments.
MAX_WINDOW_HOURS = 24

# Lowercase, digits, underscore and hyphen, starting with an alphanumeric,
# at most 32 characters. The name is the storage key every row is written
# under and the word typed after --site, so it has to survive a shell, a
# case-insensitive filesystem and a SQL literal unchanged. Allowing "ClientB"
# alongside "clientb" would produce two histories that read as one site.
SITE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# Only the braced form. "$VAR" is left alone deliberately: property IDs and
# Windows paths are full of characters that a looser syntax would start
# interpreting, and a config file is not a shell.
_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Keys accepted in each table. Anything else warns (never raises) naming the
# nearest of these, because a typo'd `intervall` that silently does nothing is
# the worst outcome available -- the same reasoning that decided D16.
TOP_LEVEL_KEYS = frozenset({"defaults", "ui", "storage", "sites"})
# Per-site overridable. `name` and `property_id` are absent on purpose: they
# identify a site, so inheriting them would give every site one identity.
OVERRIDABLE_KEYS = frozenset(
    {
        "credentials",
        "interval",
        "poll_window",
        "conversions",
        "timezone",
        "label",
        "color",
        "enabled",
    }
)
SITE_KEYS = OVERRIDABLE_KEYS | {"name", "property_id"}
UI_KEYS = frozenset(
    {"refresh", "window", "top_events", "ascii", "color", "conversion_color"}
)
STORAGE_KEYS = frozenset({"database"})

# Distinguishes "key absent" from "key present and set to None", which TOML
# cannot express but a defaulted lookup would blur.
_MISSING = object()


# --------------------------------------------------------------------------
# The resolved shapes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class UiConfig:
    """Everything under ``[ui]``, after the flags have had their say.

    Global rather than per-site: the dashboard shows one site at a time (D4),
    so a per-site refresh interval or chart width would change the furniture
    under the reader every time they pressed Tab. `color` is the exception --
    it is per-site overridable, and this value is the fallback for sites that
    do not name one.
    """

    refresh: int
    window: int
    top_events: int
    ascii: bool
    color: str
    conversion_color: str


@dataclass(frozen=True)
class SiteConfig:
    """One GA4 property and everything the tool needs to poll and draw it.

    `timezone` is None when the config leaves it empty, meaning "ask the
    Admin API at startup". That is a different thing from "UTC": UTC is what
    the tool falls back to when the Admin API refuses, and conflating the two
    would hide the refusal.

    `conversions` is a list because the single-file tool hardcoded one
    lead-event name that no site but the author's fires. There is no
    built-in default: an unconfigured conversion list would count nothing
    while looking like it was counting something.
    """

    name: str
    property_id: str
    credentials: Path
    conversions: list[str]
    interval: int
    poll_window: int
    timezone: str | None
    label: str | None
    color: str
    enabled: bool


@dataclass(frozen=True)
class Overrides:
    """The command-line flags that outrank the file. D16, as data.

    One rule: **a flag beats ``[defaults]`` and every per-site value, for
    every site.** `--interval 60` polls every site every 60 seconds even
    where the config says otherwise. The rejected alternative -- explicit
    per-site config beating the flag -- would make `--interval 60` silently
    do nothing for the one site being investigated, which is exactly the
    silent no-op the unknown-key warning exists to prevent.

    `None` means "the flag was not given", including for `ascii`: it is an
    argparse store_true, so it can only ever turn the thing on, and False
    would be indistinguishable from a flag that was never typed.
    """

    database: Path | None = None
    interval: int | None = None
    refresh: int | None = None
    window: int | None = None
    timezone: str | None = None
    ascii: bool | None = None


# "No flags were given", as one shared frozen instance. A default argument of
# `Overrides()` would build a new object per call for no reason, and the
# linter is right to object to a function call sitting in a signature.
NO_OVERRIDES = Overrides()


@dataclass(frozen=True)
class Config:
    """A loaded, merged, validated config file.

    `source` is kept so every later error can name the file the user has to
    edit, which is otherwise lost the moment the tables become dataclasses.
    """

    source: Path
    database: Path
    log_path: Path
    ui: UiConfig
    sites: list[SiteConfig]

    @property
    def directory(self) -> Path:
        """The directory every relative path in the file resolved against."""
        return self.source.parent

    @property
    def names(self) -> list[str]:
        return [site.name for site in self.sites]

    def enabled_sites(self) -> list[SiteConfig]:
        """The sites that are polled and cycled through, in file order.

        Order is the file's, not sorted: the first enabled site is the one
        the dashboard opens on, so the user decides which that is by moving a
        block rather than by renaming it (D17 keeps disabled sites readable
        by `report`, so they are dropped here and nowhere else).
        """
        return [site for site in self.sites if site.enabled]

    def site(self, name: str) -> SiteConfig:
        """Look a site up by name, or name every site that does exist.

        The listing is the whole point of raising here rather than returning
        None: a typo'd --site is most cheaply fixed by being shown the
        spellings that would have worked.
        """
        for site in self.sites:
            if site.name == name:
                return site
        known = ", ".join(self.names) or "(none)"
        raise ConfigError(
            f"no site named {name!r} in {self.source}.\n"
            f"Configured sites: {known}."
        )


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def discover_config_path(
    explicit: str | Path | None = None,
    *,
    cwd: Path,
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Find the config file, or explain every place that was looked at.

    An explicit ``--config`` is not a first candidate but an answer: given
    one, the file either exists or the run fails. Falling through to the
    other two after a typo'd path would load a *different* config than the
    one that was asked for, which is worse than the error.

    `cwd` is a required argument rather than a `Path.cwd()` default for the
    reason `paths` gives: a scheduled run has no useful working directory,
    and picking one up implicitly is how a Task Scheduler invocation ends up
    reading a different file than the interactive one.
    """
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = Path(cwd) / path
        path = path.resolve()
        if not path.is_file():
            raise ConfigError(
                f"--config {explicit} does not exist.\n"
                f"Looked at {path}.\n"
                "Run `ga4-realtime init` to write a starter config, or "
                "check the path."
            )
        return path

    candidates = paths.config_candidates(
        cwd, platform=platform, env=env, home=home
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()

    raise ConfigError(
        "no configuration file found. Three places are looked at, in "
        "order:\n"
        "  1. --config PATH                (not given)\n"
        f"  2. {candidates[0]}\n"
        f"  3. {candidates[1]}\n"
        "Run `ga4-realtime init` to write a starter config in this "
        "directory, or\n"
        f"`ga4-realtime init --path {candidates[1]}` to write it where "
        "every\nworking directory will find it."
    )


# --------------------------------------------------------------------------
# Reading and interpolation
# --------------------------------------------------------------------------


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except OSError as exc:
        raise ConfigError(
            f"cannot read {path}: {exc.strerror or exc}."
        ) from None
    except tomllib.TOMLDecodeError as exc:
        # tomllib's message carries the line and column, which is the only
        # useful thing to say about a syntax error, so it is quoted verbatim
        # rather than paraphrased.
        raise ConfigError(
            f"{path} is not valid TOML.\n"
            f"  {exc}\n"
            "Text values need quotes; a list of events looks like\n"
            '  conversions = ["purchase", "sign_up"]'
        ) from None


def _environment(
    config_dir: Path, env: Mapping[str, str] | None
) -> dict[str, str]:
    """The lookup ``${VAR}`` is resolved against: ``.env``, then the shell.

    The process environment wins over the file, which is python-dotenv's own
    default and the behaviour worth keeping: it makes
    ``GA4_CREDENTIALS=... ga4-realtime doctor`` a one-off override that does
    not involve editing anything.

    Nothing is written back into ``os.environ``. The old code exported
    GOOGLE_APPLICATION_CREDENTIALS so the Google libraries would find it,
    which cannot work once two sites have two different keys -- the
    environment is process-global. Clients are constructed with explicit
    credentials instead, so this stays a read.
    """
    base = dict(os.environ if env is None else env)
    env_path = config_dir / ENV_FILENAME
    if not env_path.is_file():
        return base
    from_file = {
        key: value
        for key, value in dotenv_values(env_path).items()
        if value is not None
    }
    from_file.update(base)
    return from_file


def _expand(
    value: str,
    env: Mapping[str, str],
    *,
    key: str,
    source: Path,
    env_path: Path,
) -> str:
    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        resolved = env.get(name)
        # Set-but-empty counts as unset. A blank property ID or credentials
        # path substitutes cleanly and then fails hundreds of lines later,
        # somewhere that says nothing about the variable that caused it.
        if resolved is None or not resolved.strip():
            raise ConfigError(
                f"{key} in {source} refers to ${{{name}}}, which is not "
                "set.\n"
                f"Define {name} in {env_path}, or export it in the "
                "environment before running.\n"
                "An unset variable is never substituted with an empty "
                "string."
            )
        return resolved

    return _VAR_PATTERN.sub(_replace, value)


def _interpolate(
    value: Any,
    env: Mapping[str, str],
    *,
    key: str,
    source: Path,
    env_path: Path,
) -> Any:
    """Walk the whole document expanding ``${VAR}`` in every string.

    Every string, not a named allow-list of keys: the point is that a user
    can keep whichever value they consider sensitive out of a file they might
    paste into an issue, and that judgement is theirs, not this module's.

    The key path is threaded through as text (``sites[1].credentials``) so a
    failure names the exact line to look at rather than only the variable,
    which in a five-site config may appear five times.
    """
    if isinstance(value, str):
        return _expand(value, env, key=key, source=source, env_path=env_path)
    if isinstance(value, dict):
        return {
            name: _interpolate(
                item,
                env,
                key=f"{key}.{name}" if key else name,
                source=source,
                env_path=env_path,
            )
            for name, item in value.items()
        }
    if isinstance(value, list):
        return [
            _interpolate(
                item,
                env,
                key=f"{key}[{index}]",
                source=source,
                env_path=env_path,
            )
            for index, item in enumerate(value)
        ]
    return value


# --------------------------------------------------------------------------
# Unknown keys -- a warning, never an error
# --------------------------------------------------------------------------


def _suggest(key: str, allowed: Iterable[str]) -> str:
    """Name the nearest valid key, or list them all when nothing is near."""
    valid = sorted(allowed)
    matches = difflib.get_close_matches(key, valid, n=1, cutoff=0.6)
    if matches:
        return f"did you mean {matches[0]!r}?"
    return "valid keys here are: " + ", ".join(valid) + "."


def _warn_unknown(
    table: Mapping[str, Any],
    allowed: Iterable[str],
    *,
    where: str,
    source: Path,
) -> None:
    """Warn about keys nobody reads, and keep going.

    A warning rather than an error, deliberately. Refusing to start over a
    stray key would punish the user for a future version's spelling, and the
    failure this exists to prevent is the opposite one: a key that looks like
    it is doing something and is not.

    It reaches the user even though the log file has not been opened yet --
    config loads before logging is configured, so logging's last-resort
    handler puts this on stderr, which is where a startup warning belongs.
    """
    for key in table:
        if key in allowed:
            continue
        log.warning(
            "%s: unknown key %r in %s; %s The key is ignored.",
            source,
            key,
            where,
            _suggest(key, allowed),
        )


# --------------------------------------------------------------------------
# Typed reads -- every one of them names the key and the file
# --------------------------------------------------------------------------


def _as_str(value: Any, *, where: str, source: Path) -> str:
    if not isinstance(value, str):
        raise ConfigError(
            f"{where} in {source} must be text in quotes, not {value!r}."
        )
    return value.strip()


def _as_bool(value: Any, *, where: str, source: Path) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(
            f"{where} in {source} must be true or false, not {value!r}."
        )
    return value


def _as_int(
    value: Any,
    *,
    where: str,
    source: Path,
    minimum: int,
    maximum: int | None = None,
) -> int:
    # bool is a subclass of int, so `interval = true` would otherwise arrive
    # here as 1 and be accepted as a plausible number of seconds.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"{where} in {source} must be a whole number, not {value!r}."
        )
    if value < minimum or (maximum is not None and value > maximum):
        bound = (
            f"at least {minimum}"
            if maximum is None
            else f"between {minimum} and {maximum}"
        )
        raise ConfigError(
            f"{where} in {source} is {value}; it must be {bound}."
        )
    return value


def _as_events(value: Any, *, where: str, source: Path) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ConfigError(
            f"{where} in {source} must be a non-empty list of event names, "
            f"not {value!r}.\n"
            '  conversions = ["purchase", "sign_up"]'
        )
    events: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(
                f"{where} in {source} contains {item!r}; every entry must "
                "be a non-empty event name exactly as GA4 reports it."
            )
        events.append(item.strip())
    return events


def _resolve_path(raw: str, *, base: Path) -> Path:
    """Resolve one relative path against the config file's own directory.

    Never against the working directory, and never against ``__file__`` --
    which now points inside site-packages and must never be written to. The
    consequence is the one a contributor expects: a repo-local
    ``./ga4-realtime.toml`` keeps its database and its keys inside the repo,
    and a config in the platform config directory keeps them beside itself.
    """
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(base) / path
    return path.resolve()


# --------------------------------------------------------------------------
# Merging -- built-in under [defaults] under [[sites]] under the flags
# --------------------------------------------------------------------------


def _pick(
    raw: Mapping[str, Any],
    defaults: Mapping[str, Any],
    key: str,
    fallback: Any,
    *,
    site_where: str,
) -> tuple[Any, str]:
    """Three layers, and a label saying which one the value came from.

    The label is half the point: "interval in [defaults]" and "interval in
    [[sites]] 'clientb'" send the reader to different lines of the same file,
    and a merged value with no provenance sends them to neither.
    """
    if key in raw:
        return raw[key], f"{key} in {site_where}"
    if key in defaults:
        return defaults[key], f"{key} in [defaults]"
    return fallback, key


def merge_ui(
    raw: Mapping[str, Any],
    *,
    source: Path,
    overrides: Overrides = NO_OVERRIDES,
) -> UiConfig:
    """``[ui]`` under the built-in defaults, with the flags on top."""
    refresh = _as_int(
        raw.get("refresh", DEFAULT_REFRESH),
        where="[ui] refresh",
        source=source,
        minimum=1,
    )
    window = _as_int(
        raw.get("window", DEFAULT_WINDOW_HOURS),
        where="[ui] window",
        source=source,
        minimum=1,
        maximum=MAX_WINDOW_HOURS,
    )
    top_events = _as_int(
        raw.get("top_events", DEFAULT_TOP_EVENTS),
        where="[ui] top_events",
        source=source,
        minimum=1,
    )
    ascii_mode = _as_bool(
        raw.get("ascii", False), where="[ui] ascii", source=source
    )
    color = _as_str(
        raw.get("color", DEFAULT_COLOR), where="[ui] color", source=source
    )
    conversion_color = _as_str(
        raw.get("conversion_color", DEFAULT_CONVERSION_COLOR),
        where="[ui] conversion_color",
        source=source,
    )

    # D16 for the global half. The flags are already bounds-checked by
    # argparse before they get here, so this is a substitution and not a
    # second validation pass.
    if overrides.refresh is not None:
        refresh = overrides.refresh
    if overrides.window is not None:
        window = overrides.window
    if overrides.ascii:
        ascii_mode = True

    return UiConfig(
        refresh=refresh,
        window=window,
        top_events=top_events,
        ascii=ascii_mode,
        color=color,
        conversion_color=conversion_color,
    )


def merge_site(
    raw: Mapping[str, Any],
    defaults: Mapping[str, Any],
    *,
    ui: UiConfig,
    config_dir: Path,
    source: Path,
    overrides: Overrides = NO_OVERRIDES,
    index: int = 0,
) -> SiteConfig:
    """One ``[[sites]]`` block, merged and validated into a `SiteConfig`.

    This is where D16 is decided: after the three config layers have been
    collapsed, a command-line flag replaces the result **whatever it was** --
    inherited or explicit, for this site and every other one.

    Validation happens here rather than in a later pass so that every message
    can say which site and which layer the bad value came from. The order
    inside is deliberate: the property ID is checked before the key file, so
    the measurement-ID mix-up is reported even when the credentials are not
    sorted out yet. It is the mistake that costs the most time to diagnose
    from the API's own error.
    """
    # `name` and `property_id` are read from the site block alone -- an empty
    # mapping for the [defaults] layer, not because it is cheaper but because
    # inheriting an identity would give two sites one property.
    site_where = f"[[sites]] entry #{index + 1}"
    name_value, name_where = _pick(
        raw, {}, "name", _MISSING, site_where=site_where
    )
    if name_value is _MISSING:
        raise ConfigError(
            f"{site_where} in {source} has no name.\n"
            "Every site needs a short slug used as the storage key and as "
            "the argument to --site:\n"
            '  name = "mysite"'
        )
    name = _as_str(name_value, where=name_where, source=source)
    if not SITE_NAME_PATTERN.match(name):
        raise ConfigError(
            f"site name {name!r} in {source} is not usable.\n"
            "A name is lowercase letters, digits, underscore and hyphen, "
            "starts with a letter or digit, and is at most 32 characters.\n"
            "It is the storage key every row is written under and the word "
            "typed after --site."
        )

    site_where = f"[[sites]] {name!r}"
    _warn_unknown(raw, SITE_KEYS, where=site_where, source=source)

    property_id = _property_id(raw, name, site_where, source)
    credentials = _credentials(
        raw, defaults, name, site_where, config_dir, source
    )

    conversions_value, conversions_where = _pick(
        raw, defaults, "conversions", _MISSING, site_where=site_where
    )
    if conversions_value is _MISSING:
        # No built-in default on purpose. The constant this replaced named
        # a lead event only the author's site fires; any other single
        # guess would be as wrong, and a silent empty list would show a
        # conversion count of zero forever without ever saying that
        # nothing was configured.
        raise ConfigError(
            f"no conversions configured for site {name!r} in {source}.\n"
            "Name the events that count as conversions, either under "
            "[defaults] for every site or in this [[sites]] block:\n"
            '  conversions = ["purchase", "sign_up"]'
        )
    conversions = _as_events(
        conversions_value, where=conversions_where, source=source
    )

    interval_value, interval_where = _pick(
        raw, defaults, "interval", DEFAULT_INTERVAL, site_where=site_where
    )
    interval = _as_int(
        interval_value, where=interval_where, source=source, minimum=1
    )
    poll_value, poll_where = _pick(
        raw,
        defaults,
        "poll_window",
        DEFAULT_POLL_WINDOW,
        site_where=site_where,
    )
    poll_window = _as_int(
        poll_value,
        where=poll_where,
        source=source,
        minimum=1,
        maximum=MAX_POLL_WINDOW,
    )

    timezone = _timezone(raw, defaults, name, site_where, source, overrides)

    label_value, label_where = _pick(
        raw, defaults, "label", None, site_where=site_where
    )
    label = (
        None
        if label_value is None
        else (_as_str(label_value, where=label_where, source=source) or None)
    )

    color_value, color_where = _pick(
        raw, defaults, "color", ui.color, site_where=site_where
    )
    color = _as_str(color_value, where=color_where, source=source)

    enabled_value, enabled_where = _pick(
        raw, defaults, "enabled", True, site_where=site_where
    )
    enabled = _as_bool(enabled_value, where=enabled_where, source=source)

    # D16, the site half: the flag lands on top of whatever the three layers
    # agreed on. `--interval 60` polls this site every 60 seconds even where
    # its own block says 120, because the alternative is a flag that
    # silently does nothing for the one site being investigated.
    if overrides.interval is not None:
        interval = overrides.interval

    return SiteConfig(
        name=name,
        property_id=property_id,
        credentials=credentials,
        conversions=conversions,
        interval=interval,
        poll_window=poll_window,
        timezone=timezone,
        label=label,
        color=color,
        enabled=enabled,
    )


def _property_id(
    raw: Mapping[str, Any], name: str, site_where: str, source: Path
) -> str:
    """Read `property_id`, rejecting the measurement ID by name.

    Passing G-XXXXXXXXXX to the API yields a PermissionDenied that never
    mentions the real cause, and it is the single most common way to lose an
    afternoon to this setup. This message is the highest-value error in the
    tool; it says which ID was found, what that ID is actually for, and where
    the right one lives.
    """
    value, where = _pick(
        raw, {}, "property_id", _MISSING, site_where=site_where
    )
    if value is _MISSING:
        raise ConfigError(
            f"site {name!r} in {source} has no property_id.\n"
            "It is the NUMERIC property ID, from GA4 -> Admin -> Property "
            "settings -> Property ID:\n"
            '  property_id = "123456789"'
        )
    # Numbers are accepted as well as strings: TOML makes `property_id =
    # 123456789` look perfectly reasonable, and refusing it over the quotes
    # would be pedantry. It is normalised to text because that is what the
    # API resource name and the site_meta column both hold.
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    text = _as_str(value, where=where, source=source)
    if text.upper().startswith("G-"):
        raise ConfigError(
            f"site {name!r} in {source} sets property_id = {text!r}, which "
            "is a *measurement* ID.\n"
            "That one belongs to gtag.js on the site itself; the API needs "
            "the numeric property ID.\n"
            "Find it in GA4 -> Admin -> Property settings -> Property ID."
        )
    if not text.isdigit():
        raise ConfigError(
            f"site {name!r} in {source} sets property_id = {text!r}; it "
            "must be all digits.\n"
            "Find it in GA4 -> Admin -> Property settings -> Property ID."
        )
    return text


def _credentials(
    raw: Mapping[str, Any],
    defaults: Mapping[str, Any],
    name: str,
    site_where: str,
    config_dir: Path,
    source: Path,
) -> Path:
    """Resolve the service-account key and prove it can be opened.

    Checked at load time, in the same pass as the rest, because a key file
    that is missing or unreadable is a text-file fix and there is no reason
    to spend an API round-trip discovering it. What is *not* checked here is
    whether the JSON is a valid key -- that fails at client construction in
    `ga4.py`, which is the only place that can tell.
    """
    value, where = _pick(
        raw, defaults, "credentials", _MISSING, site_where=site_where
    )
    if value is _MISSING:
        raise ConfigError(
            f"no credentials configured for site {name!r} in {source}.\n"
            "Point it at the service account's JSON key, either under "
            "[defaults] for every site or in this [[sites]] block:\n"
            '  credentials = "secrets/ga4-service-account.json"'
        )
    text = _as_str(value, where=where, source=source)
    path = _resolve_path(text, base=config_dir)
    if not path.is_file():
        raise ConfigError(
            f"service account key not found for site {name!r}: {path}\n"
            f"({where}, in {source}; a relative path resolves against that "
            "file's own directory.)\n"
            "Create it in Google Cloud Console -> IAM -> Service accounts "
            "-> Keys -> Add key -> JSON, then save it there."
        )
    if not os.access(path, os.R_OK):
        raise ConfigError(
            f"service account key for site {name!r} cannot be read: "
            f"{path}\nCheck the file's permissions."
        )
    return path


def _timezone(
    raw: Mapping[str, Any],
    defaults: Mapping[str, Any],
    name: str,
    site_where: str,
    source: Path,
    overrides: Overrides,
) -> str | None:
    """The site's timezone name, or None meaning "ask the Admin API".

    An empty string is the config's way of spelling None, which is why the
    example file writes ``timezone = ""`` rather than commenting the line
    out: it documents that the key exists and that leaving it blank is a
    choice, not an omission.
    """
    value, where = _pick(
        raw, defaults, "timezone", None, site_where=site_where
    )
    tz_name = (
        None
        if value is None
        else (_as_str(value, where=where, source=source) or None)
    )
    # D16 again. --timezone is documented as a debugging override rather than
    # a config replacement, and it forces *every* site's day boundaries, so
    # two sites in different zones become comparable for one run.
    if overrides.timezone is not None:
        tz_name = overrides.timezone
        where = "--timezone"
    if tz_name is None:
        return None
    try:
        ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        # Windows ships no IANA timezone database at all, so this is what a
        # missing tzdata package looks like from here. Say so rather than
        # leaving the user to read ZoneInfoNotFoundError and guess.
        raise ConfigError(
            f"timezone {tz_name!r} ({where}, for site {name!r} in "
            f"{source}) could not be resolved.\n"
            "If the name is right, Python has no system timezone database "
            "on this machine -- Windows ships none. Install the data "
            "package:\n"
            "  python -m pip install tzdata"
        ) from None
    except (ValueError, KeyError):
        raise ConfigError(
            f"timezone {tz_name!r} ({where}, for site {name!r} in "
            f"{source}) is not a valid IANA name.\n"
            'It looks like "America/Sao_Paulo" or "Asia/Tokyo".'
        ) from None
    return tz_name


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


def _database(
    storage: Mapping[str, Any],
    *,
    overrides: Overrides,
    config_dir: Path,
    cwd: Path,
    source: Path,
    platform: str | None,
    env: Mapping[str, str] | None,
    home: Path | None,
) -> Path:
    """Where the SQLite file lives, from the flag, the file, or the platform.

    Three sources and three different bases, each for its own reason. A
    ``--db`` typed at a shell is relative to that shell, because that is what
    the person typing it meant. ``[storage] database`` is relative to the
    config file, like every other path in it. And an unset key lands in the
    platform data directory rather than beside the config, so a config living
    in the Windows roaming profile does not put a WAL database there too.
    """
    if overrides.database is not None:
        return _resolve_path(str(overrides.database), base=cwd)
    if "database" in storage:
        text = _as_str(
            storage["database"], where="[storage] database", source=source
        )
        return _resolve_path(text, base=config_dir)
    return paths.default_database_path(
        platform=platform, env=env, home=home
    ).resolve()


# --------------------------------------------------------------------------
# The whole thing
# --------------------------------------------------------------------------


def load_config(
    explicit: str | Path | None = None,
    *,
    cwd: Path,
    overrides: Overrides = NO_OVERRIDES,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
) -> Config:
    """Find, read, interpolate, merge and validate the config file.

    The whole pipeline in one call, and it happens before anything opens a
    socket or a database. `platform`, `env` and `home` exist so the platform
    directories can be steered in tests exactly as `paths` allows; ordinary
    callers pass `cwd` and nothing else.
    """
    path = discover_config_path(
        explicit, cwd=cwd, platform=platform, env=env, home=home
    )
    config_dir = path.parent
    lookup = _environment(config_dir, env)
    raw = _read_toml(path)
    values = _interpolate(
        raw,
        lookup,
        key="",
        source=path,
        env_path=config_dir / ENV_FILENAME,
    )

    _warn_unknown(values, TOP_LEVEL_KEYS, where="the top level", source=path)
    defaults = _table(values, "defaults", path)
    _warn_unknown(defaults, OVERRIDABLE_KEYS, where="[defaults]", source=path)
    ui_table = _table(values, "ui", path)
    _warn_unknown(ui_table, UI_KEYS, where="[ui]", source=path)
    storage = _table(values, "storage", path)
    _warn_unknown(storage, STORAGE_KEYS, where="[storage]", source=path)

    ui = merge_ui(ui_table, source=path, overrides=overrides)
    database = _database(
        storage,
        overrides=overrides,
        config_dir=config_dir,
        cwd=cwd,
        source=path,
        platform=platform,
        env=env,
        home=home,
    )

    raw_sites = values.get("sites", [])
    if not isinstance(raw_sites, list) or not all(
        isinstance(entry, dict) for entry in raw_sites
    ):
        raise ConfigError(
            f"sites in {path} must be a list of [[sites]] blocks, each with "
            "its own name and property_id."
        )
    if not raw_sites:
        raise ConfigError(
            f"{path} defines no sites.\n"
            "Add at least one [[sites]] block:\n\n"
            "  [[sites]]\n"
            '  name = "mysite"\n'
            '  property_id = "123456789"'
        )

    sites = [
        merge_site(
            entry,
            defaults,
            ui=ui,
            config_dir=config_dir,
            source=path,
            overrides=overrides,
            index=index,
        )
        for index, entry in enumerate(raw_sites)
    ]

    seen: set[str] = set()
    for site in sites:
        if site.name in seen:
            raise ConfigError(
                f"{path} defines the site name {site.name!r} twice.\n"
                "A site name is the storage key every row is written under, "
                "so two blocks sharing one would merge into a single "
                "history that belongs to neither."
            )
        seen.add(site.name)

    if not any(site.enabled for site in sites):
        # Distinct from "no sites": the file is full of them and none will be
        # polled, which is a state worth naming rather than reporting as an
        # empty dashboard.
        raise ConfigError(
            f"every site in {path} has enabled = false, so there is nothing "
            "to poll.\n"
            "Set enabled = true on at least one [[sites]] block. Disabled "
            "sites stay readable with `ga4-realtime report`."
        )

    return Config(
        source=path,
        database=database,
        log_path=paths.log_path_for(database),
        ui=ui,
        sites=sites,
    )


def _table(
    values: Mapping[str, Any], key: str, source: Path
) -> dict[str, Any]:
    """One top-level table, defaulting to empty, refusing a non-table."""
    table = values.get(key, {})
    if not isinstance(table, dict):
        raise ConfigError(
            f"[{key}] in {source} must be a table of settings, not {table!r}."
        )
    return table
