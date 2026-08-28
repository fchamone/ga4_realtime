"""What the bell promises: one tone per rise, and never a failed poll.

Two halves, tested apart because they fail in different ways.

`ConversionWatch` is arithmetic, and the failure it exists to prevent is
noise: every poll re-reads the same ten minutes, so anything that treats a
conversion row as an event would ring for the same lead once a cycle until it
scrolled out of the window. Every test below that folds twice is there to hold
that line.

`Bell` is a side effect, and its failure is the opposite one -- a beep that
costs a poll. It is exercised through injected `beep` and `spawn` callables
rather than a real thread and a real sound card: the suite has to be silent,
CI has no audio device, and a test that joined a ringing thread would be
asserting on the scheduler rather than on the code.

Nothing here uses a real minute key from the clock. The keys are literals in
the storage format (`iso_minute`), because what the watch does with them is
string comparison and the readable thing in a failure is the minute itself.
"""

import logging
import sys
import types

import pytest

from ga4_realtime import bell as bell_module
from ga4_realtime.bell import (
    DEFAULT_DURATION,
    DEFAULT_FREQUENCY,
    Bell,
    ConversionWatch,
    beep_tone,
)

# Three consecutive minutes in the storage format, oldest first. Written out
# rather than generated so a failure names the minute a rise was seen in.
FIRST = "2026-08-28T09:00:00Z"
SECOND = "2026-08-28T09:01:00Z"
THIRD = "2026-08-28T09:02:00Z"

# The events an editor's own hooks are most likely to be beeping at already.
# The whole reason this feature has a tone rather than a BEL is that it must
# not be mistaken for one of these.
HOOK_TONES = (800, 1000)


@pytest.fixture
def sounds() -> list[tuple[int, int]]:
    """Every (frequency, duration) the bell asked for, in order."""
    return []


@pytest.fixture
def make_bell(sounds):
    """A `Bell` that records instead of sounding, and rings on this thread.

    `spawn` runs the work inline, which is what makes every assertion below a
    plain equality rather than a wait: the real spawn is a daemon thread, and
    the tests that care about *that* are the two that pass their own.
    """

    def _make(**kwargs) -> Bell:
        kwargs.setdefault("beep", lambda f, d: sounds.append((f, d)))
        kwargs.setdefault("spawn", lambda work: work())
        return Bell(**kwargs)

    return _make


# --------------------------------------------------------------------------
# The tone
# --------------------------------------------------------------------------


def test_the_default_tone_is_not_one_an_editor_hook_already_uses():
    """The point of the feature, as an assertion.

    A conversion has to be distinguishable from "the agent finished" and "the
    agent wants you", which on this author's machine are 800 Hz and 1000 Hz.
    A third alert at either pitch would be information nobody can act on.
    """
    assert DEFAULT_FREQUENCY not in HOOK_TONES


def test_a_ring_asks_for_the_configured_tone(make_bell, sounds):
    make_bell(frequency=440, duration=90).ring()
    assert sounds == [(440, 90)]


def test_the_defaults_are_what_an_unconfigured_bell_rings(make_bell, sounds):
    make_bell().ring()
    assert sounds == [(DEFAULT_FREQUENCY, DEFAULT_DURATION)]


def test_a_disabled_bell_never_reaches_the_beeper(make_bell, sounds):
    make_bell(enabled=False).ring()
    assert sounds == []


# --------------------------------------------------------------------------
# What a ring may never do
# --------------------------------------------------------------------------


def test_a_ring_arriving_while_one_sounds_is_dropped_not_queued(sounds):
    """Twelve sites converting at once are one audible event, not twelve.

    The held `spawn` stands in for a beep still sounding: the first ring's
    work is captured rather than run, so the second arrives while the permit
    is still out.
    """
    held: list = []
    bell = Bell(beep=lambda f, d: sounds.append((f, d)), spawn=held.append)

    bell.ring()
    bell.ring()
    assert len(held) == 1

    # And once the first has finished, the bell is available again.
    held[0]()
    bell.ring()
    assert len(held) == 2


