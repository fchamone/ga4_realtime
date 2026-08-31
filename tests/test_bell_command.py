"""What `ga4-realtime bell` promises: the configured tone, heard on demand.

The bell's own arithmetic is `tests/test_bell.py`. This file is about the
command that exists because the dashboard only rings when a real conversion
arrives -- the one moment nobody wants to discover that the machine has no
audio device, or that the terminal was set to a visual bell.

Four things are pinned.

**It plays the configured tone, on this thread.** The frequency and duration
come out of `[bell]` and reach `beep_tone` unchanged. Going through `Bell`
instead would schedule the note on a daemon thread and return before it
sounded, and a short-lived command exits and kills that thread -- so the
structural test below forbids naming `Bell` here at all.

**It plays even when `[bell] enabled` is false**, and says so. A command for
hearing the bell that answered a switched-off bell with silence would be
indistinguishable from the failure it exists to find.

**A tone that will not play is a finding, not a traceback.** Exit 1, the
exception named in the report, and `cli.main` never sees it.

**It polls nothing and writes nothing.** No API call, no database, no log --
a directory listing before and after, the way `test_doctor.py` pins the same
promise for the other read-only command.
"""

import ast
import sys
from pathlib import Path

import pytest

from ga4_realtime import cli
from ga4_realtime.commands import bell as bell_command

# Three sites, so the listing has something to line up: two polled with
# different conversion lists, and one switched off -- which is the answer
# somebody hunting a bell that never rings most needs to be shown.
THREE_SITE_CONFIG = """
    [defaults]
    credentials = "secrets/ga4-service-account.json"
    conversions = ["generate_lead"]

    [storage]
    database = "out/ga4_realtime.db"

    [[sites]]
    name        = "mysite"
    property_id = "123456789"

    [[sites]]
    name        = "clientb"
    property_id = "987654321"
    conversions = ["purchase", "sign_up"]

    [[sites]]
    name        = "paused"
    property_id = "555000111"
    enabled     = false
"""

# A tone nothing else on the machine is playing, and deliberately not the
# 1200 Hz default: a test asserting the default would pass against a command
# that ignored the file entirely.
CUSTOM_BELL_CONFIG = """
    [defaults]
    credentials = "secrets/ga4-service-account.json"
    conversions = ["generate_lead"]

    [bell]
    frequency = 440
    duration  = 250

    [[sites]]
    name        = "mysite"
    property_id = "123456789"
"""

SILENCED_BELL_CONFIG = """
    [defaults]
    credentials = "secrets/ga4-service-account.json"
    conversions = ["generate_lead"]

    [bell]
    enabled   = false
    frequency = 440
    duration  = 250

    [[sites]]
    name        = "mysite"
    property_id = "123456789"
"""


@pytest.fixture
def sounds(monkeypatch) -> list[tuple[int, int]]:
    """Every (frequency, duration) the command asked for, in order.

    Patched on the command module rather than on `ga4_realtime.bell`, because
    that is where the name the command calls is bound: `from ..bell import
    beep_tone` copies the function into this module's namespace at import,
    and patching the origin alone would leave the real one being called --
    which on CI means a beep nobody hears and on a laptop means one nobody
    asked for.
    """
    played: list[tuple[int, int]] = []
    monkeypatch.setattr(
        bell_command,
        "beep_tone",
        lambda frequency, duration: played.append((frequency, duration)),
    )
    return played


@pytest.fixture
def three_sites(write_config) -> Path:
    return write_config(THREE_SITE_CONFIG)


def _bell(config: Path, *argv: str) -> int:
    return cli.main(["--config", str(config), "bell", *argv])


# --------------------------------------------------------------------------
# The tone
# --------------------------------------------------------------------------


def test_it_plays_the_configured_tone_once(sounds, write_config, capsys):
    config = write_config(CUSTOM_BELL_CONFIG)

    assert _bell(config) == 0

    assert sounds == [(440, 250)]


def test_the_report_names_the_tone_it_played(sounds, write_config, capsys):
    config = write_config(CUSTOM_BELL_CONFIG)

    _bell(config)

    out = capsys.readouterr().out
    assert "440 Hz" in out
    assert "250 ms" in out


def test_the_report_names_the_config_file_the_tone_came_from(
    sounds, three_sites, capsys
):
    # The whole claim of the command is that this is the *configured* tone,
    # and there are three places the file could have come from.
    assert _bell(three_sites) == 0

    assert str(three_sites) in capsys.readouterr().out


def test_a_silenced_bell_still_plays_and_says_the_dashboard_will_not(
    sounds, write_config, capsys
):
    """`enabled = false` is reported, not obeyed, and the exit code stays 0.

    Obeying it would answer a switched-off bell with silence, which is
    exactly what a broken bell sounds like. The state is printed instead --
    the same reading `doctor` gives a site with `enabled = false`: a choice,
    not a problem.
    """
    config = write_config(SILENCED_BELL_CONFIG)

    assert _bell(config) == 0

    assert sounds == [(440, 250)]
    assert "enabled = false" in capsys.readouterr().out


def test_a_tone_that_will_not_play_is_reported_and_exits_one(
    monkeypatch, three_sites, capsys
):
    def no_sound(frequency: int, duration: int) -> None:
        raise RuntimeError("no audio device")

    monkeypatch.setattr(bell_command, "beep_tone", no_sound)

    assert _bell(three_sites) == 1

    out = capsys.readouterr().out
    assert "RuntimeError" in out
    assert "no audio device" in out


