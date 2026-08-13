"""Discovery, ``${VAR}``, merging, validation -- the four jobs of config.py.

Two habits run through the file.

The working directory is always somewhere *other* than the config file. Every
relative path in a config resolves against the file's own directory, and a
test that puts both in the same place cannot tell that rule from the one it
replaced. So `config_home` holds the config and `decoy_cwd` is an empty
directory the process is actually chdir'd into.

Errors are asserted on their content, not merely on their type. `ConfigError`
exists so the user gets a message naming the file to edit and the exact fix;
a test that only checked the exception class would pass just as happily for a
message that says nothing.
"""

import logging
from pathlib import Path

import pytest

from ga4_realtime import paths
from ga4_realtime.config import (
    DEFAULT_INTERVAL,
    DEFAULT_POLL_WINDOW,
    DEFAULT_REFRESH,
    DEFAULT_WINDOW_HOURS,
    Config,
    Overrides,
    SiteConfig,
    UiConfig,
    discover_config_path,
    load_config,
    merge_site,
    merge_ui,
)
from ga4_realtime.errors import ConfigError

# The key stub `config_home` writes, spelled the way a config file spells it.
KEY_RELATIVE = "secrets/ga4-service-account.json"

# A site with the two fields that cannot be guessed and nothing else.
# Everything a test wants to vary is appended to it or replaced inside it;
# appended lines land in the trailing [[sites]] block, which is what most of
# the per-site cases below rely on.
MINIMAL = """
    [defaults]
    credentials = "secrets/ga4-service-account.json"
    conversions = ["purchase"]

    [[sites]]
    name        = "mysite"
    property_id = "123456789"
"""


@pytest.fixture
def decoy_cwd(tmp_path, monkeypatch) -> Path:
    """A working directory holding nothing the config could resolve against.

    Both passed as `cwd=` and entered with chdir: the module takes the
    working directory as an argument and never reads it, and the chdir is
    what would catch a lapse back to `Path.cwd()` or a bare relative open.
    """
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    monkeypatch.chdir(decoy)
    return decoy


@pytest.fixture
def key_file(config_home) -> Path:
    """The resolved path of the service-account stub, for comparisons."""
    return (config_home / KEY_RELATIVE).resolve()


# --------------------------------------------------------------------------
# Discovery -- three steps, in order
# --------------------------------------------------------------------------


def test_explicit_config_beats_both_other_locations(config_home, write_config):
    """--config is an answer, not a first candidate."""
    chosen = write_config(MINIMAL, filename="elsewhere.toml")
    write_config(MINIMAL)  # ./ga4-realtime.toml, deliberately ignored
    assert discover_config_path(chosen, cwd=config_home) == chosen.resolve()


def test_explicit_relative_config_resolves_against_the_given_cwd(
    config_home, write_config
):
    write_config(MINIMAL, filename="other.toml")
    found = discover_config_path("other.toml", cwd=config_home)
    assert found == (config_home / "other.toml").resolve()


