"""Ring the configured bell once, on purpose, and say what it is ringing.

The bell only ever sounds when a real conversion arrives, which is exactly
the wrong moment to find out that the machine has no audio device, that the
terminal was set to a visual bell, or that the site's `conversions` list
names an event GA4 never reports. This command asks the same question
without waiting for a lead: it plays the tone the config file names, and
prints the two things that decide whether the dashboard will ever play it --
whether `[bell] enabled` is true, and which events each site rings for.

**It plays the tone even when `[bell] enabled` is false**, and says so on the
line above. Refusing would be the worse answer by some way: somebody who has
just switched the bell off and wants to hear what they are switching back on
would get silence, and silence is indistinguishable from the failure this
command exists to find. The state is reported instead, which is the whole
purpose of a report.

**It calls `beep_tone` directly rather than going through `Bell`.** `Bell` is
built for the one place it is used from -- inside a poll, where a tone may
not block the thread, may not queue behind another and may never raise -- so
it rings on a daemon thread and returns before a sound has been made. A
command that did that would exit while the note was still being scheduled and
kill the very thread that was about to make it audible. Here the beep is the
whole job and there is nothing to protect it from, so it happens on this
thread and its failure is the finding rather than a warning in a log nobody
opened.

**Nothing is polled and nothing is written.** The config file is read, which
is what proves the tone is the configured one; no API call is made, no
database or log is opened, and `ctx.start_logging()` is skipped for the
reason `doctor` skips it -- a command that creates the log while reporting on
the tool's own state has changed what it was asked to describe.

The module next to this one, `ga4_realtime.bell`, owns the sound and the
arithmetic behind it. This one owns only what a person typing
`ga4-realtime bell` sees.
"""

import sys

from ..bell import beep_tone
from ..config import BellConfig, Config, SiteConfig
from . import ALL_SITES, Context

# Same three-column shape `doctor` prints, and plain words for the same
# reason: this goes to a stdout `rich` never touches, and a tick mark meets a
# Windows console on a legacy code page with a UnicodeEncodeError raised out
# of the command that was meant to be helping.
OK = "ok"
FAIL = "FAIL"
WARN = "warn"
NOTE = ""

# Said at the end of every run, failures included, because it is the claim a
# user pointing this at a working setup most needs to be sure of -- and the
# one most easily broken by a later edit that reaches for a store or a
# client to add "one more useful line".
POLLED_NOTHING = (
    "Nothing was polled and nothing was written: `bell` reads the config, "
    "plays\nthe tone and stops. It makes no API call and opens no database "
    "or log."
)


def run(ctx: Context) -> int:
    config = ctx.config()
    # Before the tone rather than after it, so a `--site` typo fails while
    # the terminal is still quiet instead of after a beep the user then has
    # to decide whether to trust.
    sites = _chosen_sites(ctx, config)

    print("ga4-realtime bell")
    print()
    _line(NOTE, "config", str(config.source))
    _line(NOTE, "tone", _tone(config.bell))
    _describe_alert()
    _describe_state(config.bell, config)

    print()
    problems = _ring(config.bell)

    print()
    _print_sites(sites)
    print(POLLED_NOTHING)
    return 1 if problems else 0


# --------------------------------------------------------------------------
# What it is about to play
# --------------------------------------------------------------------------


def _tone(settings: BellConfig) -> str:
    return f"{settings.frequency} Hz for {settings.duration} ms"


def _describe_alert() -> None:
    """How this platform makes the sound, and what that costs.

    `sys.platform` is read here rather than at import so a test can move the
    platform under the command, the same way `tests/test_bell.py` moves it
    under `beep_tone` -- which is the function this describes and must not
    be allowed to disagree with.
    """
    if sys.platform == "win32":
        _line(NOTE, "alert", "winsound.Beep -- the tone above, exactly")
        return

    # Not a failure: it is what every non-Windows machine does, and the run
    # is still telling the truth about what will happen. It is a `warn`
    # because two of the three config keys do nothing here and a user who
    # has just changed `frequency` deserves to be told that before they
    # change it again.
    _line(WARN, "alert", "the terminal's own bell (BEL on stderr)")
    _detail(
        f"The stdlib cannot ask for a pitch on {sys.platform}, so "
        "[bell] frequency and\nduration are accepted but unused here -- one "
        "config file has to load on\nevery machine you run it from. What you "
        "hear is whatever your terminal\nplays for BEL, and a terminal set "
        "to a visual bell will flash instead of\nsounding. That is a "
        "terminal setting, not a setting of this tool."
    )


