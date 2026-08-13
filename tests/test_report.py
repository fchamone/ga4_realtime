"""`report`: one section per site, from the database and nothing else.

Three properties are worth more here than any particular number.

**It works from `--db` alone.** No config file, no credentials, no network:
the site names, property IDs and timezones all come out of `site_meta`, which
the live view cached while it was collecting. Most tests below therefore run
under `no_config_anywhere`, which empties all three config discovery
locations, so a config file sitting on the machine running the suite cannot
quietly supply what the command is supposed to do without. Optional is not the
same as ignorable, though: a config the user *named* is read or the run fails
(§6), and only a config that discovery went looking for is allowed to come up
broken without stopping the report.

**The all-sites form contains the single-site form, byte for byte.** That is
the whole of D14's first clause, and it is asserted as a substring rather than
as a list of fields, because a script parsing one output has to be able to
parse the other. The combined total is labelled a sum of each site's *local*
day, and when the zones differ one warning line names them -- the sum is
never allowed to read as one span of time.

**Anything in `site_meta` is reportable.** D17: a site disabled or deleted
from the config still has rows, so `report` takes its name from the database
and not from the file. Nothing you collected becomes unreachable because you
edited a text file.

The numbers themselves are checked against `store.day_totals` over the
seeded fixture's own day bounds rather than against literals. What that pins
is the *choice of bounds* -- each site's own local day -- which is the part
that can be got wrong; the arithmetic underneath already has its own tests in
`test_store.py`.
"""

import re
from pathlib import Path

import pytest

from ga4_realtime import cli
from ga4_realtime.timeutil import floor_minute, iso_minute, utcnow

# A config naming only the first of the two seeded sites, so the second is
# orphaned in the sense §7.9 means: rows in the database, no block in the
# file.
ONE_SITE_CONFIG = """
    [defaults]
    credentials = "secrets/ga4-service-account.json"
    conversions = ["generate_lead"]

    [[sites]]
    name        = "mysite"
    property_id = "123456789"
"""

# The middle row of §7.9's table: in the config, switched off. It is not
# polled and never appears in the dashboard's rotation, and `report` treats it
# like any other site -- including counting the conversions its block names.
DISABLED_SITE_CONFIG = """
    [defaults]
    credentials = "secrets/ga4-service-account.json"
    conversions = ["generate_lead"]

    [[sites]]
    name        = "mysite"
    property_id = "123456789"

    [[sites]]
    name        = "clientb"
    property_id = "987654321"
    conversions = ["purchase", "sign_up"]
    enabled     = false
"""

# Two conversions, one of which the seed never fires. "Every configured
# conversion is listed by name with its count" is only worth asserting on an
# event that did not happen: a name that is missing and a name that is zero
# are different answers.
UNFIRED_CONVERSION_CONFIG = """
    [defaults]
    credentials = "secrets/ga4-service-account.json"
    conversions = ["generate_lead", "newsletter_signup"]

    [[sites]]
    name        = "mysite"
    property_id = "123456789"
"""

# A config file that does not parse: `[defaults` is never closed. The one
# failure that reaches `report` from a file it otherwise never needs, and the
# whole of what separates "named on the command line" from "found by looking".
BROKEN_CONFIG = """
    [defaults
    conversions = ["generate_lead"]
"""

# The label D14 requires on the combined block, quoted here exactly as it is
# printed: the label is the whole reason the sum is publishable at all.
SUM_LABEL = "sum of each site's local day"


def _run(*argv: str) -> int:
    return cli.main(list(argv))