def test_missing_explicit_config_names_the_path_it_looked_at(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        discover_config_path("nope.toml", cwd=tmp_path)
    message = str(excinfo.value)
    assert "nope.toml" in message
    assert str((tmp_path / "nope.toml").resolve()) in message


def test_the_working_directory_is_the_second_place_looked_at(
    config_home, write_config
):
    written = write_config(MINIMAL)
    assert discover_config_path(cwd=config_home) == written.resolve()


def test_the_platform_config_dir_is_the_last_resort(tmp_path):
    """Step 3, in whichever of the three conventions the host machine uses.

    `home=` is steered rather than the environment, because an XDG value has
    to be POSIX-absolute and an %APPDATA% value Windows-absolute -- steering
    `home` produces a real directory under tmp_path on all three platforms,
    which keeps this honest on both CI runners.
    """
    home = tmp_path / "home"
    target = paths.user_config_path(env={}, home=home)
    target.parent.mkdir(parents=True)
    target.write_text("", encoding="utf-8")

    empty = tmp_path / "empty"
    empty.mkdir()
    assert (
        discover_config_path(cwd=empty, env={}, home=home) == target.resolve()
    )


def test_the_working_directory_beats_the_platform_config_dir(
    tmp_path, config_home, write_config
):
    home = tmp_path / "home"
    platform_config = paths.user_config_path(env={}, home=home)
    platform_config.parent.mkdir(parents=True)
    platform_config.write_text("", encoding="utf-8")

    local = write_config(MINIMAL)
    found = discover_config_path(cwd=config_home, env={}, home=home)
    assert found == local.resolve()


def test_no_config_anywhere_names_all_three_places_and_init(tmp_path):
    home = tmp_path / "home"
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(ConfigError) as excinfo:
        discover_config_path(cwd=empty, env={}, home=home)

    message = str(excinfo.value)
    assert "--config" in message
    assert str(empty / paths.LOCAL_CONFIG_FILENAME) in message
    assert str(paths.user_config_path(env={}, home=home)) in message
    assert "ga4-realtime init" in message


def test_load_config_discovers_when_no_path_is_given(config_home, tmp_config):
    config = load_config(cwd=config_home)
    assert config.source == tmp_config.resolve()
    assert config.directory == config_home.resolve()


# --------------------------------------------------------------------------
# ${VAR} interpolation
# --------------------------------------------------------------------------


def test_var_is_read_from_the_dotenv_beside_the_config(
    write_config, decoy_cwd
):
    path = write_config(
        MINIMAL.replace('"123456789"', '"${GA4_PID}"'),
        env="GA4_PID=555000111\n",
    )
    config = load_config(path, cwd=decoy_cwd, env={})
    assert config.sites[0].property_id == "555000111"


def test_var_is_read_from_the_environment(write_config, decoy_cwd):
    path = write_config(MINIMAL.replace('"123456789"', '"${GA4_PID}"'))
    config = load_config(path, cwd=decoy_cwd, env={"GA4_PID": "42"})
    assert config.sites[0].property_id == "42"


def test_the_environment_beats_the_dotenv_file(write_config, decoy_cwd):
    """A one-off override that edits nothing, which is dotenv's own rule."""
    path = write_config(
        MINIMAL.replace('"123456789"', '"${GA4_PID}"'),
        env="GA4_PID=111\n",
    )
    config = load_config(path, cwd=decoy_cwd, env={"GA4_PID": "222"})
    assert config.sites[0].property_id == "222"


def test_var_interpolates_inside_a_list(write_config, decoy_cwd):
    """Any string value, not an allow-list of keys."""
    path = write_config(
        MINIMAL.replace('["purchase"]', '["${WANTED}", "sign_up"]')
    )
    config = load_config(path, cwd=decoy_cwd, env={"WANTED": "purchase"})
    assert config.sites[0].conversions == ["purchase", "sign_up"]


def test_unresolved_var_names_both_the_variable_and_the_key(
    write_config, decoy_cwd
):
    path = write_config(MINIMAL.replace('"123456789"', '"${GA4_PID}"'))
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, cwd=decoy_cwd, env={})

    message = str(excinfo.value)
    assert "GA4_PID" in message
    assert "sites[0].property_id" in message
    assert str((path.parent / ".env").resolve()) in message


def test_a_var_set_to_the_empty_string_counts_as_unset(
    write_config, decoy_cwd
):
    """An empty property ID fails hundreds of lines later, or not at all."""
    path = write_config(
        MINIMAL.replace('"123456789"', '"${GA4_PID}"'), env="GA4_PID=\n"
    )
    with pytest.raises(ConfigError, match="GA4_PID"):
        load_config(path, cwd=decoy_cwd, env={})


# --------------------------------------------------------------------------
# property_id -- the highest-value error message in the tool
# --------------------------------------------------------------------------


def test_measurement_id_is_rejected_by_name(write_config, decoy_cwd):
    path = write_config(MINIMAL.replace('"123456789"', '"G-ABC1234567"'))
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, cwd=decoy_cwd, env={})

    message = str(excinfo.value)
    assert "measurement" in message
    assert "gtag.js" in message
    assert "Property settings" in message
    assert "mysite" in message
    assert str(path.resolve()) in message


