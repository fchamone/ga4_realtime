"""The one sink, the in-memory tail, and the site every line belongs to.

Three things are worth failing a build over here. That `setup_logging` puts a
rotating file handler and a ring buffer on the logger and **nothing else** --
a stream handler would write into the frame Rich is repainting and tear the
display, and it is the kind of line somebody adds while debugging and forgets
to remove. That calling it twice replaces its own handlers rather than
stacking a second pair, since the second pair would double every line in the
file and leak the first pair's open file handle. And that a record logged
through a site's adapter carries that site's name into both the file and the
ring buffer, because the --verbose panel showing every site interleaved is
what stops a non-focused site failing in silence.

The logger is process-wide, so every test here restores it afterwards. A test
that left a handler attached would be writing another test's log lines into
its own tmp_path -- and on Windows, holding that file open would fail the
temporary directory's cleanup rather than the assertion.
"""

import logging
from pathlib import Path

import pytest

from ga4_realtime import logging_setup
from ga4_realtime.logging_setup import (
    RingBufferHandler,
    setup_logging,
    site_logger,
)


@pytest.fixture(autouse=True)
def restore_logger():
    """Leave the package logger exactly as it was found."""
    log = logging_setup.log
    before_handlers = list(log.handlers)
    before_level = log.level
    before_propagate = log.propagate
    yield
    for handler in list(log.handlers):
        log.removeHandler(handler)
        # Only handlers this test attached are closed: closing one that was
        # already there would break whoever owns it.
        if handler not in before_handlers:
            handler.close()
    for handler in before_handlers:
        log.addHandler(handler)
    log.setLevel(before_level)
    log.propagate = before_propagate


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    """A log path whose parent does not exist yet, as on a first run."""
    return tmp_path / "data" / "ga4_realtime.log"


def _handlers_of(kind) -> list[logging.Handler]:
    return [h for h in logging_setup.log.handlers if isinstance(h, kind)]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Handler set-up
# --------------------------------------------------------------------------


def test_creates_the_parent_directory(log_path: Path) -> None:
    assert not log_path.parent.exists()

    setup_logging(False, log_path)

    assert log_path.parent.is_dir()
    assert log_path.exists()


def test_accepts_a_string_path(tmp_path: Path) -> None:
    """Callers hand over whatever the config resolved, str included."""
    target = tmp_path / "nested" / "ga4_realtime.log"

    setup_logging(False, str(target))

    assert target.exists()


def test_attaches_exactly_one_file_handler_and_one_ring(
    log_path: Path,
) -> None:
    ring = setup_logging(False, log_path)

    files = _handlers_of(logging.handlers.RotatingFileHandler)
    rings = _handlers_of(RingBufferHandler)
    assert len(files) == 1
    assert len(rings) == 1
    assert rings[0] is ring
    assert len(logging_setup.log.handlers) == 2


def test_adds_no_stream_handler(log_path: Path) -> None:
    """Nothing on this logger may write to the terminal.

    `isinstance(h, StreamHandler)` alone would not do: FileHandler is a
    StreamHandler subclass, so the rotating file handler answers yes. What
    the assertion has to say is that no handler writes to a console stream.
    """
    setup_logging(True, log_path)

    for handler in logging_setup.log.handlers:
        assert not (
            isinstance(handler, logging.StreamHandler)
            and not isinstance(handler, logging.FileHandler)
        )
        stream = getattr(handler, "stream", None)
        assert stream is None or stream.name not in ("<stdout>", "<stderr>")


def test_does_not_propagate_to_the_root_logger(log_path: Path) -> None:
    """basicConfig anywhere in the process must not echo lines to stderr."""
    setup_logging(False, log_path)

    assert logging_setup.log.propagate is False


# --------------------------------------------------------------------------
# Idempotence
# --------------------------------------------------------------------------


def test_second_call_replaces_the_handlers_of_the_first(
    log_path: Path, tmp_path: Path
) -> None:
    first_ring = setup_logging(False, log_path)
    first_file = _handlers_of(logging.handlers.RotatingFileHandler)[0]

    second_path = tmp_path / "elsewhere" / "ga4_realtime.log"
    second_ring = setup_logging(False, second_path)

    assert len(logging_setup.log.handlers) == 2
    assert first_ring not in logging_setup.log.handlers
    assert first_file not in logging_setup.log.handlers
    assert second_ring in logging_setup.log.handlers
    # Closed, not just detached: FileHandler.close drops the stream, which is
    # what releases the file on Windows.
    assert first_file.stream is None


