"""The dashboard command: what a key does, what a pipe gets, what quits.

Four things are under test here, and none of them is "does it look right".

**The key table.** `Tab`/`n`, `p`, `1`-`9`, `r`, `q` and `Ctrl-C` are asserted
against a `Focus` ring with no terminal anywhere near it, because that is the
part a user notices being wrong. `Shift-Tab` gets its own test on `KeyReader`
itself: it arrives as a two-character extended sequence on Windows `msvcrt`
and is swallowed there, which is exactly why the spec leaves it unbound.

**The loop.** Driven directly, with a fake Live region and a scripted key
source, so a frame is produced per key press and the whole file finishes in
milliseconds. **Nothing here may wait out a refresh interval.** A dashboard
test that sleeps a real cycle is a bug in the test: every wait below is on an
Event something else sets, and `KEY_TICK` is shrunk to milliseconds by an
autouse fixture.

**The degradation paths.** Not a tty means print `report`'s output -- the same
text, byte for byte, that the `report` subcommand prints (§7.8) -- and exit 0
without a `Live` region ever being constructed; stdout that cannot encode
block glyphs forces `--ascii`; `SIGWINCH` is only ever named where it exists,
because merely naming it on Windows raises `AttributeError`.

**Quitting joins every poller thread.** Asserted through a fake `PollerSet`,
since the real one is `poller.py`'s to prove and what this command owes is
calling `stop()` on every path out -- including the one where the render
raised.
"""

import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Self

import pytest

from ga4_realtime import cli
from ga4_realtime.commands import live as live_module
from ga4_realtime.config import UiConfig
from ga4_realtime.errors import ConfigError
from ga4_realtime.keys import KeyReader
from ga4_realtime.logging_setup import log as package_log
from ga4_realtime.poller import PollStatus, PrimeResult
from ga4_realtime.ui import theme

# The two sites `tmp_config` and `seeded_store` agree about. Spelled out so a
# failure names the site rather than an index into a fixture.
SITES = ("mysite", "clientb")

# The middle row of §7.9's table: in the config, switched off. It is not
# polled and is not in the Tab rotation, and the pipe is then the only place
# its numbers can still be read -- which is the whole of why this path takes
# its list of sites from the database rather than from `enabled_sites()`.
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

# The bottom row of the same table: rows in the database, no block in the file
# at all. Nothing names its conversions, so its section says n/a rather than
# zero -- but the section is still printed.
ONE_SITE_CONFIG = """
    [defaults]
    credentials = "secrets/ga4-service-account.json"
    conversions = ["generate_lead"]

    [[sites]]
    name        = "mysite"
    property_id = "123456789"
"""


# --------------------------------------------------------------------------
# Fixtures and fakes
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fast_ticks(monkeypatch):
    """Milliseconds instead of tenths, for every loop in this file.

    `KEY_TICK` is how long the loop blocks between key polls when nothing has
    been pressed. Shrinking it is what keeps the one test that lets the key
    source run dry from costing a tenth of a second per iteration.
    """
    monkeypatch.setattr(live_module, "KEY_TICK", 0.001)


@pytest.fixture(autouse=True)
def restore_logger():
    """Leave the package logger exactly as it was found.

    `run` calls `ctx.start_logging()`, which attaches a rotating file handler
    pointed at this test's tmp_path. Left in place it writes the rest of the
    suite's lines there, and on Windows the open handle fails the directory's
    cleanup rather than the assertion.
    """
    before = list(package_log.handlers)
    level, propagate = package_log.level, package_log.propagate
    yield
    for handler in list(package_log.handlers):
        package_log.removeHandler(handler)
        if handler not in before:
            handler.close()
    for handler in before:
        package_log.addHandler(handler)
    package_log.setLevel(level)
    package_log.propagate = propagate