def test_non_digit_property_id_is_rejected(write_config, decoy_cwd):
    path = write_config(MINIMAL.replace('"123456789"', '"my-property"'))
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, cwd=decoy_cwd, env={})
    assert "all digits" in str(excinfo.value)


def test_a_missing_property_id_says_where_to_find_it(write_config, decoy_cwd):
    path = write_config(MINIMAL.replace('property_id = "123456789"', ""))
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, cwd=decoy_cwd, env={})
    assert "Property ID" in str(excinfo.value)


def test_an_unquoted_property_id_is_accepted_as_a_number(
    write_config, decoy_cwd
):
    """TOML makes `property_id = 123456789` look reasonable; it is."""
    path = write_config(MINIMAL.replace('"123456789"', "123456789"))
    config = load_config(path, cwd=decoy_cwd, env={})
    assert config.sites[0].property_id == "123456789"


# --------------------------------------------------------------------------
# Site names
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["mysite", "a", "0", "client-b", "client_b", "a" * 32]
)
def test_valid_site_names(write_config, decoy_cwd, name):
    path = write_config(MINIMAL.replace('"mysite"', f'"{name}"'))
    config = load_config(path, cwd=decoy_cwd, env={})
    assert config.names == [name]


@pytest.mark.parametrize(
    "name",
    [
        "MySite",  # a case-insensitive filesystem makes this two of one site
        "-leading",
        "_leading",
        "has space",
        "has.dot",
        "",
        "a" * 33,
    ],
)
def test_invalid_site_names_are_rejected(write_config, decoy_cwd, name):
    path = write_config(MINIMAL.replace('"mysite"', f'"{name}"'))
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, cwd=decoy_cwd, env={})
    assert "name" in str(excinfo.value)


def test_a_site_without_a_name_is_rejected(write_config, decoy_cwd):
    path = write_config(MINIMAL.replace('name        = "mysite"', ""))
    with pytest.raises(ConfigError, match="no name"):
        load_config(path, cwd=decoy_cwd, env={})


def test_duplicate_site_names_are_rejected(write_config, decoy_cwd):
    path = write_config(
        MINIMAL
        + """
        [[sites]]
        name        = "mysite"
        property_id = "987654321"
        """
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, cwd=decoy_cwd, env={})
    message = str(excinfo.value)
    assert "'mysite'" in message
    assert "storage key" in message


# --------------------------------------------------------------------------
# Merging: built-in under [defaults] under [[sites]] under the flags (D16)
# --------------------------------------------------------------------------


def test_built_in_defaults_apply_when_nothing_says_otherwise(
    write_config, decoy_cwd
):
    path = write_config(MINIMAL)
    config = load_config(path, cwd=decoy_cwd, env={})
    site = config.sites[0]
    assert site.interval == DEFAULT_INTERVAL
    assert site.poll_window == DEFAULT_POLL_WINDOW
    assert site.timezone is None
    assert site.label is None
    assert site.enabled is True
    assert site.color == config.ui.color
    assert config.ui.refresh == DEFAULT_REFRESH
    assert config.ui.window == DEFAULT_WINDOW_HOURS


def test_a_site_overrides_defaults_and_leaves_its_sibling_alone(
    tmp_config, decoy_cwd
):
    config = load_config(tmp_config, cwd=decoy_cwd, env={})
    inherited, overridden = config.sites

    assert inherited.interval == 300
    assert inherited.conversions == ["generate_lead"]
    assert inherited.timezone is None
    assert inherited.color == "cyan"

    assert overridden.interval == 120
    assert overridden.conversions == ["purchase", "sign_up"]
    assert overridden.timezone == "Asia/Tokyo"
    assert overridden.color == "magenta"
    # Not overridden in that block, so it still comes from [defaults].
    assert overridden.poll_window == 10