def test_a_beeper_that_raises_goes_quiet_for_the_run_and_says_so(
    make_bell, sounds, caplog
):
    """No audio device is a permanent condition, not a per-lead surprise."""

    def explode(frequency, duration):
        raise OSError("no audio device")

    bell = make_bell(beep=explode)
    with caplog.at_level(logging.WARNING):
        bell.ring()
    assert "no audio device" in caplog.text
    assert "[bell] enabled = false" in caplog.text

    # Silenced, and silently so: the second lead of the day must not put a
    # second warning in the log panel the user is trying to read.
    caplog.clear()
    bell.ring()
    assert caplog.text == ""

    # And a working beeper afterwards is still not consulted.
    bell._beep = lambda f, d: sounds.append((f, d))
    bell.ring()
    assert sounds == []


def test_a_spawn_that_cannot_start_a_thread_does_not_raise_at_the_caller(
    make_bell, caplog
):
    """`ring()` is called inside `Poller.poll_once`, which must not fail."""

    def refuse(work):
        raise RuntimeError("can't start new thread")

    bell = make_bell(spawn=refuse)
    with caplog.at_level(logging.WARNING):
        bell.ring()  # must not raise
    assert "can't start new thread" in caplog.text


def test_the_default_spawn_is_a_daemon_thread_nobody_joins(monkeypatch):
    """A beep must never be the reason Ctrl-C waits."""
    made: list = []

    class FakeThread:
        def __init__(self, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon
            made.append(self)

        def start(self):
            self.target()

    monkeypatch.setattr(bell_module.threading, "Thread", FakeThread)
    beeps: list = []
    Bell(beep=lambda f, d: beeps.append((f, d))).ring()

    assert len(made) == 1
    assert made[0].daemon is True
    assert made[0].name == "ga4-bell"
    assert beeps == [(DEFAULT_FREQUENCY, DEFAULT_DURATION)]


# --------------------------------------------------------------------------
# What actually makes the sound, on each platform
# --------------------------------------------------------------------------


def test_windows_gets_a_real_tone_from_winsound(monkeypatch):
    """Asserted from any platform: `winsound` is supplied, not imported."""
    calls: list = []
    fake = types.ModuleType("winsound")
    fake.Beep = lambda frequency, duration: calls.append((frequency, duration))
    monkeypatch.setitem(sys.modules, "winsound", fake)
    monkeypatch.setattr(bell_module.sys, "platform", "win32")

    beep_tone(1200, 150)
    assert calls == [(1200, 150)]


def test_elsewhere_the_alert_is_bel_on_stderr(monkeypatch, capsys):
    """stdout belongs to the Live region; a byte in a frame tears it."""
    monkeypatch.setattr(bell_module.sys, "platform", "linux")

    beep_tone(1200, 150)
    captured = capsys.readouterr()
    assert captured.err == "\a"
    assert captured.out == ""


# --------------------------------------------------------------------------
# What counts as new
# --------------------------------------------------------------------------


def test_the_first_fold_is_a_baseline_and_reports_nothing(make_row):
    """Priming reads up to half an hour of history. None of it is news."""
    watch = ConversionWatch(["generate_lead"])
    rows = [make_row(FIRST, "generate_lead", events=3)]
    assert watch.new_events(rows) == {}


def test_the_same_minute_re_read_is_a_correction_not_a_conversion(make_row):
    """The failure this whole class exists to prevent."""
    watch = ConversionWatch(["generate_lead"])
    rows = [make_row(FIRST, "generate_lead", events=1)]
    watch.new_events(rows)
    assert watch.new_events(rows) == {}
    assert watch.new_events(rows) == {}


def test_a_lead_in_a_new_minute_is_reported_once(make_row):
    watch = ConversionWatch(["generate_lead"])
    watch.new_events([make_row(FIRST, "generate_lead", events=1)])
    risen = watch.new_events(
        [
            make_row(FIRST, "generate_lead", events=1),
            make_row(SECOND, "generate_lead", events=1),
        ]
    )
    assert risen == {"generate_lead": 1}


def test_only_the_rise_inside_a_minute_is_counted(make_row):
    """A minute that goes from one lead to three is two new leads, not three."""
    watch = ConversionWatch(["generate_lead"])
    watch.new_events([make_row(FIRST, "generate_lead", events=1)])
    risen = watch.new_events([make_row(FIRST, "generate_lead", events=3)])
    assert risen == {"generate_lead": 2}


def test_the_fan_out_of_one_minute_is_summed_not_counted(make_row):
    """One minute reaches the watch once per device and country."""
    watch = ConversionWatch(["generate_lead"])
    watch.new_events([make_row(FIRST, "page_view", events=1)])
    risen = watch.new_events(
        [
            make_row(FIRST, "page_view", events=1),
            make_row(SECOND, "generate_lead", device="desktop", events=1),
            make_row(SECOND, "generate_lead", device="mobile", events=1),
        ]
    )
    assert risen == {"generate_lead": 2}


def test_every_configured_conversion_is_watched_and_named(make_row):
    watch = ConversionWatch(["purchase", "sign_up"])
    watch.new_events([make_row(FIRST, "page_view")])
    risen = watch.new_events(
        [
            make_row(FIRST, "page_view"),
            make_row(SECOND, "purchase", events=2),
            make_row(SECOND, "sign_up", events=1),
        ]
    )
    assert risen == {"purchase": 2, "sign_up": 1}


def test_an_event_that_is_not_a_conversion_is_ignored(make_row):
    """`page_view` rises every minute of every day. It is not an alert."""
    watch = ConversionWatch(["generate_lead"])
    watch.new_events([make_row(FIRST, "page_view", events=4)])
    assert watch.new_events([make_row(SECOND, "page_view", events=90)]) == {}


def test_a_site_with_no_conversions_reports_nothing_at_all(make_row):
    """Not reachable from a config -- `conversions` is required -- but the
    class is constructible with an empty list and must not ring on one."""
    watch = ConversionWatch([])
    watch.new_events([make_row(FIRST, "generate_lead")])
    assert watch.new_events([make_row(SECOND, "generate_lead")]) == {}


def test_a_correction_downwards_is_not_a_rise_and_is_remembered(make_row):
    """GA4 restating a minute lower must not ring, and must not arm a ring."""
    watch = ConversionWatch(["generate_lead"])
    watch.new_events([make_row(FIRST, "generate_lead", events=3)])
    assert watch.new_events([make_row(FIRST, "generate_lead", events=1)]) == {}
    # Back up to 3 is two new leads against the corrected 1, not none against
    # the original 3.
    risen = watch.new_events([make_row(FIRST, "generate_lead", events=3)])
    assert risen == {"generate_lead": 2}


def test_a_poll_that_came_back_empty_keeps_the_baseline(make_row):
    """An empty response is "nothing right now", not "start again".

    Dropping the baseline here would make the next poll's re-read of the same
    ten minutes look like a fresh conversion in every one of them.
    """
    watch = ConversionWatch(["generate_lead"])
    rows = [make_row(FIRST, "generate_lead", events=1)]
    watch.new_events(rows)
    assert watch.new_events([]) == {}
    assert watch.new_events(rows) == {}


def test_minutes_that_left_the_api_window_are_forgotten(make_row):
    """State is bounded by the window, not by how long the process ran.

    Asserted on the private dict deliberately: the alternative is a public
    accessor that only a test would ever call, which is the same trade the
    store makes by having no `poll_log` reader.
    """
    watch = ConversionWatch(["generate_lead"])
    watch.new_events(
        [
            make_row(FIRST, "generate_lead"),
            make_row(SECOND, "generate_lead"),
        ]
    )
    assert len(watch._seen) == 2

    # The next poll's window starts at THIRD: FIRST and SECOND are further
    # back than the Realtime API can now answer for.
    watch.new_events([make_row(THIRD, "generate_lead")])
    assert list(watch._seen) == [(THIRD, "generate_lead")]