class FakeLive:
    """A Live region that records frames instead of painting them."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.frames: list = []
        self.entered = False

    def __enter__(self) -> Self:
        self.entered = True
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def update(self, renderable, refresh: bool = False) -> None:
        self.frames.append(renderable)


class ScriptedKeys:
    """A key source that hands back a written-down sequence, then quits.

    The trailing `q` is implicit rather than something each test remembers to
    add: a script that runs out would otherwise leave the loop waiting for a
    key that is never coming, and a test that hangs says far less about what
    broke than one that fails an assertion about the frames it got.
    """

    def __init__(self, keys) -> None:
        self.script = list(keys)
        self.polls = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def poll(self) -> str | None:
        self.polls += 1
        return self.script.pop(0) if self.script else "q"


class FakePollerSet:
    """`PollerSet`'s shape, with no threads and no API behind it."""

    def __init__(self, sites, db_path, control) -> None:
        self.sites = list(sites)
        self.db_path = Path(db_path)
        self.control = control
        self.stop_event = threading.Event()
        self.statuses = {site.name: PollStatus() for site in self.sites}
        self.primed = False
        self.started = False
        self.stops = 0

    def prime_all(self) -> dict:
        self.primed = True
        if self.control.prime_error is not None:
            raise self.control.prime_error
        if self.control.prime_results is not None:
            return dict(self.control.prime_results)
        return {
            site.name: PrimeResult(site=site.name, ok=True, rows=2)
            for site in self.sites
        }

    def start(self) -> None:
        self.started = True

    def stop(self, timeout: float | None = None) -> None:
        self.stops += 1
        self.stop_event.set()


@pytest.fixture
def pollers(monkeypatch):
    """Replace `PollerSet` in the command's namespace with a recorder.

    Patched on `commands.live` rather than on `poller`, because that is how
    the command reaches it: the module-level name is the seam, and patching
    the class at its origin would also change what every other module sees.
    """
    control = SimpleNamespace(
        made=[], prime_results=None, prime_error=None, last=None
    )

    def _factory(sites, db_path):
        pollers = FakePollerSet(sites, db_path, control)
        control.made.append(pollers)
        control.last = pollers
        return pollers

    monkeypatch.setattr(live_module, "PollerSet", _factory)
    return control


@pytest.fixture
def no_live(monkeypatch):
    """Make constructing a `Live` region fail the test that did it.

    The piped-stdout path's whole point is that this never happens: a Live
    region writes control sequences into whatever is reading, and a test that
    only checked the printed summary would still pass if one had been opened
    and immediately closed.
    """

    def _refuse(**kwargs):
        pytest.fail("a Live region was opened on a stdout that is not a tty")

    monkeypatch.setattr(live_module, "Live", _refuse)


@pytest.fixture
def on_a_terminal(monkeypatch):
    """Pretend stdout is a tty, and hand back the fakes the UI then uses.

    `signal` is faked along with the rest: on Linux the real call would
    install a process-wide SIGWINCH handler that outlives the test, and the
    fake is also how the `hasattr` guard gets asserted from either platform.
    """
    lives: list[FakeLive] = []
    keys = ScriptedKeys(["q"])
    signals = SimpleNamespace(SIGWINCH=28, calls=[])
    signals.signal = lambda *args: signals.calls.append(args)

    def _live(**kwargs):
        region = FakeLive(**kwargs)
        lives.append(region)
        return region

    monkeypatch.setattr(live_module, "stdout_is_a_terminal", lambda: True)
    monkeypatch.setattr(live_module, "Live", _live)
    monkeypatch.setattr(live_module, "KeyReader", lambda: keys)
    monkeypatch.setattr(live_module, "signal", signals)
    return SimpleNamespace(lives=lives, keys=keys, signal=signals)


@pytest.fixture
def run_live(seeded_store, tmp_config):
    """Run the dashboard end to end, against the seeded database.

    Through `cli.main` rather than by building a `Context` by hand: the exit
    code is half of what several of these tests assert, and the parser is
    where `--site` gets resolved to a configured, enabled site.
    """

    def _run(*argv: str, config: Path | None = None) -> int:
        return cli.main(
            [
                "--config",
                str(config or tmp_config),
                "--db",
                str(seeded_store.path),
                *argv,
            ]
        )

    return _run


def make_ui(**kwargs) -> UiConfig:
    fields = {
        "refresh": 10,
        "window": 6,
        "top_events": 10,
        "ascii": False,
        "color": "cyan",
        "conversion_color": "green",
    }
    fields.update(kwargs)
    return UiConfig(**fields)


def page_views(seeded_store, site: str) -> int:
    """What the store says the site's local day holds, as printed."""
    start, end = seeded_store.bounds(site)
    totals = seeded_store.store.day_totals(
        site, seeded_store.conversions(site), start, end
    )
    return totals.page_views


# --------------------------------------------------------------------------
# The key table
# --------------------------------------------------------------------------


