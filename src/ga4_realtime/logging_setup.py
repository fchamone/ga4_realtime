"""The log file, the in-memory tail behind --verbose, and the site prefix.

One rule shapes this whole module: **stdout belongs to the Live region.**
Rich repaints the dashboard in place, and a single line written to the
terminal from anywhere else pushes the frame down and tears the display for
the rest of the run. So the rotating file is the only sink -- there is no
stream handler at any level, and none may be added -- and the only logging
that ever reaches the screen is the --verbose panel, fed from memory by
`RingBufferHandler` and drawn *inside* the frame like any other panel.

The log lives beside the database rather than beside this source file. The
package installs into site-packages now, which must never be written to, and
the database is the one location the user has either chosen or been given a
default for; `paths.log_path_for` is where that rule is written down, and the
caller passes the answer in.

Every record a poller emits carries the name of the site that produced it, so
the --verbose panel can show all sites interleaved and still be read. A panel
showing only the focused site would hide exactly the site worth looking at --
the one whose marker in the status strip has gone red -- and would defeat the
strip it sits below.
"""

import logging
import time
from collections import deque
from collections.abc import MutableMapping
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# One logger for the whole package, addressed by name rather than a
# per-module `getLogger(__name__)` tree. The level, the file and the ring
# buffer are all process-wide anyway, and a tree would only add places for a
# record to reach the root logger -- which is to say, to reach stderr.
LOGGER_NAME = "ga4_realtime"
log = logging.getLogger(LOGGER_NAME)

# 1 MB per file, three backups: a day of ordinary polling is well under one
# file, so the rotation exists for the run that goes wrong and logs an error
# every cycle for a week, not for the run that goes right.
MAX_BYTES = 1_000_000
BACKUP_COUNT = 3

# The panel shows a dozen lines at most; the rest of the buffer is scrollback
# for the moment something fails and the last few minutes matter.
RING_CAPACITY = 200

FILE_FORMAT = "%(asctime)s %(levelname)-7s %(site_prefix)s%(message)s"


def site_prefix(record: logging.LogRecord) -> str:
    """`[name] ` when the record belongs to a site, nothing when it does not.

    Read with getattr rather than assumed: startup, configuration and the CLI
    all log through the plain module logger and own no site at all, and a
    bracketed empty pair in front of those lines would be noise claiming to
    be information.
    """
    site = getattr(record, "site", "")
    return f"[{site}] " if site else ""


class SiteContextFilter(logging.Filter):
    """Fill in the `site_prefix` the file formatter names.

    Attached to the handler rather than to the logger, and that placement is
    the point. A logger's filters run only for the records that logger
    creates; a record propagated up from a child logger reaches the same
    handlers without ever passing them. A handler's filters run for every
    record it handles, whoever made it. Miss one and the formatter raises
    KeyError on %(site_prefix)s, which logging reports by writing to
    stderr -- into the Live region this module exists to keep clear.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.site_prefix = site_prefix(record)
        return True


class RingBufferHandler(logging.Handler):
    """Keeps the last N records in memory for the --verbose panel.

    The lines are rendered on the way in and kept as text, not as records
    formatted at paint time: the panel is rebuilt on every refresh, and
    formatting the whole buffer several times a second to show its last dozen
    lines is work the render budget cannot spare.
    """

    def __init__(self, capacity: int = RING_CAPACITY) -> None:
        super().__init__()
        self.records: deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        # Local wall clock, through the same converter logging uses for
        # %(asctime)s, so a line read off the panel and the same line read
        # out of the file agree. Storage keys are UTC and only display is
        # local (see timeutil); a log line is display.
        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        # The site prefix is computed here rather than read off the record,
        # so the handler renders the same line whether or not
        # SiteContextFilter ran first. The buffer is what a person reads when
        # something has already gone wrong; it should not depend on the rest
        # of the set-up being right.
        self.records.append(
            f"{stamp} "
            f"{record.levelname[:4]:<4} "
            f"{site_prefix(record)}{record.getMessage()}"
        )


class SiteLoggerAdapter(logging.LoggerAdapter):
    """Stamps every record it forwards with the site that produced it.

    An attribute on the record, not a prefix baked into the message string.
    The file and the panel want the site in different positions, and a caller
    that has to remember to write the prefix by hand is a caller that will
    eventually forget -- on the one line reporting the failure, since that is
    the line nobody rehearsed.
    """

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        # Merged rather than assigned. LoggerAdapter.process replaces
        # kwargs["extra"] outright, so a caller passing extra= of its own
        # would silently lose the site -- or keep the site and lose its own
        # keys, depending which way round the default happened to run.
        extra = dict(self.extra or {})
        extra.update(kwargs.get("extra") or {})
        kwargs["extra"] = extra
        return msg, kwargs


def site_logger(site: str) -> SiteLoggerAdapter:
    """The logger a poller, or anything else owned by one site, should use."""
    return SiteLoggerAdapter(log, {"site": site})


def setup_logging(verbose: bool, log_path: Path) -> RingBufferHandler:
    """Log to a rotating file only -- stdout belongs to the Live region.

    Two handlers, and deliberately never a third: the rotating file, which is
    the only sink anything is written to, and the ring buffer the --verbose
    panel reads. No stream handler is attached here and none should be added
    anywhere else; a `logging.StreamHandler` on this logger writes into the
    frame Rich is repainting and tears it on every refresh.

    Idempotent by construction. Every handler left behind by an earlier call
    is removed and closed before the new pair goes on, so calling this twice
    -- a test, a reload, a future `collect` that shares the process -- neither
    doubles every line in the file nor leaks the file handle of the handler
    it replaced.

    `log_path` is passed in rather than derived: it lives beside the resolved
    database (see `paths.log_path_for`), which is a decision the config
    already made and this module has no business making a second time. Its
    parent is created here because the platform data directory frequently
    does not exist yet on a first run.
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    # Propagation off for the same reason there is no stream handler: the
    # root logger acquires a stderr handler the moment anything in the
    # process calls basicConfig, and every line would then be echoed into the
    # display.
    log.propagate = False

    for handler in list(log.handlers):
        log.removeHandler(handler)
        # Closed, not merely detached. A RotatingFileHandler holds its file
        # open, and on Windows an open handle keeps the file locked: the next
        # rotation -- which renames the file underneath itself -- fails on a
        # handler nobody is using any more.
        handler.close()

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        # Explicit, because the platform default on Windows is the ANSI code
        # page and property display names are whatever the account's owner
        # typed. A UnicodeEncodeError raised inside the logging call would
        # surface as a failed poll for a reason that has nothing to do with
        # the API.
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(FILE_FORMAT))
    file_handler.addFilter(SiteContextFilter())
    log.addHandler(file_handler)

    ring = RingBufferHandler()
    log.addHandler(ring)
    return ring
