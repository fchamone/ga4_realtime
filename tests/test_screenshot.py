"""`--screenshot`: the mockup frame, and the promises it makes.

Three claims are worth a test each, and they are the claims rather than the
implementation:

* **It fits.** The frame never exceeds the terminal it was measured against --
  the invariant a whole second render path could quietly break -- and it stops
  one row short, so the returning shell prompt does not scroll the header off
  the screen being photographed.
* **It writes nothing and reads nothing.** No database, no log, no config. A
  broken `ga4-realtime.toml` sitting in the working directory must not change
  the outcome, because that is the only way to prove the config is never
  reached rather than merely not needed.
* **It touches no API.** Free, and worth stating: `conftest.py`'s autouse
  guards fail any test that constructs either Google client, so a mode that
  reached for one could not pass this file.

The size sweep renders through a `rich.Console` of exact width and height, the
way `test_dashboard.py` does, and for its reason: `force_terminal=False`,
`color_system=None` and `legacy_windows=False` keep the line count a property
of the frame rather than of the machine, and CI runs Windows as well as Linux.
That helper is written again here rather than imported, because
`--import-mode=importlib` keeps the repo root off `sys.path` and a cross-test
import does not resolve.
"""

import io
import re

import pytest
from rich.console import Console

from ga4_realtime import cli
from ga4_realtime.commands import screenshot
from ga4_realtime.timeutil import day_bounds_utc
from ga4_realtime.ui.dashboard import _NEVER_POLLED

# Terminal sizes to sweep. The wide ones are what a screenshot is actually
# taken at; the small ones walk the height budget down through each of its
# three sacrifices -- log panel, then table rows, then the chart entirely.
#
# The floor is 15 rows, which is where `test_dashboard.py`'s own sweep starts,
# and it is a real bound rather than a convenient one: the header and the
# footer are never sacrificed, so eight rows of panel is the smallest frame
# `render_dashboard` can produce and a shorter terminal is a promise nothing
# in this package makes. See `test_the_floor_is_the_header_and_the_footer`.
SIZES = [
    (140, 45),
    (120, 40),
    (100, 30),
    (90, 24),
    (80, 20),
    (72, 18),
    (60, 16),
    (40, 15),
]


def measure(frame, width: int, height: int) -> list[str]:
    """Render `frame` through a console of exactly that size; return its lines."""
    console = Console(
        width=width,
        height=height,
        file=io.StringIO(),
        force_terminal=False,
        color_system=None,
        legacy_windows=False,
    )
    with console.capture() as capture:
        console.print(frame)
    return capture.get().splitlines()


@pytest.fixture(scope="module")
def demo():
    """The demo sites, ui and store, built once for the whole sweep.

    Module-scoped because seeding a day of rows per parametrised case would
    make this file a benchmark of SQLite rather than a test of the frame.
    """
    sites = screenshot.demo_sites()
    focus = screenshot.default_focus(sites)
    store = screenshot.demo_store(sites, focus)
    try:
        yield sites, screenshot.demo_ui(), store, focus
    finally:
        store.close()


def render(demo, width: int, height: int, **kwargs):
    sites, ui, store, focus = demo
    return screenshot.frame(
        store,
        sites,
        kwargs.pop("ui", ui),
        focus=kwargs.pop("focus", focus),
        width=width,
        height=height,
        verbose=kwargs.pop("verbose", False),
        no_color=True,
    )


# --------------------------------------------------------------------------
# The frame fits
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("width", "height"), SIZES)
@pytest.mark.parametrize("verbose", [False, True])
def test_the_frame_never_exceeds_the_terminal(
    demo, width: int, height: int, verbose: bool
) -> None:
    """The invariant this whole mode is at risk of breaking.

    A frame taller than the terminal scrolls, which in the live view tears the
    display on every refresh and here ruins the picture. `--verbose` is swept
    alongside because the log panel is the first thing the budget sacrifices,
    and a panel that failed to be dropped would show up only at these sizes.
    """
    lines = measure(
        render(demo, width, height, verbose=verbose), width, height
    )

    assert len(lines) <= height
    assert all(len(line) <= width for line in lines)


@pytest.mark.parametrize(("width", "height"), SIZES)
def test_the_header_and_footer_always_survive(
    demo, width: int, height: int
) -> None:
    """Never sacrificed, at any size: the two panels that say what this is.

    A frame that cannot name the site or say how stale the numbers are is not
    worth the rows it saved, which is why the budget spends the chart first.
    """
    lines = measure(render(demo, width, height), width, height)
    text = "\n".join(lines)

    assert "example.com" in text or "demo01" in text
    assert "page views" in text


def test_the_floor_is_the_header_and_the_footer(demo) -> None:
    """Below which the frame stops shrinking, on purpose rather than by bug.

    Asked for six rows it still produces eight: four of header and four of
    footer, with everything between them already spent. That is
    `render_dashboard`'s rule -- a frame that cannot say which site it is about
    or how stale the numbers are is not worth the rows it saved -- and it is
    recorded here so the sweep's floor above reads as a bound rather than as a
    number somebody picked.
    """
    lines = measure(render(demo, 30, 6), 30, 6)

    assert len(lines) == 8
    assert "page views" in "\n".join(lines)