def test_tab_advances_focus_and_wraps() -> None:
    focus = live_module.Focus(list(SITES))

    assert focus.name == "mysite"
    assert live_module.handle_key("\t", focus) == live_module.REDRAW
    assert focus.name == "clientb"
    # Wraps rather than stopping at the end: with two sites configured, Tab
    # is the only way back to the first one.
    assert live_module.handle_key("\t", focus) == live_module.REDRAW
    assert focus.name == "mysite"


@pytest.mark.parametrize("key", ["n", "N"])
def test_n_advances_like_tab(key: str) -> None:
    focus = live_module.Focus(list(SITES))

    assert live_module.handle_key(key, focus) == live_module.REDRAW
    assert focus.name == "clientb"


@pytest.mark.parametrize("key", ["p", "P"])
def test_p_moves_backwards_and_wraps(key: str) -> None:
    focus = live_module.Focus(["a", "b", "c"])

    assert live_module.handle_key(key, focus) == live_module.REDRAW
    assert focus.name == "c"
    assert live_module.handle_key(key, focus) == live_module.REDRAW
    assert focus.name == "b"


@pytest.mark.parametrize(("key", "expected"), [("1", "a"), ("3", "c")])
def test_digits_jump_to_a_site(key: str, expected: str) -> None:
    focus = live_module.Focus(["a", "b", "c"], "b")

    assert live_module.handle_key(key, focus) == live_module.REDRAW
    assert focus.name == expected


@pytest.mark.parametrize("key", ["4", "9"])
def test_a_digit_beyond_the_site_count_is_ignored(key: str) -> None:
    # Ignored rather than clamped to the last site: pressing 9 with three
    # configured is a mistake, and quietly showing site 3 would look like it
    # worked.
    focus = live_module.Focus(["a", "b", "c"])

    assert live_module.handle_key(key, focus) == live_module.IGNORE
    assert focus.name == "a"


def test_zero_is_not_a_site() -> None:
    # The binding is 1-9, one-based, because the status strip numbers nothing
    # and the sites are read off the config file in the order they are in it.
    focus = live_module.Focus(["a", "b"])

    assert live_module.handle_key("0", focus) == live_module.IGNORE
    assert focus.name == "a"


@pytest.mark.parametrize("key", ["q", "Q", "\x03"])
def test_q_and_ctrl_c_quit(key: str) -> None:
    # \x03 is Ctrl-C arriving as a character rather than as a signal, which is
    # what happens once the terminal is in cbreak mode on some platforms.
    focus = live_module.Focus(list(SITES))

    assert live_module.handle_key(key, focus) == live_module.QUIT


@pytest.mark.parametrize("key", ["r", "R"])
def test_r_forces_a_redraw_without_moving_focus(key: str) -> None:
    focus = live_module.Focus(list(SITES), "clientb")

    assert live_module.handle_key(key, focus) == live_module.REDRAW
    assert focus.name == "clientb"


@pytest.mark.parametrize("key", [None, "", "x", "\x00", "\xe0"])
def test_an_unbound_key_is_ignored(key) -> None:
    focus = live_module.Focus(list(SITES))

    assert live_module.handle_key(key, focus) == live_module.IGNORE
    assert focus.name == "mysite"


def test_focus_falls_back_to_the_first_site_for_an_unknown_name() -> None:
    # A render is the worst place to raise -- the exception costs the whole
    # display rather than one frame -- and `render_dashboard` makes the same
    # choice for the same reason.
    focus = live_module.Focus(list(SITES), "deleted")

    assert focus.name == "mysite"


# --------------------------------------------------------------------------
# KeyReader
# --------------------------------------------------------------------------


def test_shift_tab_is_swallowed_before_the_loop_sees_it(monkeypatch) -> None:
    """Two characters in, nothing out. Which is why it stays unbound.

    Shift-Tab arrives from `msvcrt` as `\\x00` followed by a payload byte, and
    `KeyReader` reads the payload and returns None so it cannot be mistaken
    for a command. Binding it would mean teaching the reader to assemble
    sequences, which is the beginning of reimplementing curses.
    """
    getwch = iter(["\x00", "\x0f"])
    fake = SimpleNamespace(kbhit=lambda: True, getwch=lambda: next(getwch))
    monkeypatch.setitem(sys.modules, "msvcrt", fake)

    reader = KeyReader(enabled=False)
    reader.enabled = True
    reader._win = True

    assert reader.poll() is None
    # Both characters consumed: a payload left in the buffer would be read as
    # a command on the very next poll.
    assert next(getwch, None) is None


