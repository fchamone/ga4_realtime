"""The three states of a site, and the database `sites` must not create.

`sites` answers one question -- "what is there, and what is still being
polled?" -- from two sources that can disagree. The config file says which
sites exist *now*; `site_meta` says which ones have history. D17 is the rule
for the disagreement: a site removed from the config keeps its history and is
listed as **orphaned**, because nothing you collected should become
unreachable by editing a text file.

Two things are pinned beyond the listing itself.

**It creates nothing.** Pointed at a database that is not there, it says so.
`RealtimeStore` creates the file it is handed, so the check has to come before
the open -- and a run that invented an empty database would make the *next*
run report a file this one made up.

**It works from `--db` alone**, with no config file, no credentials and no
network (§6, D15). In that mode there is nothing for a site to be orphaned
*from*, and the listing says as much rather than marking every stored site
orphaned against a config it never read.
"""

from pathlib import Path

import pytest

from ga4_realtime import cli, commands

# `mysite` is enabled, `newsite` is switched off and has never been polled,
# and the seeded database also holds `clientb` -- which this file does not
# mention, so it is the orphan. All three states of §7.9 in one run.
THREE_STATE_CONFIG = """
    [defaults]
    credentials = "secrets/ga4-service-account.json"
    conversions = ["generate_lead"]

    [storage]
    database = "{database}"

    [[sites]]
    name        = "mysite"
    property_id = "123456789"

    [[sites]]
    name        = "newsite"
    property_id = "555000111"
    enabled     = false
"""

# The same database, with `clientb` present but switched off: a disabled site
# that *does* have history, which is the case the orphan test cannot cover.
PAUSED_SITE_CONFIG = """
    [defaults]
    credentials = "secrets/ga4-service-account.json"
    conversions = ["generate_lead"]

    [storage]
    database = "{database}"

    [[sites]]
    name        = "mysite"
    property_id = "123456789"

    [[sites]]
    name        = "clientb"
    property_id = "987654321"
    enabled     = false
"""


@pytest.fixture
def config_for(write_config, seeded_store):
    """Write a config whose [storage] database is the seeded one.

    `as_posix()` rather than `str()`: a Windows path in a TOML basic string
    would have every backslash read as an escape, and `\\ga4` is not one.
    """

    def _write(body: str) -> Path:
        return write_config(body.format(database=seeded_store.path.as_posix()))

    return _write


def _sites(*argv: str) -> int:
    return cli.main([*argv, "sites"])


# --------------------------------------------------------------------------
# From --db alone: no config, no credentials, no network
# --------------------------------------------------------------------------


def test_from_the_database_alone_it_lists_what_is_stored(
    seeded_store, monkeypatch, capsys
):
    def _refuse(*args, **kwargs):
        pytest.fail("the config file was read for a --db listing")

    monkeypatch.setattr(commands, "load_config", _refuse)

    assert _sites("--db", str(seeded_store.path)) == 0

    out = capsys.readouterr().out
    for site in seeded_store.sites:
        assert site in out
        assert seeded_store.property_id(site) in out
        assert seeded_store.tz_name(site) in out
    # Nothing can be orphaned from a config that was never read, and saying so
    # of every stored site would be the most alarming possible way to be
    # wrong.
    assert "orphaned" not in out


def test_a_missing_database_is_reported_and_not_created(tmp_path, capsys):
    missing = tmp_path / "data" / "ga4_realtime.db"

    assert _sites("--db", str(missing)) == 0

    out = capsys.readouterr().out
    assert str(missing.resolve()) in out
    assert not missing.exists()
    # Not even the directory: RealtimeStore makes its parent as well as the
    # file, so a partial creation would still be a creation.
    assert not missing.parent.exists()


# --------------------------------------------------------------------------
# With a config: all three states of §7.9
# --------------------------------------------------------------------------


def test_all_three_states_appear_in_one_listing(
    config_for, seeded_store, capsys
):
    config = config_for(THREE_STATE_CONFIG)

    assert _sites("--config", str(config)) == 0

    out = capsys.readouterr().out
    assert "enabled" in out
    assert "disabled" in out
    assert "orphaned" in out
    for name in ("mysite", "newsite", "clientb"):
        assert name in out


def test_an_orphan_is_named_with_the_config_it_is_missing_from(
    config_for, seeded_store, capsys
):
    config = config_for(THREE_STATE_CONFIG)

    assert _sites("--config", str(config)) == 0

    out = capsys.readouterr().out
    assert str(config.resolve()) in out
    # The whole point of not hiding it: what was collected is still readable.
    assert "report --site clientb" in out
    assert seeded_store.tz_name("clientb") in out


def test_a_disabled_site_keeps_what_it_stored(
    config_for, seeded_store, capsys
):
    config = config_for(PAUSED_SITE_CONFIG)

    assert _sites("--config", str(config)) == 0

    out = capsys.readouterr().out
    assert "disabled" in out
    assert "orphaned" not in out
    assert seeded_store.property_id("clientb") in out
    assert seeded_store.tz_name("clientb") in out


def test_a_configured_site_with_no_history_says_so(
    config_for, seeded_store, capsys
):
    # `newsite` is in the file and in nobody's site_meta. "Nothing stored" and
    # "no such site" are different answers and it must give the first one.
    config = config_for(THREE_STATE_CONFIG)

    assert _sites("--config", str(config)) == 0

    out = capsys.readouterr().out
    assert "newsite" in out
    assert "nothing stored yet" in out


def test_the_database_and_the_config_are_both_named(
    config_for, seeded_store, capsys
):
    config = config_for(THREE_STATE_CONFIG)

    assert _sites("--config", str(config)) == 0

    out = capsys.readouterr().out
    assert str(config.resolve()) in out
    assert str(seeded_store.path) in out