@pytest.fixture
def no_config_anywhere(monkeypatch, tmp_path) -> Path:
    """Empty all three config discovery locations for one test.

    `report` is specified to work with `--db` alone, and the only honest way
    to test that is to make sure there is nothing for it to find: the author's
    own machine has a config in the working directory, and CI does not, so a
    test that did not do this would assert different things in the two places.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    # Belt and braces: every platform branch in `paths` falls back to the home
    # directory when its environment variable is unusable, and a real home
    # directory is exactly where a real config file would be.
    monkeypatch.setattr(Path, "home", lambda: home)
    return elsewhere


def _figure(text: str, label: str) -> int:
    """Read one `label   1,234` line out of a report.

    Anchored at the start of the line so `events` cannot match the `top
    events` heading, and tolerant of anything after the number so the
    combined block's parentheticals do not have to be spelled out here.
    """
    pattern = rf"^\s+{re.escape(label)}\s+([\d,]+)(?:\s|$)"
    found = re.findall(pattern, text, re.MULTILINE)
    assert found, f"no {label!r} figure in:\n{text}"
    return int(found[0].replace(",", ""))


def _combined(text: str) -> str:
    """Just the combined block -- everything from its heading onwards."""
    marker = text.index(SUM_LABEL)
    return text[marker:]


def _lines_containing(text: str, needle: str) -> list[str]:
    return [line for line in text.splitlines() if needle in line]


def _conversion_lines(text: str) -> list[str]:
    """Every `conversions <figure>` line in a report.

    One per section plus one in the combined block, which is why the tests
    below say *which* of them they mean: the two blocks are headed
    differently and so cannot word an absent count the same way.
    """
    return [
        line
        for line in text.splitlines()
        if line.strip().startswith("conversions")
    ]


# --------------------------------------------------------------------------
# --db alone
# --------------------------------------------------------------------------


def test_a_seeded_database_reports_with_db_alone(
    no_config_anywhere, seeded_store, capsys
) -> None:
    """No config file, no credentials, no network -- §7.8 and D15."""
    assert _run("--db", str(seeded_store.path), "report") == 0

    out = capsys.readouterr().out
    for site in seeded_store.sites:
        assert site in out
        assert seeded_store.property_id(site) in out
        assert seeded_store.tz_name(site) in out


def test_report_writes_nothing_beside_the_database(
    no_config_anywhere, seeded_store, capsys
) -> None:
    # start_logging() is opt-in for exactly this reason: a read-only command
    # writing a rotating log is a side effect nobody asked for, and --verbose
    # must not turn one on either.
    assert _run("--db", str(seeded_store.path), "--verbose", "report") == 0

    capsys.readouterr()
    assert not seeded_store.path.with_suffix(".log").exists()


def test_a_missing_database_creates_nothing(
    no_config_anywhere, tmp_path, capsys
) -> None:
    """`RealtimeStore` creates what it is pointed at; `report` must not.

    The parent directory is the tell: opening a store would have created it,
    so its absence proves the guard ran before the connection.
    """
    target = tmp_path / "nowhere" / "ga4_realtime.db"

    assert _run("--db", str(target), "report") == 1

    assert not target.exists()
    assert not target.parent.exists()
    assert "nowhere" in capsys.readouterr().err


def test_a_database_with_no_sites_says_so(
    no_config_anywhere, store, capsys
) -> None:
    assert _run("--db", str(store.path), "report") == 1

    error = capsys.readouterr().err
    assert "no sites" in error
    assert str(store.path) in error


# --------------------------------------------------------------------------
# One section per site, and the combined total
# --------------------------------------------------------------------------


def test_the_all_form_repeats_each_section_byte_for_byte(
    no_config_anywhere, seeded_store, capsys
) -> None:
    """D14's first clause: a script parsing one form parses the other."""
    sections = {}
    for site in seeded_store.sites:
        argv = ("--db", str(seeded_store.path), "--site", site, "report")
        assert _run(*argv) == 0
        sections[site] = capsys.readouterr().out

    assert _run("--db", str(seeded_store.path), "report") == 0
    everything = capsys.readouterr().out

    for site, section in sections.items():
        assert section.strip("\n") in everything, site


def test_site_all_is_the_same_as_omitting_it(
    no_config_anywhere, seeded_store, capsys
) -> None:
    assert _run("--db", str(seeded_store.path), "--site", "all", "report") == 0
    named = capsys.readouterr().out

    assert _run("--db", str(seeded_store.path), "report") == 0
    omitted = capsys.readouterr().out

    assert named == omitted


def test_the_combined_total_is_labelled_a_sum_of_local_days(
    no_config_anywhere, seeded_store, capsys
) -> None:
    assert _run("--db", str(seeded_store.path), "report") == 0

    assert SUM_LABEL in capsys.readouterr().out


def test_the_combined_total_sums_each_sites_own_day(
    no_config_anywhere, seeded_store, capsys
) -> None:
    """The arithmetic D14 chose: per-site local days, added up.

    The rejected alternative -- one shared window for every site -- would make
    each site's contribution here disagree with its own single-site section,
    which is why the sections are what this is checked against.
    """
    per_site = {}
    for site in seeded_store.sites:
        argv = ("--db", str(seeded_store.path), "--site", site, "report")
        assert _run(*argv) == 0
        section = capsys.readouterr().out
        per_site[site] = (
            _figure(section, "page views"),
            _figure(section, "events"),
        )

    assert _run("--db", str(seeded_store.path), "report") == 0
    combined = _combined(capsys.readouterr().out)

    assert _figure(combined, "page views") == sum(
        views for views, _ in per_site.values()
    )
    assert _figure(combined, "events") == sum(
        events for _, events in per_site.values()
    )


def test_a_single_site_prints_no_combined_total(
    no_config_anywhere, seeded_store, capsys
) -> None:
    # A sum of one is the section again. Printing it twice tells the reader
    # nothing the section did not already say.
    argv = ("--db", str(seeded_store.path), "--site", "mysite", "report")
    assert _run(*argv) == 0

    assert SUM_LABEL not in capsys.readouterr().out