def test_a_plain_key_comes_back_as_itself(monkeypatch) -> None:
    fake = SimpleNamespace(kbhit=lambda: True, getwch=lambda: "q")
    monkeypatch.setitem(sys.modules, "msvcrt", fake)

    reader = KeyReader(enabled=False)
    reader.enabled = True
    reader._win = True

    assert reader.poll() == "q"


def test_a_disabled_reader_never_polls_anything() -> None:
    # Which is the state it is in whenever stdin is not a tty -- the piped
    # case, and every test in this suite.
    with KeyReader(enabled=False) as reader:
        assert reader.enabled is False
        assert reader.poll() is None


def test_the_reader_is_disabled_when_stdin_is_not_a_tty(monkeypatch) -> None:
    """The piped case, asserted against `stdin` rather than against pytest.

    `sys.stdin` is replaced here rather than left to the capture pytest
    happens to be doing: with `-s` the real terminal is still attached and
    `enabled` is correctly True, so a test that relied on capture would be
    asserting the harness and would fail in a plain developer invocation.
    """
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))

    assert KeyReader().enabled is False


def test_the_reader_is_enabled_when_stdin_is_a_tty(monkeypatch) -> None:
    # The other half, and the reason the one above has to fake its stream:
    # `enabled` is a reading of the terminal, so both answers have to be
    # reachable from one test run.
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))

    assert KeyReader().enabled is True


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


def test_the_loop_redraws_for_every_key_and_stops_on_q() -> None:
    focus = live_module.Focus(["a", "b", "c"])
    region = FakeLive()
    stop = threading.Event()

    live_module.run_loop(
        region,
        ScriptedKeys(["\t", "\t", "q"]),
        focus,
        stop,
        lambda name: name,
        refresh=60,
    )

    # One frame per key, and the third one is the site the second Tab moved
    # to -- not a fourth frame after quitting.
    assert region.frames == ["a", "b", "c"]
    assert stop.is_set()


def test_the_loop_draws_nothing_when_the_stop_event_is_already_set() -> None:
    region = FakeLive()
    stop = threading.Event()
    stop.set()

    live_module.run_loop(
        region,
        ScriptedKeys([]),
        live_module.Focus(list(SITES)),
        stop,
        lambda name: name,
        refresh=60,
    )

    assert region.frames == []


def test_the_loop_ends_when_something_else_sets_the_event() -> None:
    """A poller set stopped from outside must end the loop, not the interval.

    The wait is on the Event rather than on `time.sleep`, so this returns as
    soon as the flag goes up. With a 60-second refresh, a loop that slept
    would still be here when the test timed out.
    """
    region = FakeLive()
    stop = threading.Event()

    class _Stops:
        def __init__(self) -> None:
            self.polls = 0

        def poll(self) -> None:
            self.polls += 1
            if self.polls >= 3:
                stop.set()

    live_module.run_loop(
        region,
        _Stops(),
        live_module.Focus(list(SITES)),
        stop,
        lambda name: name,
        refresh=60,
    )

    assert region.frames == ["mysite"]


def test_the_loop_renders_the_site_that_is_focused() -> None:
    seen: list[str] = []
    stop = threading.Event()

    live_module.run_loop(
        FakeLive(),
        ScriptedKeys(["2", "q"]),
        live_module.Focus(["a", "b", "c"]),
        stop,
        lambda name: seen.append(name) or name,
        refresh=60,
    )

    assert seen == ["a", "b"]


# --------------------------------------------------------------------------
# The ascii fallback
# --------------------------------------------------------------------------


def test_ascii_is_forced_when_stdout_cannot_encode_blocks(
    monkeypatch,
) -> None:
    monkeypatch.setattr(live_module, "stdout_supports_blocks", lambda: False)

    assert live_module.resolve_ascii(make_ui(ascii=False)).ascii is True


def test_a_capable_stdout_leaves_the_config_alone(monkeypatch) -> None:
    monkeypatch.setattr(live_module, "stdout_supports_blocks", lambda: True)
    ui = make_ui(ascii=False)

    # The same object, not an equal copy: nothing was decided, so nothing is
    # rebuilt.
    assert live_module.resolve_ascii(ui) is ui