def test_a_tone_that_will_not_play_says_the_dashboard_survives_it(
    monkeypatch, three_sites, capsys
):
    """The finding is not "your tool is broken", and saying so is the point.

    A bell that cannot ring logs one warning, goes quiet for the rest of the
    run and never fails the poll it was announcing -- so somebody who has
    just been told the tone will not play needs to know that collection is
    unaffected, and where to switch it off if they would rather it stopped
    being tried.
    """

    def no_sound(frequency: int, duration: int) -> None:
        raise OSError("no such device")

    monkeypatch.setattr(bell_command, "beep_tone", no_sound)

    assert _bell(three_sites) == 1

    out = capsys.readouterr().out
    assert "never fails the poll" in out
    assert "[bell] enabled = false" in out


def test_windows_is_told_the_tone_is_exact(monkeypatch, three_sites, capsys):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(bell_command, "beep_tone", lambda f, d: None)

    assert _bell(three_sites) == 0

    assert "winsound" in capsys.readouterr().out


def test_elsewhere_the_report_says_the_tone_keys_do_nothing(
    monkeypatch, three_sites, capsys
):
    """The line that stops a Linux user tuning a frequency nothing reads.

    `beep_tone` falls back to BEL off Windows, so `frequency` and `duration`
    are accepted by the config and used by nobody. Saying so here is cheaper
    than the hour spent editing them -- and the terminal's own visual-bell
    setting, which is the other way to hear nothing, is named with it.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(bell_command, "beep_tone", lambda f, d: None)

    assert _bell(three_sites) == 0

    out = capsys.readouterr().out
    assert "BEL" in out
    assert "unused" in out
    assert "visual bell" in out


# --------------------------------------------------------------------------
# What rings it
# --------------------------------------------------------------------------


def test_it_lists_every_site_and_its_conversions(sounds, three_sites, capsys):
    assert _bell(three_sites) == 0

    out = capsys.readouterr().out
    assert "generate_lead" in out
    assert "purchase, sign_up" in out


def test_a_disabled_site_is_listed_as_one_that_never_rings(
    sounds, three_sites, capsys
):
    # Listed rather than filtered out: "this site is not polled" is the
    # answer, and dropping the row would leave the user to notice a name
    # that is not there.
    assert _bell(three_sites) == 0

    out = capsys.readouterr().out
    assert "paused" in out
    assert "never rings" in out


def test_site_narrows_the_listing_to_one(sounds, three_sites, capsys):
    assert _bell(three_sites, "--site", "clientb") == 0

    out = capsys.readouterr().out
    assert "purchase, sign_up" in out
    assert "generate_lead" not in out


def test_site_all_is_the_same_as_omitting_it(sounds, three_sites, capsys):
    assert _bell(three_sites, "--site", "all") == 0
    every = capsys.readouterr().out

    assert _bell(three_sites) == 0
    assert capsys.readouterr().out == every


def test_an_unknown_site_names_the_ones_that_would_have_worked(
    sounds, three_sites, capsys
):
    assert _bell(three_sites, "--site", "clientc") == 1

    error = capsys.readouterr().err
    assert "mysite" in error
    assert "clientb" in error


def test_an_unknown_site_fails_before_anything_is_played(
    sounds, three_sites, capsys
):
    # The order is the point: a beep followed by an error leaves the user
    # deciding whether the tone they just heard was the configured one.
    assert _bell(three_sites, "--site", "clientc") == 1

    assert sounds == []


# --------------------------------------------------------------------------
# Nothing is polled, and nothing is written
# --------------------------------------------------------------------------


def test_it_creates_no_file(sounds, three_sites, config_home, capsys):
    before = _snapshot(config_home)

    assert _bell(three_sites) == 0

    assert _snapshot(config_home) == before
    # Not even the directory the database and the log would have gone in.
    assert not (config_home / "out").exists()


def test_it_names_neither_the_store_nor_the_api_nor_the_log(sounds):
    """Read as an AST, so the docstring may still explain the rule.

    Three names would each break a promise in the module docstring by being
    called at all: `RealtimeStore` creates the file it is handed,
    `start_logging` opens the rotating log, and `GA4Client` would turn a
    command about a sound into a command that needs credentials and a
    network. A substring search would trip over the explanation of why.
    """
    used = _names_used(bell_command)

    assert "RealtimeStore" not in used
    assert "start_logging" not in used
    assert "GA4Client" not in used


def test_it_never_rings_through_the_poller_s_bell(sounds):
    """`Bell` schedules a tone on a daemon thread and returns.

    That is right inside a poll, where the note may not block the thread, and
    wrong in a command that exits immediately afterwards: the process would
    end while the note was still being scheduled, and the one thing the user
    asked for is the one thing they would not get. `beep_tone` on this thread
    is the whole difference, and it is structural rather than visible in the
    output, so it is pinned here.
    """
    used = _names_used(bell_command)

    assert "Bell" not in used
    assert "beep_tone" in used


def _names_used(module) -> set[str]:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            used |= {alias.name for alias in node.names}
    return used


def _snapshot(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))
