"""The audible alert: what counts as a new conversion, and how it sounds.

Two objects, and the split between them is the point. `ConversionWatch`
decides *whether* something new arrived -- arithmetic over poll rows, where
every double-counting risk in this feature lives, and the half worth testing
hard. `Bell` decides what that sounds like, and its entire job is to make a
noise without ever being able to cost a poll.

**A tone rather than the terminal bell, where a tone can be asked for.** The
alert competes with whatever else already beeps on the machine -- an editor
hook at 800 Hz, a notification at 1000 Hz -- and a third sound
indistinguishable from those two carries no information. Windows can be asked
for a frequency directly (`winsound.Beep`); the stdlib offers nothing
equivalent anywhere else, so elsewhere the alert is the terminal's own BEL and
the pitch is whatever the terminal decides. That is why `[bell] frequency` and
`duration` are documented as Windows-only rather than quietly ignored on the
other two platforms.

**Nothing here may raise into a poll.** `ring()` is called from inside
`Poller.poll_once`, which is wrapped in the broad `except Exception` that
keeps collection alive -- so a machine with no audio device would otherwise
turn every poll into a logged failure and an exponential backoff. It rings on
a thread of its own (`winsound.Beep` blocks for the whole duration, and a poll
is not going to wait 150 ms for a noise), it drops a ring that arrives while
one is already sounding rather than queueing it, and the first failure
silences it for the rest of the run with one warning in the log.

**This module imports nothing from the package.** stdlib only, so the
collection layer can ring it without acquiring a dependency on anything above
it -- the same reason `poller.py` imports nothing from `ui/`.
"""

import logging
import sys
import threading
from collections.abc import Callable, Iterable, Sequence
from typing import Protocol

log = logging.getLogger(__name__)

# The tone, in Hz and milliseconds. 1200 Hz sits clear of the 800 and 1000 Hz
# an editor's own hooks are likely to be using already, and 150 ms is long
# enough to be heard as a note rather than a click without holding the ringing
# thread open for a noticeable part of a poll.
DEFAULT_FREQUENCY = 1200
DEFAULT_DURATION = 150

# `winsound.Beep`'s own accepted range, checked at config load so an
# impossible tone is a `ConfigError` naming the file to edit rather than a
# ValueError raised out of the first conversion of the day -- by which point
# the run has been going for hours and the traceback names a Windows API
# rather than a config key.
MIN_FREQUENCY = 37
MAX_FREQUENCY = 32767
# Milliseconds. Below 10 the tone is a click on most hardware; above 5000 the
# bell is holding a note for longer than the shortest sensible poll interval,
# which is a stuck alarm rather than an alert.
MIN_DURATION = 10
MAX_DURATION = 5000


class EventRow(Protocol):
    """The three fields of a poll row this module reads.

    A structural type rather than an import of `ga4.MinuteRow`, for the reason
    `store.MinuteRowLike` is one: naming the concrete row here would make a
    stdlib-only module depend on the API layer, and the watch needs three of
    its six fields.
    """

    minute_ts_utc: str
    event_name: str
    event_count: int


# What actually makes the sound. Injected rather than called directly so the
# suite can prove the ringing logic without a test run being audible -- and
# without CI needing a sound card.
Beeper = Callable[[int, int], None]
# How the beep gets off the calling thread. Injected for the same reason: a
# test that started a real thread would have to join it, and joining is the
# one thing `ring()` deliberately never does.
Spawn = Callable[[Callable[[], None]], None]


def beep_tone(frequency: int, duration: int) -> None:
    """One beep: a real tone on Windows, the terminal's BEL anywhere else."""
    if sys.platform == "win32":
        # Imported here rather than at module scope: `winsound` does not exist
        # on the other two platforms in the CI matrix, and a guarded top-level
        # import would leave a name whose absence every reader has to check
        # for. Python caches the module after the first call like any other.
        import winsound

        winsound.Beep(frequency, duration)
        return
    # stderr, not stdout: stdout belongs to the Live region, and a byte
    # written into the middle of a frame's escape sequences is how a display
    # tears. BEL moves no cursor and prints no glyph, so the terminal gets the
    # sound and the frame gets nothing at all.
    stream = sys.stderr
    if stream is None:  # pythonw, and nowhere to ring
        return
    stream.write("\a")
    stream.flush()


def _in_a_thread(work: Callable[[], None]) -> None:
    """Run `work` on a daemon thread nobody waits for.

    Daemon because a beep must never be the reason a quit hangs: `winsound`
    holds its thread for the whole duration, and Ctrl-C during a 150 ms note
    should end the process now rather than at the end of the note.
    """
    threading.Thread(target=work, name="ga4-bell", daemon=True).start()


