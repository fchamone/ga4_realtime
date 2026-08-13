"""Box characters, the ascii fallback, and whether colour is wanted at all.

Three questions every panel asks and none of them should answer twice. All
three are about the *terminal* rather than about the data, which is why they
sit below the panels rather than inside them: `--ascii` has to reach the
plot's own axis characters as well as the panel borders, and a `NO_COLOR`
that only some panels honoured would be worse than one nobody did.
"""

import os
import sys
from collections.abc import Mapping

from rich import box

# plotext draws its axes with box-drawing characters whatever the theme, and
# those are no more encodable on a legacy code page than the braille markers
# are. --ascii has to flatten them too, or it only half works.
_ASCII_FRAME = str.maketrans(
    {
        "─": "-",
        "━": "-",
        "│": "|",
        "┃": "|",
        "┌": "+",
        "┐": "+",
        "└": "+",
        "┘": "+",
        "├": "+",
        "┤": "+",
        "┬": "+",
        "┴": "+",
        "┼": "+",
        "╌": "-",
        "╎": "|",
    }
)


def frame_box(ascii_mode: bool) -> box.Box:
    """Border style for every panel and table, so --ascii covers them too."""
    return box.ASCII if ascii_mode else box.ROUNDED


def stdout_supports_blocks() -> bool:
    """Can stdout encode the braille and block glyphs the plots use?

    A legacy Windows code page cannot, and the failure mode is a
    UnicodeEncodeError thrown from inside the Live refresh -- a crash in the
    middle of the dashboard rather than an obviously wrong character. Checking
    up front turns that into an automatic fall back to --ascii.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "█⣿".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def no_color_requested(env: Mapping[str, str] | None = None) -> bool:
    """Honour NO_COLOR: set and non-empty means every style is dropped.

    The convention (no-color.org) is that the value carries no meaning -- only
    whether the variable is set to something. `NO_COLOR=` is therefore *not* a
    request, which is what lets a shell unset it for one command by exporting
    it empty.

    Resolved once by the caller and passed down as a plain bool rather than
    re-read in each panel: the dashboard is drawn several times a minute, and
    a rendering decision that changes with the environment mid-run would be
    a display that quietly disagrees with itself between frames.
    """
    return bool((os.environ if env is None else env).get("NO_COLOR"))