def test_a_flag_beats_defaults_and_every_per_site_value(tmp_config, decoy_cwd):
    """D16, asserted per site rather than per layer: it beats both, twice."""
    config = load_config(
        tmp_config,
        cwd=decoy_cwd,
        env={},
        overrides=Overrides(interval=60, timezone="America/Sao_Paulo"),
    )
    assert [site.interval for site in config.sites] == [60, 60]
    assert [site.timezone for site in config.sites] == [
        "America/Sao_Paulo",
        "America/Sao_Paulo",
    ]


def test_merge_site_is_where_the_precedence_is_decided(
    config_home, tmp_config
):
    """The same rule at the seam, with no file-reading around it."""
    ui = merge_ui({}, source=tmp_config)
    site = merge_site(
        {"name": "explicit", "property_id": "123456789", "interval": 120},
        {
            "credentials": KEY_RELATIVE,
            "conversions": ["purchase"],
            "interval": 300,
        },
        ui=ui,
        config_dir=config_home,
        source=tmp_config,
        overrides=Overrides(interval=60),
    )
    assert isinstance(site, SiteConfig)
    assert site.interval == 60


def test_ui_flags_beat_the_ui_table(tmp_config, decoy_cwd):
    config = load_config(
        tmp_config,
        cwd=decoy_cwd,
        env={},
        overrides=Overrides(refresh=2, window=12, ascii=True),
    )
    assert isinstance(config.ui, UiConfig)
    assert config.ui.refresh == 2
    assert config.ui.window == 12
    assert config.ui.ascii is True


def test_the_ascii_flag_can_only_switch_ascii_on(tmp_config, decoy_cwd):
    """store_true cannot express "off", so None must not mean False."""
    config = load_config(
        tmp_config, cwd=decoy_cwd, env={}, overrides=Overrides(ascii=None)
    )
    assert config.ui.ascii is False


# --------------------------------------------------------------------------
# Path resolution
# --------------------------------------------------------------------------


def test_relative_paths_resolve_against_the_config_dir_not_cwd(
    config_home, key_file, tmp_config, decoy_cwd
):
    """The decoy cwd has no secrets/ directory, so a lapse fails loudly."""
    config = load_config(tmp_config, cwd=decoy_cwd, env={})
    assert config.sites[0].credentials == key_file
    assert (
        config.database == (config_home / "out" / "ga4_realtime.db").resolve()
    )


def test_the_log_lives_beside_the_database(tmp_config, decoy_cwd):
    config = load_config(tmp_config, cwd=decoy_cwd, env={})
    assert config.log_path.parent == config.database.parent
    assert config.log_path.suffix == ".log"


def test_an_unset_database_lands_in_the_platform_data_dir(
    tmp_path, write_config, decoy_cwd
):
    home = tmp_path / "home"
    path = write_config(MINIMAL)
    config = load_config(path, cwd=decoy_cwd, env={}, home=home)
    assert (
        config.database
        == paths.default_database_path(env={}, home=home).resolve()
    )


def test_the_db_flag_beats_the_config_and_follows_the_shell(
    tmp_config, decoy_cwd
):
    """A --db typed at a shell is relative to that shell, not to the file."""
    config = load_config(
        tmp_config,
        cwd=decoy_cwd,
        env={},
        overrides=Overrides(database=Path("other.db")),
    )
    assert config.database == (decoy_cwd / "other.db").resolve()


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


def test_a_missing_key_file_names_it_and_says_where_to_get_one(
    write_config, decoy_cwd
):
    path = write_config(MINIMAL.replace(KEY_RELATIVE, "secrets/absent.json"))
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, cwd=decoy_cwd, env={})

    message = str(excinfo.value)
    assert str((path.parent / "secrets" / "absent.json").resolve()) in message
    assert "Service accounts" in message
    assert "mysite" in message


def test_a_site_can_name_its_own_key(config_home, write_config, decoy_cwd):
    (config_home / "secrets" / "other.json").write_text("{}", encoding="utf-8")
    path = write_config(MINIMAL + '\ncredentials = "secrets/other.json"\n')
    config = load_config(path, cwd=decoy_cwd, env={})
    assert config.sites[0].credentials.name == "other.json"


