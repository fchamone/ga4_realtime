"""The live dashboard: prime every site, then poll and render until quit.

The bare invocation, and the only command that needs the API. It primes every
enabled site concurrently while stdout is still a plain terminal, so every
credential, permission and timezone failure gets a readable paragraph before
the Live region takes the screen over; then, if stdout is *not* a terminal, it
hands over to `report` and exits rather than emitting control sequences into
whatever is reading.

**The pipe gets `report`, by calling it (§5, §7.8).** Not a second renderer
that resembles it: `ga4-realtime | cat` and `ga4-realtime report` are
documented to be the same text, so a script parsing one parses the other, and
two implementations of "the same text" is exactly the pair that drifts. It is
also what makes a disabled or orphaned site still reachable through the pipe
-- `report` takes its list of sites from `site_meta` rather than from the
config's enabled list, which is D17 and nothing this module should be
deciding for itself.

**One site is on screen at a time (D4), and `Focus` is which.** Everything
day-scoped in a frame belongs to the focused site's local day, so moving the
focus moves the day bounds with it; the header's timezone and clock are what
make that legible when Tab crosses from a site at 23:40 to one already at
04:40 the next morning. Every *other* site still reaches the frame as a marker
in the status strip and as its own lines in the --verbose log panel, which is
what stops a site nobody has pressed Tab to from failing quietly.

**The loop is separate from what runs it.** `run_loop` takes a Live region, a
key source and a render callable, so the key table can be exercised at full
speed against fakes: a test that waited out a refresh interval to see whether
Tab worked would be slow when it passed and flaky when the machine was busy.
"""

import shutil
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any, Protocol

from rich.console import Console, RenderableType
from rich.live import Live

from ..config import SiteConfig, UiConfig
from ..keys import KeyReader
from ..logging_setup import RingBufferHandler, log
from ..poller import PollerSet, PrimeResult
from ..store import RealtimeStore
from ..ui.dashboard import render_dashboard
from ..ui.theme import no_color_requested, stdout_supports_blocks
from . import Context, report

# What one key press asks the loop to do next. Three words rather than a
# tuple of booleans, because "moved the focus" and "wants a frame" are the
# same answer -- every binding that changes what is on screen redraws now
# instead of waiting out the rest of the refresh interval.
QUIT = "quit"
REDRAW = "redraw"
IGNORE = "ignore"

# `q` quits with exit 0, and so does Ctrl-C arriving as a character rather
# than as a signal -- which is what a terminal in cbreak mode can deliver.
# The signal form is deliberately NOT caught anywhere in this command: it
# unwinds through the `finally` that joins the poller threads and reaches
# cli.main as the KeyboardInterrupt that section 4 maps to exit 130, a table
# with no exception for the dashboard.
QUIT_KEYS = frozenset({"q", "Q", "\x03"})
NEXT_KEYS = frozenset({"\t", "n", "N"})
PREV_KEYS = frozenset({"p", "P"})
REDRAW_KEYS = frozenset({"r", "R"})
# One-based, and 0 is not a site: the config file's order is what the digits
# index, and that order starts at the first block in the file. A set rather
# than the string "123456789", where `in` is a substring test and a key that
# somehow arrived as two characters would match.
JUMP_KEYS = frozenset("123456789")

# How long the loop blocks between key polls while it waits out a refresh
# interval. It is a wait on the stop event rather than a sleep, so a poller
# set stopped from elsewhere ends the loop immediately; the tenth of a second
# is what keeps a key press from feeling delayed without spinning a core.
KEY_TICK = 0.1

# What to assume the terminal is when it will not say. Only reached when
# stdout is a terminal the OS cannot measure, since the not-a-tty case has
# already returned by then.
TERMINAL_FALLBACK = (100, 30)


class LiveRegion(Protocol):
    """What the loop needs from a `rich.live.Live`: somewhere to put a frame.

    A structural type for the same reason `dashboard.StatusLike` is one: what
    the loop actually depends on is one method, and saying so is what lets it
    be driven by a recorder in a test instead of by a terminal.
    """

    def update(self, renderable: Any, refresh: bool = False) -> None: ...


class KeySource(Protocol):
    """One keystroke if there is one, None if there is not. Never blocks."""

    def poll(self) -> str | None: ...