def _describe_state(settings: BellConfig, config: Config) -> None:
    if settings.enabled:
        _line(OK, "state", "enabled: the dashboard rings on a conversion")
        return

    # Reported, and the tone still plays below. A choice rather than a
    # problem, so the exit code stays 0 -- the same reading `doctor` gives a
    # site with `enabled = false`. Neither of these two lines can raise the
    # exit code: only a tone that would not play does that.
    _line(WARN, "state", "[bell] enabled = false: the dashboard stays silent")
    # The path on a line of its own rather than inside the sentence: it is
    # the one thing here that has to be copied, and an absolute Windows path
    # wrapped into the middle of a paragraph is the hardest possible way to
    # read one.
    _detail(
        "To hear this while polling, set enabled = true under [bell] in\n"
        f"  {config.source}\n"
        "The tone below plays either way, because a command for hearing the\n"
        "bell that answered with silence would be indistinguishable from a\n"
        "bell that could not ring at all."
    )


# --------------------------------------------------------------------------
# The sound itself
# --------------------------------------------------------------------------


def _ring(settings: BellConfig) -> int:
    """Play one tone on this thread, and report what happened. 0 or 1."""
    _line(NOTE, "ringing", "once, now")
    try:
        beep_tone(settings.frequency, settings.duration)
    except Exception as exc:  # noqa: BLE001 - diagnose, never traceback
        # Broad for the reason `Bell` is broad: what comes back from a
        # machine with no audio device is whatever the platform's sound API
        # decided to raise, and a command whose one job is to find that out
        # must not answer it with a traceback.
        _line(FAIL, "rang", "no: the tone could not be played")
        _detail(f"{exc.__class__.__name__}: {exc}")
        _detail(
            "The dashboard survives this -- the bell logs one warning, goes "
            "quiet for\nthe rest of the run and never fails the poll it was "
            "announcing. Set\n[bell] enabled = false to stop it being tried "
            "at all."
        )
        return 1

    # "asked for" rather than "you heard": the call returning proves the API
    # accepted the tone, not that a speaker was connected, unmuted or turned
    # up. Claiming more than that would send somebody looking at their config
    # file for a problem that is in the room.
    _line(OK, "rang", "yes -- if you heard nothing, check the volume first")
    return 0


# --------------------------------------------------------------------------
# What would ring it
# --------------------------------------------------------------------------


def _chosen_sites(ctx: Context, config: Config) -> list[SiteConfig]:
    """The sites to list. `--site all` and no `--site` are the same thing.

    Disabled sites are kept rather than filtered out: "this site is not
    polled, so it never rings" is the answer somebody hunting a bell that
    never sounds most needs, and dropping the site from the listing would
    leave them to notice the absence.
    """
    wanted = ctx.args.site
    if wanted is None or wanted == ALL_SITES:
        return list(config.sites)
    # Raises naming every spelling that would have worked.
    return [config.site(wanted)]


def _print_sites(sites: list[SiteConfig]) -> None:
    print("What rings it -- each site's conversions:")
    width = max(len(site.name) for site in sites)
    for site in sites:
        if site.enabled:
            detail = ", ".join(site.conversions)
        else:
            detail = "not polled (enabled = false), so this site never rings"
        print(f"  {site.name:<{width}}  {detail}")
    print()
    # The one thing about the bell that is not visible from the lines above,
    # and the reason it is worth a line of its own: the listing reads like a
    # per-site setting, and every site here rings the same bell whether or
    # not it is the site on screen.
    print(
        "Every site rings this one bell, for its own events, whether or not\n"
        "it is the site on screen. What counts as new is a rise in one\n"
        "minute's count for one event, so a lead is announced once."
    )
    print()


# --------------------------------------------------------------------------
# Printing
# --------------------------------------------------------------------------


def _line(state: str, label: str, detail: str) -> None:
    print(f"  {state:<4}  {label:<8}  {detail}")


def _detail(text: object) -> None:
    """A multi-line explanation, indented under the line it belongs to.

    Indented rather than reflowed, for `doctor`'s reason: these messages
    arrive already broken into lines that mean something, and rewrapping
    them to the terminal would join two of them.
    """
    for line in str(text).splitlines():
        print(f"        {line}")