def test_the_frame_stops_one_row_short_of_the_terminal(capsys) -> None:
    """So the returning shell prompt does not scroll the header off the top.

    The live dashboard owns the screen and uses every row of it. This one
    hands the terminal back, and the row it leaves is the one the prompt
    lands on.
    """
    assert cli.main(["--screenshot"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines, "the frame printed nothing"
    # The command measures the real terminal, which under pytest falls back to
    # live.TERMINAL_FALLBACK; asserting the exact height would pin the
    # fallback rather than the rule, so this asserts the rule.
    assert len(lines) < 30


# --------------------------------------------------------------------------
# It reads nothing and writes nothing
# --------------------------------------------------------------------------


def test_it_writes_nothing_anywhere(tmp_path, monkeypatch, capsys) -> None:
    """The claim that makes this usable in a fresh clone.

    `RealtimeStore` creates the file it is pointed at, `setup_logging` creates
    the directory the log goes in, and either would leave something behind on a
    machine the user had not set up yet.
    """
    monkeypatch.chdir(tmp_path)

    assert cli.main(["--screenshot"]) == 0
    assert capsys.readouterr().out

    assert list(tmp_path.iterdir()) == []


def test_the_store_is_genuinely_in_memory() -> None:
    """Not "a file we then delete": there is no file at any point.

    `PRAGMA database_list` names the file backing each attached database, and
    the empty string is what an in-memory one reports. Asserting that the
    platform database path does not *exist* would fail on any machine that has
    actually run the tool, including the author's.
    """
    store = screenshot.MockupStore()
    try:
        backing = store._conn.execute("PRAGMA database_list").fetchone()[2]
        assert backing == ""
        # The footer's two numbers still have to be usable ones.
        assert store.size_bytes() > 0
    finally:
        store.close()


def test_a_broken_config_in_the_working_directory_is_ignored(
    tmp_path, monkeypatch, capsys
) -> None:
    """The proof that the config is never *reached*, not merely not needed.

    A config file this broken fails `tomllib` on the first line, so any code
    path that loaded one would exit 1 here. Discovery looks in the working
    directory second, which is what this puts a file in.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ga4-realtime.toml").write_text("this is not toml [[[\n")

    assert cli.main(["--screenshot"]) == 0
    assert capsys.readouterr().out


def test_it_says_on_stderr_that_the_numbers_are_invented(capsys) -> None:
    """On stderr, so `--screenshot > frame.txt` captures only the frame."""
    assert cli.main(["--screenshot"]) == 0

    captured = capsys.readouterr()
    assert "mockup" in captured.err
    assert "mockup" not in captured.out


# --------------------------------------------------------------------------
# The flags it honours, and the ones it refuses
# --------------------------------------------------------------------------


def test_ascii_renders_without_a_single_non_ascii_character(capsys) -> None:
    """For terminals without braille, and for a screenshot that has to paste.

    The whole frame, not just the chart: the panel borders, the table rules and
    the status markers all have ASCII alternates, and one that did not switch
    would raise UnicodeEncodeError on a legacy Windows code page.
    """
    assert cli.main(["--screenshot", "--ascii"]) == 0

    assert capsys.readouterr().out.isascii()


def test_verbose_adds_the_log_panel(capsys) -> None:
    assert cli.main(["--screenshot", "--verbose"]) == 0
    with_panel = capsys.readouterr().out

    assert cli.main(["--screenshot"]) == 0
    without_panel = capsys.readouterr().out

    assert "next poll in 300s" in with_panel
    assert "next poll in 300s" not in without_panel


def test_site_moves_the_focus(capsys) -> None:
    """The header names the site that was asked for, not the default one."""
    assert cli.main(["--screenshot", "--site", "demo03"]) == 0

    assert "docs.example" in capsys.readouterr().out


def test_an_unknown_site_is_refused_by_name(capsys) -> None:
    """Rather than silently drawing a different one.

    `render_dashboard` falls back to the first site for an unknown focus, which
    is right inside a render -- an exception there costs the whole display --
    and wrong here, where it would answer a typo with the wrong picture.
    """
    assert cli.main(["--screenshot", "--site", "nosuchsite"]) == 1

    error = capsys.readouterr().err
    assert "nosuchsite" in error
    assert "demo01" in error


def test_an_unresolvable_timezone_is_refused(capsys) -> None:
    """Because no config file will check it, and the renderer will not either.

    `panels.resolve_zone` falls back to UTC in silence, so without the check
    this would draw a UTC frame while claiming the zone that was asked for.
    """
    assert cli.main(["--screenshot", "--timezone", "Not/AZone"]) == 1

    assert "Not/AZone" in capsys.readouterr().err


def test_window_reaches_the_chart(capsys) -> None:
    """The axis is a trailing window, so the flag changes what it is labelled."""
    assert cli.main(["--screenshot", "--window", "24"]) == 0

    assert "last 24h" in capsys.readouterr().out


# --------------------------------------------------------------------------
# The data behind the frame
# --------------------------------------------------------------------------


def test_the_seeded_day_is_not_empty(demo) -> None:
    """The failure a date-anchoring mistake produces, caught directly.

    Rows written under a hardcoded date fall outside the day bounds the frame
    computes from `utcnow()`, and everything still renders -- as zeros. That
    reads as a layout bug rather than as a seeding bug, so it is worth an
    assertion that names it.
    """
    sites, _ui, store, focus = demo
    site = next(one for one in sites if one.name == focus)

    start, end = day_bounds_utc(
        screenshot.utcnow().astimezone(screenshot.zone_of(site)).date(),
        screenshot.zone_of(site),
    )
    totals = store.day_totals(site.name, site.conversions, start, end)

    assert totals.page_views > 0
    assert totals.events > totals.page_views
    assert totals.minutes_recorded > 0


def test_the_chart_has_data_in_the_default_window(demo, capsys) -> None:
    """The default focus exists to guarantee this at every hour of the day.

    A day-scoped frame drawn at 00:20 local would show twenty minutes of
    overnight ramp; the focus picks whichever demo site is deepest into its
    afternoon so the picture is always of a working day.
    """
    assert cli.main(["--screenshot"]) == 0

    out = capsys.readouterr().out
    assert "no data yet" not in out
    assert "no data in window" not in out


def test_every_event_reaches_the_table(demo) -> None:
    """None of them is rounded out of existence on the way in.

    At two thousand views a day every event is a fraction of one per minute, so
    plain division would floor all four to zero in every minute and the table
    would come out empty.
    """
    sites, _ui, store, focus = demo
    site = next(one for one in sites if one.name == focus)

    start, end = day_bounds_utc(
        screenshot.utcnow().astimezone(screenshot.zone_of(site)).date(),
        screenshot.zone_of(site),
    )
    events = dict(store.top_events(site.name, start, end, limit=20))

    for name, _ in screenshot._EVENT_MIX:
        assert events.get(name, 0) > 0, f"{name} rounded away"


def test_the_events_arrive_in_the_proportions_the_mix_declares(demo) -> None:
    """`_EVENT_MIX` is documentation, and this is what keeps it true.

    The first attempt at spreading fractional events fired them whenever the
    fraction beat a threshold that advanced by a prime each minute. It looked
    right and was not: the threshold cycles with the clock, the fraction cycles
    with the traffic, and the two beat against each other into a per-event bias
    that landed `user_engagement` at 47% of page views where the mix says 58.
    Nothing on the frame looked wrong -- the order of the table was even still
    correct -- so only an assertion on the numbers themselves catches it.
    """
    sites, _ui, store, focus = demo
    site = next(one for one in sites if one.name == focus)

    start, end = day_bounds_utc(
        screenshot.utcnow().astimezone(screenshot.zone_of(site)).date(),
        screenshot.zone_of(site),
    )
    events = dict(store.top_events(site.name, start, end, limit=20))
    base = events["page_view"]
    # The default focus is mid-afternoon, so the day is well underway; the
    # accumulator's error is bounded by one event per series, which is noise
    # against a base this size but would not be against a base of fifty.
    assert base > 200

    for name, share in screenshot._EVENT_MIX:
        measured = 100 * events[name] / base
        assert abs(measured - share) < 2, (
            f"{name}: {measured:.1f}% vs {share}%"
        )


def test_no_user_metric_reaches_the_frame(capsys) -> None:
    """The invariant, restated where a mockup would be tempted to break it.

    A screenshot looks richer with a visitor count on it, and realtime data
    cannot produce one: the same visitor recurs across minutes with no
    identifier to deduplicate on, and the fan-out puts one visitor in several
    rows of a single minute. There is no column to fill in, and there must be
    no invented figure either.
    """
    assert cli.main(["--screenshot"]) == 0

    out = capsys.readouterr().out.lower()
    assert not re.search(r"\b(users?|visitors?|active users)\b", out)


def test_the_numbers_are_the_same_from_one_run_to_the_next(demo) -> None:
    """Nothing here is random, so a retaken screenshot differs only by time."""
    sites, _, _, focus = demo
    site = next(one for one in sites if one.name == focus)

    first = screenshot.seed_rows(site)
    second = screenshot.seed_rows(site)

    assert first == second


def test_a_demo_status_carries_every_key_the_frame_indexes() -> None:
    """A missing key is a KeyError raised inside a render, not a wrong number.

    `dashboard._NEVER_POLLED` is the authoritative list, so this compares
    against it rather than repeating it.
    """
    snapshot = screenshot.DemoStatus().snapshot()

    assert snapshot.keys() == _NEVER_POLLED.keys()


def test_it_builds_no_google_client(demo) -> None:
    """Free, from conftest's autouse guards -- and worth pinning anyway.

    Those fixtures replace both client classes with something that raises on
    construction, so this test passing at all is the assertion. Stated
    explicitly because "the mode needs no credentials" is its headline claim.
    """
    sites, _, store, focus = demo

    assert store.load_site(focus) is not None
    assert [site.name for site in sites] == ["demo01", "demo02", "demo03"]