def test_the_ascii_flag_is_never_asked_about(monkeypatch) -> None:
    """--ascii on the command line short-circuits the encoding check.

    Ordering, not I/O: `stdout_supports_blocks` only encodes two glyphs in
    memory, so the probe would be harmless -- but a user who has already
    said ascii has answered the question it asks, and the config comes back
    the same object, not a re-decided copy.
    """

    def _refuse() -> bool:
        pytest.fail("stdout was probed even though --ascii was given")

    monkeypatch.setattr(live_module, "stdout_supports_blocks", _refuse)
    ui = make_ui(ascii=True)

    assert live_module.resolve_ascii(ui) is ui


@pytest.mark.parametrize(
    ("encoding", "expected"),
    [("cp1252", False), ("cp437", False), ("utf-8", True)],
)
def test_stdout_supports_blocks_reads_the_real_encoding(
    monkeypatch, encoding: str, expected: bool
) -> None:
    """The deciding function, driven by encodings that really exist.

    Every other test in this section replaces `stdout_supports_blocks` with
    a lambda, which proves the wiring around it and nothing about it. The
    legacy Windows code pages are the case the fallback exists for; neither
    can encode the block or braille glyph the plots draw with.
    """
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(encoding=encoding))

    assert theme.stdout_supports_blocks() is expected


def test_a_stdout_with_no_encoding_counts_as_ascii(monkeypatch) -> None:
    # pythonw, or a replaced stream: no encoding attribute at all. Treated as
    # the most conservative answer rather than raising inside a probe.
    monkeypatch.setattr(sys, "stdout", SimpleNamespace())

    assert theme.stdout_supports_blocks() is False


def test_the_forced_ascii_reaches_the_frame(
    run_live, pollers, on_a_terminal, monkeypatch
) -> None:
    """The call site, not the resolver: `run` must use the answer it got.

    `resolve_ascii` is proved above, but only this notices the one line in
    `run` that applies it -- delete `ui = resolve_ascii(config.ui)` and
    every other test in this file stays green.
    """
    monkeypatch.setattr(live_module, "stdout_supports_blocks", lambda: False)
    seen: list[bool] = []
    real = live_module.render_dashboard

    def _record(store, sites, statuses, ui, ring, **kwargs):
        seen.append(ui.ascii)
        return real(store, sites, statuses, ui, ring, **kwargs)

    monkeypatch.setattr(live_module, "render_dashboard", _record)

    assert run_live() == 0
    assert seen and all(seen)


# --------------------------------------------------------------------------
# The piped-stdout path
# --------------------------------------------------------------------------


def test_the_piped_path_prints_exactly_what_report_prints(
    run_live, pollers, no_live, capsys
) -> None:
    """§7.8, byte for byte: one output, reached by two spellings.

    Not "looks similar". A script parsing `ga4-realtime report` has to parse
    `ga4-realtime | cat`, and the only thing that keeps that true through
    later edits is both spellings reaching the same renderer. Compared as
    whole strings rather than field by field, because every structural
    difference two renderers drift into -- the heading, the conversion block,
    the combined label and where its warning sits, the column widths, the
    order of the sections -- is one a field-by-field test would have been
    written not to notice.

    `run_live` is the argv builder, not the dashboard: `report` is a
    subcommand of the same parser, so the two runs differ in exactly the one
    word under test and read the same database. Priming is faked, so nothing
    is written between them.
    """
    assert run_live() == 0
    piped = capsys.readouterr().out

    assert run_live("report") == 0
    printed = capsys.readouterr().out

    assert piped == printed


def test_each_section_names_the_site_it_is_about(
    run_live, pollers, no_live, capsys
) -> None:
    """The config's own name for the site, not only the GA4 display name.

    A display name is whatever the property's owner typed: two properties may
    carry the same one, and neither has to resemble the key `--site` takes.
    In a stream of sections it is the site name that tells the reader which
    block belongs to which `[[sites]]` entry.
    """
    assert run_live() == 0

    lines = capsys.readouterr().out.splitlines()
    for site in SITES:
        assert any(line.startswith(f"{site} - ") for line in lines)