def test_credentials_are_required_somewhere(write_config, decoy_cwd):
    path = write_config(MINIMAL.replace(f'credentials = "{KEY_RELATIVE}"', ""))
    with pytest.raises(ConfigError, match="credentials"):
        load_config(path, cwd=decoy_cwd, env={})


# --------------------------------------------------------------------------
# Conversions
# --------------------------------------------------------------------------


def test_conversions_have_no_built_in_default(write_config, decoy_cwd):
    """The one it replaced was an event only the author's site fires."""
    path = write_config(MINIMAL.replace('conversions = ["purchase"]', ""))
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, cwd=decoy_cwd, env={})
    assert "conversions" in str(excinfo.value)


@pytest.mark.parametrize(
    "value", ["[]", '"purchase"', '["purchase", ""]', "[1]"]
)
def test_malformed_conversions_are_rejected(write_config, decoy_cwd, value):
    path = write_config(MINIMAL.replace('["purchase"]', value))
    with pytest.raises(ConfigError, match="conversions"):
        load_config(path, cwd=decoy_cwd, env={})


# --------------------------------------------------------------------------
# Timezone
# --------------------------------------------------------------------------


def test_an_unresolvable_timezone_blames_the_missing_database(
    write_config, decoy_cwd
):
    path = write_config(MINIMAL + '\ntimezone = "Mars/Olympus"\n')
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, cwd=decoy_cwd, env={})

    message = str(excinfo.value)
    assert "Mars/Olympus" in message
    assert "tzdata" in message
    assert "mysite" in message


def test_a_timezone_that_is_a_path_is_rejected_as_a_name(
    write_config, decoy_cwd
):
    """ZoneInfo raises ValueError, not ZoneInfoNotFoundError, for a path.

    Worth its own branch because the tzdata advice would be wrong here: the
    database is present and the *name* is not one.
    """
    path = write_config(MINIMAL + '\ntimezone = "/etc/localtime"\n')
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, cwd=decoy_cwd, env={})

    message = str(excinfo.value)
    assert "IANA" in message
    assert "tzdata" not in message


def test_an_empty_timezone_means_ask_the_admin_api(write_config, decoy_cwd):
    path = write_config(MINIMAL + '\ntimezone = ""\n')
    config = load_config(path, cwd=decoy_cwd, env={})
    assert config.sites[0].timezone is None