def test_second_call_does_not_double_the_lines(log_path: Path) -> None:
    setup_logging(False, log_path)
    ring = setup_logging(False, log_path)

    logging_setup.log.info("only once")

    assert _read(log_path).count("only once") == 1
    assert sum("only once" in line for line in ring.records) == 1


# --------------------------------------------------------------------------
# The site prefix
# --------------------------------------------------------------------------


def test_adapter_records_carry_the_site(log_path: Path) -> None:
    ring = setup_logging(False, log_path)

    site_logger("blog").info("polled 7 minutes")

    assert any("[blog] polled 7 minutes" in r for r in ring.records)
    assert "[blog] polled 7 minutes" in _read(log_path)


def test_two_sites_interleave_in_the_buffer(log_path: Path) -> None:
    """The panel's whole job: the failing site is visible while unfocused."""
    ring = setup_logging(False, log_path)

    site_logger("blog").info("ok")
    site_logger("shop").warning("permission denied")
    site_logger("blog").info("ok again")

    assert [r.split(" ", 2)[2] for r in ring.records] == [
        "[blog] ok",
        "[shop] permission denied",
        "[blog] ok again",
    ]
    contents = _read(log_path)
    assert "[shop] permission denied" in contents
    assert contents.index("[blog] ok") < contents.index("[shop]")


def test_records_without_a_site_carry_no_brackets(log_path: Path) -> None:
    """Startup and config lines own no site; an empty pair would be noise."""
    ring = setup_logging(False, log_path)

    logging_setup.log.info("reading config")

    assert ring.records[-1].endswith(" reading config")
    assert "[" not in ring.records[-1]
    assert "[" not in _read(log_path)


def test_child_logger_records_reach_the_file(log_path: Path) -> None:
    """A propagated record never passes the *logger's* filters.

    Which is why SiteContextFilter is attached to the handler: a record made
    by a child logger would otherwise arrive at the formatter with no
    site_prefix attribute, and the KeyError would be reported on stderr --
    into the Live region.
    """
    setup_logging(False, log_path)

    logging.getLogger("ga4_realtime.child").info("from a child")

    assert "from a child" in _read(log_path)


def test_adapter_keeps_the_callers_own_extra(log_path: Path) -> None:
    ring = setup_logging(False, log_path)
    seen: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record)

    logging_setup.log.addHandler(_Capture())
    site_logger("blog").info("hi", extra={"attempt": 3})

    assert seen[-1].site == "blog"
    assert seen[-1].attempt == 3
    assert any("[blog] hi" in r for r in ring.records)


# --------------------------------------------------------------------------
# Levels and the buffer itself
# --------------------------------------------------------------------------


def test_verbose_admits_debug_records(log_path: Path) -> None:
    ring = setup_logging(True, log_path)

    logging_setup.log.debug("cycle detail")

    assert any("cycle detail" in r for r in ring.records)
    assert logging_setup.log.level == logging.DEBUG


def test_quiet_drops_debug_records(log_path: Path) -> None:
    ring = setup_logging(False, log_path)

    logging_setup.log.debug("cycle detail")
    logging_setup.log.info("kept")

    assert not any("cycle detail" in r for r in ring.records)
    assert "cycle detail" not in _read(log_path)
    assert any("kept" in r for r in ring.records)
    assert logging_setup.log.level == logging.INFO


def test_ring_buffer_is_bounded() -> None:
    """A process running for weeks must not accumulate its own log."""
    ring = RingBufferHandler(capacity=3)

    for index in range(5):
        ring.emit(
            logging.LogRecord(
                "ga4_realtime",
                logging.INFO,
                __file__,
                1,
                f"m{index}",
                (),
                None,
            )
        )

    assert len(ring.records) == 3
    assert [r.split(" ", 2)[2] for r in ring.records] == ["m2", "m3", "m4"]