def test_a_one_site_database_prints_no_combined_total(
    no_config_anywhere, store, make_row, capsys
) -> None:
    minute = iso_minute(floor_minute(utcnow()))
    store.save_site(
        "solo",
        property_id="123456789",
        display_name="solo.example",
        tz_name="UTC",
    )
    store.upsert_minutes("solo", [make_row(minute)])

    assert _run("--db", str(store.path), "report") == 0

    out = capsys.readouterr().out
    assert "solo" in out
    assert SUM_LABEL not in out


# --------------------------------------------------------------------------
# Timezones
# --------------------------------------------------------------------------


def test_one_warning_line_names_the_zones_when_they_differ(
    no_config_anywhere, seeded_store, capsys
) -> None:
    assert _run("--db", str(seeded_store.path), "report") == 0

    warnings = _lines_containing(capsys.readouterr().out, "warning")
    assert len(warnings) == 1
    for site in seeded_store.sites:
        assert seeded_store.tz_name(site) in warnings[0]


def test_no_warning_when_every_site_shares_a_zone(
    no_config_anywhere, seeded_store, capsys
) -> None:
    shared = seeded_store.tz_name("mysite")
    seeded_store.store.save_site(
        "clientb",
        property_id=seeded_store.property_id("clientb"),
        display_name="clientb.example",
        tz_name=shared,
    )

    assert _run("--db", str(seeded_store.path), "report") == 0

    out = capsys.readouterr().out
    assert SUM_LABEL in out
    assert _lines_containing(out, "warning") == []


def test_the_timezone_flag_forces_every_site_into_one_zone(
    no_config_anywhere, seeded_store, capsys
) -> None:
    # D16, and the flag's documented purpose: a debugging override that makes
    # two sites in different zones comparable for one run.
    argv = ("--db", str(seeded_store.path), "--timezone", "UTC", "report")
    assert _run(*argv) == 0

    out = capsys.readouterr().out
    for site in seeded_store.sites:
        assert seeded_store.tz_name(site) not in out
    assert _lines_containing(out, "warning") == []


def test_an_unknown_timezone_flag_is_a_config_error(
    no_config_anywhere, seeded_store, capsys
) -> None:
    argv = (
        "--db",
        str(seeded_store.path),
        "--timezone",
        "Mars/Olympus_Mons",
        "report",
    )
    assert _run(*argv) == 1

    assert "Mars/Olympus_Mons" in capsys.readouterr().err


# --------------------------------------------------------------------------
# --date
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", ["yesterday", "05/08/2026", "2026-13-40", "2026-08", ""]
)
def test_a_malformed_date_is_a_config_error(
    no_config_anywhere, seeded_store, capsys, value: str
) -> None:
    """Exit 1 with the format, not argparse's exit 2 with its own wording."""
    argv = ("--db", str(seeded_store.path), "report", "--date", value)
    assert _run(*argv) == 1

    error = capsys.readouterr().err
    assert "YYYY-MM-DD" in error
    assert "Traceback" not in error


def test_a_day_with_nothing_in_it_reports_zero(
    no_config_anywhere, seeded_store, capsys
) -> None:
    argv = (
        "--db",
        str(seeded_store.path),
        "--site",
        "mysite",
        "report",
        "--date",
        "2001-01-01",
    )
    assert _run(*argv) == 0

    out = capsys.readouterr().out
    assert "2001-01-01" in out
    assert _figure(out, "page views") == 0
    assert "nothing recorded" in out


# --------------------------------------------------------------------------
# Conversions
# --------------------------------------------------------------------------


def test_every_configured_conversion_is_listed_with_its_count(
    tmp_config, seeded_store, capsys
) -> None:
    site = "clientb"
    expected = seeded_store.store.day_totals(
        site,
        seeded_store.conversions(site),
        *seeded_store.bounds(site),
    )

    argv = (
        "--config",
        str(tmp_config),
        "--db",
        str(seeded_store.path),
        "--site",
        site,
        "report",
    )
    assert _run(*argv) == 0

    out = capsys.readouterr().out
    for name, count in expected.conversions.items():
        assert _figure(out, name) == count
    assert _figure(out, "conversions") == expected.conversions_total
    assert expected.conversions_total > 0


def test_a_conversion_that_never_fired_is_listed_at_zero(
    write_config, seeded_store, capsys
) -> None:
    config = write_config(UNFIRED_CONVERSION_CONFIG)

    argv = (
        "--config",
        str(config),
        "--db",
        str(seeded_store.path),
        "--site",
        "mysite",
        "report",
    )
    assert _run(*argv) == 0

    out = capsys.readouterr().out
    assert _figure(out, "newsletter_signup") == 0
    assert _figure(out, "generate_lead") > 0