class Focus:
    """Which site is on screen, and the ring the key table moves it around.

    Holds names rather than `SiteConfig` objects: the frame is rebuilt from
    the config on every render, so a ring of live objects would be a second
    copy of the same list with nothing keeping the two in step.
    """

    def __init__(
        self, names: Sequence[str], current: str | None = None
    ) -> None:
        self.names = list(names)
        # An unknown name falls back to the first site rather than raising.
        # The caller has already validated `--site` against the config, so
        # reaching this means a site vanished mid-run -- and a render is the
        # worst possible place to raise, since the exception costs the whole
        # display rather than one frame. `render_dashboard` makes the same
        # choice for the same reason.
        self.index = self.names.index(current) if current in self.names else 0

    @property
    def name(self) -> str:
        return self.names[self.index]

    def next(self) -> None:
        self.index = (self.index + 1) % len(self.names)

    def previous(self) -> None:
        self.index = (self.index - 1) % len(self.names)

    def jump(self, number: int) -> bool:
        """Focus site `number`, one-based. False if there is no such site.

        Out of range is ignored rather than clamped to the last site: with
        three configured, pressing 9 is a mistake, and quietly showing site 3
        would look like it had worked.
        """
        if not 1 <= number <= len(self.names):
            return False
        self.index = number - 1
        return True


def handle_key(key: str | None, focus: Focus) -> str:
    """Map one keystroke onto what the loop should do, moving the focus.

    Shift-Tab is absent on purpose and cannot be added here: it arrives from
    Windows `msvcrt` as a two-character extended sequence, which `KeyReader`
    swallows by design, so it never reaches this function at all.
    """
    if not key:
        return IGNORE
    if key in QUIT_KEYS:
        return QUIT
    if key in NEXT_KEYS:
        focus.next()
        return REDRAW
    if key in PREV_KEYS:
        focus.previous()
        return REDRAW
    if key in JUMP_KEYS:
        return REDRAW if focus.jump(int(key)) else IGNORE
    if key in REDRAW_KEYS:
        return REDRAW
    return IGNORE


def run_loop(
    live: LiveRegion,
    keys: KeySource,
    focus: Focus,
    stop_event: threading.Event,
    render: Callable[[str], RenderableType],
    refresh: float,
) -> None:
    """Draw a frame, then watch the keyboard until the next one is due.

    Two loops rather than one, and the inner one is the reason the dashboard
    feels immediate: a refresh interval is seconds long, so a key press has to
    be able to cut it short. Every wait inside it is on `stop_event`, which is
    what lets a quit -- from `q` here, or from a poller set stopped elsewhere
    -- end the loop without waiting out the interval it happened to land in.
    """
    while not stop_event.is_set():
        live.update(render(focus.name), refresh=True)
        deadline = time.monotonic() + refresh
        while time.monotonic() < deadline and not stop_event.is_set():
            action = handle_key(keys.poll(), focus)
            if action == QUIT:
                stop_event.set()
                break
            if action == REDRAW:
                break
            if stop_event.wait(KEY_TICK):
                break


# --------------------------------------------------------------------------
# What stdout can be asked to do
# --------------------------------------------------------------------------


def stdout_is_a_terminal() -> bool:
    """Whether there is a screen for the Live region to take over.

    Piped or redirected stdout gets the summary instead. A function rather
    than the `isatty()` call inline because the answer chooses between two
    entirely different commands, and because `sys.stdout` is None under
    pythonw -- where the attribute lookup, not the answer, would be the bug.
    """
    stream = sys.stdout
    return stream is not None and hasattr(stream, "isatty") and stream.isatty()


def resolve_ascii(ui: UiConfig) -> UiConfig:
    """Force --ascii when stdout cannot encode the plot's block glyphs.

    A legacy Windows code page cannot, and the failure mode is a
    UnicodeEncodeError raised from inside a Live refresh -- a crash in the
    middle of the dashboard rather than an obviously wrong character.

    Asked only when `--ascii` is off, and the config is returned untouched
    when nothing was decided: a user who has already asked for ascii has
    settled every question probing the encoding could have answered.
    """
    if ui.ascii or stdout_supports_blocks():
        return ui
    log.info(
        "stdout encoding %r cannot represent block glyphs; using --ascii",
        getattr(sys.stdout, "encoding", None),
    )
    return replace(ui, ascii=True)


# --------------------------------------------------------------------------
# Startup, and the report a pipe gets instead of a dashboard
# --------------------------------------------------------------------------


def print_priming(
    results: Mapping[str, PrimeResult], sites: Sequence[SiteConfig]
) -> None:
    """Say what startup found, per site, while stdout is still a terminal.

    On **stderr**, not stdout: `ga4-realtime | cat` is a documented way to get
    the day's numbers, and a permission diagnosis in the middle of them is one
    more thing for whatever is reading to have to skip. It is also the last
    moment any of this can be read at all, since a Live region is about to own
    the screen and a failing site is a marker in a strip from then on.

    In config order rather than in the order the priming pool finished, which
    is arbitrary where the file's order is not.
    """
    for site in sites:
        result = results.get(site.name)
        if result is None:
            continue
        # A failure and a warning are alternatives, not a pair: `PrimeResult`
        # carries the warning only on the path where the site came up.
        if result.error:
            print(result.error, file=sys.stderr)
        elif result.warning:
            print(result.warning, file=sys.stderr)