def test_a_disabled_site_still_prints_on_the_piped_path(
    run_live, pollers, no_live, write_config, capsys
) -> None:
    # D17: nothing you collected becomes unreachable because you edited a text
    # file. The site is not polled and not in the Tab rotation, so the pipe is
    # where its numbers still have to be readable.
    assert run_live(config=write_config(DISABLED_SITE_CONFIG)) == 0

    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith("clientb - ") for line in lines)


def test_an_orphaned_site_still_prints_on_the_piped_path(
    run_live, pollers, no_live, write_config, capsys
) -> None:
    # Deleted from the config entirely rather than switched off, which is the
    # same requirement one row further down §7.9's table.
    assert run_live(config=write_config(ONE_SITE_CONFIG)) == 0

    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith("clientb - ") for line in lines)


def test_piped_stdout_prints_every_site_and_exits_zero(
    run_live, pollers, no_live, seeded_store, capsys
) -> None:
    # Under pytest stdout is a capture object, so this is the piped path
    # without anything having to pretend.
    assert run_live() == 0

    out = capsys.readouterr().out
    for site in SITES:
        assert f"{site}.example" in out
        assert f"{page_views(seeded_store, site):,}" in out
    assert pollers.last.primed is True
    # Priming happened; the threads did not start, because there is no frame
    # for them to feed.
    assert pollers.last.started is False


def test_piped_stdout_honours_a_named_site(
    run_live, pollers, no_live, capsys
) -> None:
    assert run_live("--site", "clientb") == 0

    out = capsys.readouterr().out
    assert "clientb.example" in out
    assert "mysite.example" not in out


def test_site_all_prints_every_site(
    run_live, pollers, no_live, capsys
) -> None:
    assert run_live("--site", "all") == 0

    out = capsys.readouterr().out
    assert "mysite.example" in out
    assert "clientb.example" in out


def test_every_configured_conversion_is_listed_by_name(
    run_live, pollers, no_live, capsys
) -> None:
    assert run_live() == 0

    out = capsys.readouterr().out
    # clientb counts two, and a conversion that fired zero times still has to
    # appear: "not shown" and "did not happen" are different answers.
    for event in ("generate_lead", "purchase", "sign_up"):
        assert event in out


def test_the_combined_total_says_what_it_summed(
    run_live, pollers, no_live, seeded_store, capsys
) -> None:
    assert run_live() == 0

    out = capsys.readouterr().out
    assert "sum of each site's local day" in out
    total = sum(page_views(seeded_store, site) for site in SITES)
    assert f"{total:,}" in out


def test_mixed_timezones_are_called_out(
    run_live, pollers, no_live, capsys
) -> None:
    # The seeded database has mysite in Sao Paulo and clientb in Tokyo, so the
    # two sections cover different spans of time and the sum has to say so.
    assert run_live() == 0

    out = capsys.readouterr().out
    assert "Asia/Tokyo" in out
    assert "America/Sao_Paulo" in out
    assert "timezone" in out.lower()


def test_one_shared_timezone_does_not_warn(
    run_live, pollers, no_live, capsys
) -> None:
    """--timezone forces one zone, and then the sum really is one span.

    The zones come from `site_meta` rather than from the config, because this
    path reads the database -- so editing the file cannot make two sites
    agree, and `--timezone` is the documented override that can. That is what
    makes it the only way to ask this question of a seeded database.
    """
    assert run_live("--timezone", "Asia/Tokyo") == 0

    out = capsys.readouterr().out
    assert "sum of each site's local day" in out
    assert "timezones differ" not in out


def test_a_single_site_gets_no_combined_total(
    run_live, pollers, no_live, capsys
) -> None:
    # One section already is the total, and a "combined" line under it would
    # be the same numbers twice.
    assert run_live("--site", "mysite") == 0

    assert "sum of each site's local day" not in capsys.readouterr().out


# --------------------------------------------------------------------------
# Priming, and what it prints
# --------------------------------------------------------------------------


def test_a_failing_site_is_diagnosed_before_the_ui(
    run_live, pollers, no_live, capsys
) -> None:
    pollers.prime_results = {
        "mysite": PrimeResult(site="mysite", ok=True, rows=2),
        "clientb": PrimeResult(
            site="clientb", ok=False, error="site 'clientb': no such key file"
        ),
    }

    assert run_live() == 0

    captured = capsys.readouterr()
    # On stderr: `ga4-realtime | cat` is a documented way to get the day's
    # numbers, and a diagnosis in the middle of them is one more thing for
    # whatever is reading to have to skip.
    assert "no such key file" in captured.err
    assert "no such key file" not in captured.out