def test_without_a_config_the_conversions_are_marked_unknown(
    no_config_anywhere, seeded_store, capsys
) -> None:
    """Nothing names them, so nothing is claimed about them.

    Printing 0 would be a lie in the one direction that matters: it reads as
    "no conversions today" when what happened is that no file said which
    events count as conversions.
    """
    assert _run("--db", str(seeded_store.path), "report") == 0

    lines = _conversion_lines(capsys.readouterr().out)
    assert lines
    assert all("n/a" in line for line in lines)


def test_the_combined_block_does_not_speak_of_one_site(
    no_config_anywhere, seeded_store, capsys
) -> None:
    """Under a heading reading "all sites", "this site's" names nothing.

    The two blocks share a figure line and cannot share its note: a section
    is one site, and the sum is every site at once.
    """
    assert _run("--db", str(seeded_store.path), "report") == 0

    out = capsys.readouterr().out
    section_note = _conversion_lines(out)[0]
    combined_note = _conversion_lines(_combined(out))[0]

    assert "this site's" in section_note
    assert "this site" not in combined_note
    assert "any site's" in combined_note


# --------------------------------------------------------------------------
# A config file the user named, versus one discovery went looking for
# --------------------------------------------------------------------------


def test_a_named_config_that_is_missing_stops_the_run(
    no_config_anywhere, seeded_store, capsys
) -> None:
    """§6, step 1: `--config PATH` errors if the file does not exist.

    `--db` makes the config unnecessary for the *numbers*, which is not a
    reason to ignore a path somebody typed: the run would then print `n/a`
    against conversions the named file may well have listed, and say the
    reason was that no config entry names them.
    """
    missing = no_config_anywhere / "does-not-exist.toml"

    argv = (
        "--config",
        str(missing),
        "--db",
        str(seeded_store.path),
        "report",
    )
    assert _run(*argv) == 1

    error = capsys.readouterr().err
    assert "does-not-exist.toml" in error
    assert "Traceback" not in error


def test_a_named_config_that_does_not_parse_stops_the_run(
    no_config_anywhere, write_config, seeded_store, capsys
) -> None:
    """The same rule for a file that exists and cannot be read."""
    config = write_config(BROKEN_CONFIG)

    argv = (
        "--config",
        str(config),
        "--db",
        str(seeded_store.path),
        "report",
    )
    assert _run(*argv) == 1

    error = capsys.readouterr().err
    assert "TOML" in error
    assert "Traceback" not in error


def test_a_broken_config_nobody_named_does_not_stop_the_run(
    no_config_anywhere, write_config, seeded_store, capsys
) -> None:
    """The other half of the rule, and the reason the swallow exists.

    A syntax error in a config file that merely happens to be in the working
    directory must not make a stored day unreadable: `--db` said where the
    rows are, so the file was only ever going to name conversions -- and
    `n/a` is an honest answer for that.
    """
    write_config(BROKEN_CONFIG, directory=no_config_anywhere)

    assert _run("--db", str(seeded_store.path), "report") == 0

    out = capsys.readouterr().out
    assert _conversion_lines(out)
    assert all("n/a" in line for line in _conversion_lines(out))


# --------------------------------------------------------------------------
# D17 -- the database decides which names are reportable
# --------------------------------------------------------------------------


def test_an_orphaned_site_is_reportable(
    write_config, seeded_store, capsys
) -> None:
    """In `site_meta`, absent from the config, and still readable."""
    config = write_config(ONE_SITE_CONFIG)

    argv = (
        "--config",
        str(config),
        "--db",
        str(seeded_store.path),
        "--site",
        "clientb",
        "report",
    )
    assert _run(*argv) == 0

    out = capsys.readouterr().out
    assert "clientb" in out
    assert seeded_store.property_id("clientb") in out
    assert _figure(out, "page views") > 0


def test_a_disabled_site_keeps_its_configured_conversions(
    write_config, seeded_store, capsys
) -> None:
    """`enabled = false` stops it being polled, not being read."""
    config = write_config(DISABLED_SITE_CONFIG)

    argv = (
        "--config",
        str(config),
        "--db",
        str(seeded_store.path),
        "--site",
        "clientb",
        "report",
    )
    assert _run(*argv) == 0

    out = capsys.readouterr().out
    assert "n/a" not in out
    assert _figure(out, "purchase") > 0
    assert _figure(out, "sign_up") > 0


def test_an_unknown_site_names_the_sites_the_database_has(
    no_config_anywhere, seeded_store, capsys
) -> None:
    argv = ("--db", str(seeded_store.path), "--site", "nope", "report")
    assert _run(*argv) == 1

    error = capsys.readouterr().err
    assert "nope" in error
    for site in seeded_store.sites:
        assert site in error
