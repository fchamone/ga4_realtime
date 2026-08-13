"""Single keystrokes, read without blocking and without curses.

The live view has to notice `q` or `Tab` while it is redrawing on a timer,
which rules out `input()` -- it blocks until Enter -- and curses, which would
take the screen over from the `Live` region that already owns it. What is left
is one character at a time, polled, from whichever of the two platform
mechanisms is present.

Its own module rather than a corner of the command, because it is the only
place in the tool that touches terminal *modes*: on POSIX it changes them, and
getting them back is not optional.
"""

import os
import sys
from typing import Self


class KeyReader:
    """Non-blocking single-key reads, without curses.

    On Windows msvcrt does this with no terminal mode changes at all. On POSIX
    the terminal has to be put into cbreak mode, and restoring it lives in
    __exit__ so that an exception mid-render cannot leave the user's shell
    with echo switched off.
    """

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled and sys.stdin is not None and sys.stdin.isatty()
        self._fd = None
        self._saved = None
        self._win = os.name == "nt"

    def __enter__(self) -> Self:
        if not self.enabled:
            return self
        if self._win:
            import msvcrt  # noqa: F401 - imported for availability check

            return self
        try:
            import termios
            import tty

            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        except Exception:  # noqa: BLE001 - not a tty we can drive
            self.enabled = False
        return self

    def __exit__(self, *exc_info) -> None:
        if self._saved is not None and self._fd is not None:
            import termios

            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)

    def poll(self) -> str | None:
        if not self.enabled:
            return None
        if self._win:
            import msvcrt

            if msvcrt.kbhit():
                char = msvcrt.getwch()
                # Arrow and function keys arrive as a two-character sequence;
                # swallow the payload so it is not read as a command. This is
                # also why Shift-Tab is deliberately left unbound: it is one
                # of those sequences, so it never reaches the key table at
                # all, and binding it would mean teaching this reader to
                # assemble sequences -- the first step of reimplementing
                # curses, which the class exists to avoid.
                if char in ("\x00", "\xe0"):
                    msvcrt.getwch()
                    return None
                return char
            return None

        import select

        if select.select([sys.stdin], [], [], 0)[0]:
            return sys.stdin.read(1)
        return None