def test_a_warning_is_printed_and_the_site_still_runs(
    run_live, pollers, no_live, capsys
) -> None:
    pollers.prime_results = {
        "mysite": PrimeResult(
            site="mysite", ok=True, rows=2, warning="falling back to UTC"
        ),
        "clientb": PrimeResult(site="clientb", ok=True, rows=2),
    }

    assert run_live() == 0

    captured = capsys.readouterr()
    assert "falling back to UTC" in captured.err
    assert "mysite.example" in captured.out


def test_every_site_failing_exits_one(
    run_live, pollers, no_live, capsys
) -> None:
    # `prime_all` is the one that decides this, and it raises; what the
    # command owes is not swallowing it.
    pollers.prime_error = ConfigError("every configured site failed to start")

    assert run_live() == 1
    assert "every configured site failed" in capsys.readouterr().err


# --------------------------------------------------------------------------
# The dashboard itself
# --------------------------------------------------------------------------


def test_quitting_joins_every_poller(run_live, pollers, on_a_terminal) -> None:
    assert run_live() == 0

    assert pollers.last.started is True
    assert pollers.last.stops == 1
    assert on_a_terminal.lives[0].frames  # at least one frame was painted


def test_the_pollers_are_stopped_even_when_the_render_raises(
    run_live, pollers, on_a_terminal, monkeypatch
) -> None:
    """The join is in a `finally`, so a bug in the layout still hands back
    the terminal and the threads."""

    def _explode(*args, **kwargs):
        raise RuntimeError("layout arithmetic went wrong")

    monkeypatch.setattr(live_module, "render_dashboard", _explode)

    with pytest.raises(RuntimeError):
        run_live()

    assert pollers.last.stops == 1


def test_ctrl_c_as_a_signal_exits_130_and_still_stops_the_pollers(
    run_live, pollers, on_a_terminal, monkeypatch
) -> None:
    """Section 4's table makes no exception for the dashboard: 130.

    `q` -- and Ctrl-C delivered as the `\\x03` character -- is the exit-0
    quit; the signal form is an interruption, and it owes the same shutdown
    with the exit code that says so. The KeyboardInterrupt is raised from
    the render, which is where one lands when the signal arrives mid-frame.
    """

    def _interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(live_module, "render_dashboard", _interrupt)

    assert run_live() == 130
    assert pollers.last.stops == 1


def test_tab_moves_the_frame_to_the_next_site(
    run_live, pollers, on_a_terminal
) -> None:
    on_a_terminal.keys.script = ["\t", "q"]

    assert run_live() == 0

    # Two frames, and they are different objects because they are two
    # different sites' days.
    assert len(on_a_terminal.lives[0].frames) == 2


def test_the_dashboard_opens_on_the_site_that_was_asked_for(
    run_live, pollers, on_a_terminal, monkeypatch
) -> None:
    seen: list[str] = []
    real = live_module.render_dashboard

    def _record(*args, **kwargs):
        seen.append(kwargs["focus"])
        return real(*args, **kwargs)

    monkeypatch.setattr(live_module, "render_dashboard", _record)

    assert run_live("--site", "clientb") == 0
    assert seen[0] == "clientb"


def test_sigwinch_is_only_touched_where_it_exists(
    run_live, pollers, on_a_terminal
) -> None:
    assert run_live() == 0

    assert [call[0] for call in on_a_terminal.signal.calls] == [
        on_a_terminal.signal.SIGWINCH
    ]


def test_a_platform_without_sigwinch_is_left_alone(
    run_live, pollers, on_a_terminal
) -> None:
    # Windows, where merely naming signal.SIGWINCH raises AttributeError --
    # which is why the guard is `hasattr` and not a try/except around the
    # handler installation.
    del on_a_terminal.signal.SIGWINCH

    assert run_live() == 0
    assert on_a_terminal.signal.calls == []


def test_the_live_region_does_not_take_the_screen(
    run_live, pollers, on_a_terminal
) -> None:
    # screen=False keeps the frame in the scrollback, which is what makes the
    # last frame still readable after quitting -- and is also why the height
    # budget exists at all.
    assert run_live() == 0

    kwargs = on_a_terminal.lives[0].kwargs
    assert kwargs["screen"] is False
    assert kwargs["auto_refresh"] is False