# --------------------------------------------------------------------------
# Bounds and types
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "needle"),
    [
        ("interval = 0", "at least 1"),
        ("poll_window = 0", "between 1 and 30"),
        ("poll_window = 31", "between 1 and 30"),
    ],
)
def test_out_of_range_site_values_name_the_bound(
    write_config, decoy_cwd, line, needle
):
    path = write_config(f"{MINIMAL}\n{line}\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, cwd=decoy_cwd, env={})
    assert needle in str(excinfo.value)


@pytest.mark.parametrize(
    ("line", "needle"),
    [
        ("refresh = 0", "at least 1"),
        ("window = 0", "between 1 and 24"),
        ("window = 25", "between 1 and 24"),
        ("top_events = 0", "at least 1"),
    ],
)
def test_out_of_range_ui_values_name_the_bound(
    write_config, decoy_cwd, line, needle
):
    path = write_config(f"{MINIMAL}\n[ui]\n{line}\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, cwd=decoy_cwd, env={})
    assert needle in str(excinfo.value)


@pytest.mark.parametrize(
    ("line", "needle"),
    [
        ('interval = "300"', "whole number"),
        ("interval = true", "whole number"),
        ('enabled = "yes"', "true or false"),
        ("label = 7", "text in quotes"),
    ],
)
def test_wrong_types_are_reported_as_types(
    write_config, decoy_cwd, line, needle
):
    path = write_config(f"{MINIMAL}\n{line}\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, cwd=decoy_cwd, env={})
    assert needle in str(excinfo.value)


def test_a_section_that_is_not_a_table_is_rejected(write_config, decoy_cwd):
    """`ui = 3` at the top level, which TOML accepts and this must not."""
    path = write_config(f"ui = 3\n{MINIMAL}")
    with pytest.raises(ConfigError, match="table"):
        load_config(path, cwd=decoy_cwd, env={})


def test_malformed_toml_names_the_file(write_config, decoy_cwd):
    path = write_config("[[sites]\nname = 'x'\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, cwd=decoy_cwd, env={})
    message = str(excinfo.value)
    assert str(path.resolve()) in message
    assert "TOML" in message


# --------------------------------------------------------------------------
# Unknown keys warn, and only warn
# --------------------------------------------------------------------------


def test_a_typo_in_defaults_warns_and_names_the_nearest_key(
    write_config, decoy_cwd, caplog
):
    """`intervall` silently doing nothing is the outcome being prevented."""
    path = write_config(
        MINIMAL.replace(
            'conversions = ["purchase"]',
            'conversions = ["purchase"]\n    intervall   = 60',
        )
    )
    with caplog.at_level(logging.WARNING):
        config = load_config(path, cwd=decoy_cwd, env={})

    assert config.sites[0].interval == DEFAULT_INTERVAL
    assert "'intervall'" in caplog.text
    assert "'interval'" in caplog.text
    assert "[defaults]" in caplog.text


def test_an_unknown_site_key_warns_naming_the_site(
    write_config, decoy_cwd, caplog
):
    path = write_config(MINIMAL + "\nconversion = 3\n")
    with caplog.at_level(logging.WARNING):
        load_config(path, cwd=decoy_cwd, env={})
    assert "'conversion'" in caplog.text
    assert "'conversions'" in caplog.text
    assert "mysite" in caplog.text


def test_an_unknown_top_level_key_lists_the_valid_sections(
    write_config, decoy_cwd, caplog
):
    path = write_config(MINIMAL + "\n[nonsense]\nwhatever = 1\n")
    with caplog.at_level(logging.WARNING):
        load_config(path, cwd=decoy_cwd, env={})
    assert "'nonsense'" in caplog.text
    assert "defaults" in caplog.text
    assert "storage" in caplog.text


# --------------------------------------------------------------------------
# The site list as a whole
# --------------------------------------------------------------------------


def test_a_config_with_no_sites_is_rejected(write_config, decoy_cwd):
    path = write_config('[defaults]\nconversions = ["purchase"]\n')
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, cwd=decoy_cwd, env={})
    assert "[[sites]]" in str(excinfo.value)


def test_a_config_with_every_site_disabled_is_rejected(
    write_config, decoy_cwd
):
    """Distinct from "no sites": the file is full of them and none run."""
    path = write_config(MINIMAL + "\nenabled = false\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, cwd=decoy_cwd, env={})
    message = str(excinfo.value)
    assert "enabled = true" in message
    assert "report" in message


def test_sites_that_are_not_a_list_of_tables_are_rejected(
    write_config, decoy_cwd
):
    path = write_config(
        'sites = "mysite"\n\n[defaults]\nconversions = ["p"]\n'
    )
    with pytest.raises(ConfigError, match=r"\[\[sites\]\]"):
        load_config(path, cwd=decoy_cwd, env={})


def test_disabled_sites_stay_loaded_but_are_not_polled(
    write_config, decoy_cwd
):
    """D17: nothing collected becomes unreachable by editing a text file."""
    path = write_config(
        MINIMAL
        + """
        [[sites]]
        name        = "retired"
        property_id = "987654321"
        enabled     = false
        """
    )
    config = load_config(path, cwd=decoy_cwd, env={})
    assert config.names == ["mysite", "retired"]
    assert [site.name for site in config.enabled_sites()] == ["mysite"]


def test_an_unknown_site_name_lists_the_ones_that_exist(tmp_config, decoy_cwd):
    config = load_config(tmp_config, cwd=decoy_cwd, env={})
    assert isinstance(config, Config)
    assert config.site("clientb").property_id == "987654321"

    with pytest.raises(ConfigError) as excinfo:
        config.site("clientc")
    message = str(excinfo.value)
    assert "clientc" in message
    assert "mysite, clientb" in message