class Bell:
    """Rings, at most once at a time, and never raises at the caller."""

    def __init__(
        self,
        *,
        frequency: int = DEFAULT_FREQUENCY,
        duration: int = DEFAULT_DURATION,
        enabled: bool = True,
        beep: Beeper = beep_tone,
        spawn: Spawn = _in_a_thread,
    ) -> None:
        self.frequency = frequency
        self.duration = duration
        self.enabled = enabled
        self._beep = beep
        self._spawn = spawn
        # Single-flight, not a queue. Twelve sites can land a conversion in
        # the same second, and twelve tones back to back say nothing that one
        # does -- the status strip and the log are where "which site" is
        # answered, and neither of them is in a hurry.
        self._free = threading.Semaphore(1)
        self._silenced = False

    def ring(self) -> None:
        """Ask for one beep. Returns immediately, and cannot fail."""
        if not self.enabled or self._silenced:
            return
        if not self._free.acquire(blocking=False):
            return
        try:
            self._spawn(self._sound)
        except Exception as exc:  # noqa: BLE001 - a poll is waiting on this
            # Only reachable if the thread could not be started at all, which
            # is a machine out of threads rather than a bell problem. Getting
            # the permit back matters more here than the diagnosis does.
            self._free.release()
            self._silence(exc)

    def _sound(self) -> None:
        try:
            self._beep(self.frequency, self.duration)
        except Exception as exc:  # noqa: BLE001 - runs on its own thread
            # An exception here would reach threading.excepthook, which writes
            # a traceback to stderr and straight through the frame the main
            # thread is drawing. No audio device is the ordinary cause, and it
            # will still be true at the next conversion, so the bell goes
            # quiet for the run rather than logging once per lead.
            self._silence(exc)
        finally:
            self._free.release()

    def _silence(self, exc: Exception) -> None:
        self._silenced = True
        log.warning(
            "the bell could not ring (%s: %s); it is off for the rest of "
            "this run. Set [bell] enabled = false to stop it being tried.",
            exc.__class__.__name__,
            exc,
        )


class ConversionWatch:
    """One site's answer to "did a conversion arrive since the last poll?".

    The hard part is that a poll is not a stream of new events: every cycle
    re-reads the same `poll_window` minutes and the store's UPSERT *corrects*
    them, so a run that rang once per conversion row would ring for the same
    lead every cycle for ten minutes. What is new is therefore defined as a
    *rise* in one minute's count for one event, against the last count seen
    for that same minute and event -- the same distinction the store already
    makes between an insert and a correction, applied to the handful of events
    the site rings for: its conversions plus its `bell_additional_events`
    (`SiteConfig.bell_events`). The name stayed `ConversionWatch` because
    the arithmetic does not care which list an event came from; the poller
    is what tells the two apart, in the log line.

    Three consequences worth stating, because each is a bug if reversed:

    * **The first fold rings nothing.** It establishes the baseline. Priming
      reads up to 30 minutes of history before the loop starts, and a startup
      that announced every lead of the last half hour as breaking news would
      be a reason to turn the bell off on the first day.
    * **A correction downwards is not a rise**, and the lowered count is what
      is kept, so a later climb back past it rings once rather than not at
      all.
    * **State is pruned to the window the API answered with.** A minute that
      has fallen out of the Realtime API's reach cannot come back, and keeping
      its count would grow the dict for as long as the process ran.

    Midnight needs no special case, because nothing here is day-scoped. The
    status strip's counter resets at the site's local midnight because it asks
    the store for a day; the watch only ever compares a minute with itself.
    """

    def __init__(self, events: Iterable[str]) -> None:
        # A set for the membership test in the fold, which runs over every row
        # of every poll; the order events were configured in is not something
        # this class ever reports.
        self.events = frozenset(events)
        self._seen: dict[tuple[str, str], int] = {}
        self._primed = False

    def new_events(self, rows: Sequence[EventRow]) -> dict[str, int]:
        """Fold one poll's rows in, and report the rise, per event name.

        Empty when nothing rose -- and on the first call, and for a site with
        no conversions configured, and when the poll came back with no rows at
        all. A caller that rings on a non-empty answer is doing the right
        thing in all four cases.
        """
        if not self.events:
            return {}

        totals: dict[tuple[str, str], int] = {}
        oldest: str | None = None
        for row in rows:
            # The oldest minute in the *response*, not merely among conversion
            # rows: it is the window the API answered with, and pruning to the
            # oldest lead instead would drop minutes that are still live.
            if oldest is None or row.minute_ts_utc < oldest:
                oldest = row.minute_ts_utc
            if row.event_name not in self.events:
                continue
            key = (row.minute_ts_utc, row.event_name)
            # Summed over the fan-out: one minute's leads reach this loop once
            # per device and country they were recorded under, and those rows
            # are the split of a single figure.
            totals[key] = totals.get(key, 0) + int(row.event_count)

        if oldest is None:
            # No rows at all. Nothing rose, and nothing may be pruned either:
            # dropping the baseline here would make the next poll's re-read of
            # the same minutes look like a fresh conversion in every one.
            return {}

        risen: dict[str, int] = {}
        if self._primed:
            for (minute, event), count in totals.items():
                rise = count - self._seen.get((minute, event), 0)
                if rise > 0:
                    risen[event] = risen.get(event, 0) + rise

        self._seen = {
            key: value for key, value in self._seen.items() if key[0] >= oldest
        }
        self._seen.update(totals)
        self._primed = True
        return risen