def print_report(ctx: Context) -> int:
    """Hand the pipe over to `report`, which owns that output entirely.

    A call rather than a copy. §7.8 requires `ga4-realtime | cat` to print
    what `ga4-realtime report` prints, "a script parsing one parses the
    other", and the only way to keep that true through later edits is for
    there to be one renderer. Two implementations of the same format is the
    pair that drifts -- and this one had already drifted, down to which
    sentence labels the combined total.

    It also settles which sites a pipe shows, without this module having an
    opinion: `report` reads `site_meta`, so a disabled or orphaned site still
    prints (D17). Selecting from `config.enabled_sites()` here made
    `ga4-realtime | cat` and `ga4-realtime report` disagree about how many
    sections exist, which is the same divergence in a different disguise.
    """
    # `--date` belongs to the report subparser, so the bare invocation's
    # namespace has no such attribute and `report.run` would reach for one
    # that is not there. Today is the only day a dashboard can mean, which is
    # exactly what the flag defaults to -- said here rather than as a getattr
    # default inside report.py, where it would look like `--date` is
    # sometimes optional to declare.
    ctx.args.date = getattr(ctx.args, "date", None)
    return report.run(ctx)


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------


def frame(
    store: RealtimeStore,
    sites: Sequence[SiteConfig],
    statuses: Mapping[str, Any],
    ui: UiConfig,
    ring: RingBufferHandler | None,
    *,
    focus: str,
    verbose: bool,
    no_color: bool,
    started: float,
) -> RenderableType:
    """One frame, measured against the terminal as it is at this instant.

    `get_terminal_size` on every frame rather than once at startup, because
    the window can be resized at any point in a run that lasts hours -- and
    on Windows there is no SIGWINCH to be told about it, so re-measuring is
    the only portable answer.
    """
    size = shutil.get_terminal_size(fallback=TERMINAL_FALLBACK)
    return render_dashboard(
        store,
        sites,
        statuses,
        ui,
        ring,
        focus=focus,
        width=size.columns,
        height=size.lines,
        verbose=verbose,
        no_color=no_color,
        started=started,
    )


def run(ctx: Context) -> int:
    # Anchored ahead of the config load and the priming poll rather than at
    # the render loop, because both can take a visible moment and the user
    # has been watching the program since here.
    started = time.monotonic()
    config = ctx.config()
    # Enabled only, in file order: a disabled site is not polled and is not
    # in the Tab rotation (D17), and this one list is what the poller set,
    # the focus ring and every frame all agree about.
    sites = config.enabled_sites()
    # `Context.focus()` has already run for the bare invocation and caches,
    # so this costs nothing and keeps the one place --site is validated in
    # cli.main, before a thread or a Live region exists.
    focus = Focus([site.name for site in sites], ctx.focus().name)
    ring = ctx.start_logging()
    ui = resolve_ascii(config.ui)
    no_color = no_color_requested()

    database = ctx.database()
    pollers = PollerSet(sites, database)
    # Priming raises when *every* site failed, which reaches cli.main as a
    # ConfigError and exits 1. A partial failure is printed and continued
    # past: one bad key must never hide two working sites.
    print_priming(pollers.prime_all(), sites)

    if not stdout_is_a_terminal():
        # Piped or redirected: the Live region would emit control sequences
        # into whatever is reading. Print the report instead and stop. The
        # priming poll above still happened, so what gets printed includes
        # this minute rather than only what a previous run left behind --
        # which is the one thing this path adds over running `report` itself.
        return print_report(ctx)

    pollers.start()

    # SIGWINCH does not exist on Windows and merely naming it raises
    # AttributeError. Where it does exist it prompts an immediate redraw;
    # where it does not, the per-frame get_terminal_size() call is already
    # enough to re-fit the plots on its own.
    if hasattr(signal, "SIGWINCH"):
        signal.signal(signal.SIGWINCH, lambda *_: None)

    console = Console(no_color=no_color, soft_wrap=False)
    store = RealtimeStore(database)

    def render(name: str) -> RenderableType:
        return frame(
            store,
            sites,
            pollers.statuses,
            ui,
            ring,
            focus=name,
            verbose=ctx.args.verbose,
            no_color=no_color,
            started=started,
        )

    try:
        with (
            KeyReader() as keys,
            Live(
                console=console,
                screen=False,
                auto_refresh=False,
                transient=False,
            ) as live,
        ):
            run_loop(live, keys, focus, pollers.stop_event, render, ui.refresh)
    finally:
        # In a finally rather than after the loop: an exception the render
        # never anticipated would otherwise leave N poller threads writing
        # into a database nobody is reading. The terminal itself is already
        # safe by this point -- KeyReader's __exit__ restores cbreak as the
        # `with` unwinds, which is exactly why the restore lives there.
        pollers.stop()
        store.close()
        console.print("stopped. data is in " + str(store.path))
    return 0
