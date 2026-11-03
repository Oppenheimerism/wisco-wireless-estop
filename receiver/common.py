"""Small helpers shared by sender.py and receiver.py: timestamped logging and
single-keypress input that works both in a real terminal and from a pipe."""

import os
import sys
import termios
import time
import tty

_COLORS = {"red": "31", "green": "32", "yellow": "33", "cyan": "36", "bold": "1"}
_USE_COLOR = sys.stdout.isatty()
_listeners = []


def add_log_listener(fn):
    """fn(stamp, msg, color) is called for every log line (used by the learning site)."""
    _listeners.append(fn)


def log(msg, color=None):
    """Print one line with a wall-clock timestamp (HH:MM:SS.mmm)."""
    t = time.time()
    stamp = time.strftime("%H:%M:%S", time.localtime(t)) + f".{int(t * 1000) % 1000:03d}"
    for fn in _listeners:
        fn(stamp, msg, color)
    if color and _USE_COLOR:
        msg = f"\033[{_COLORS[color]}m{msg}\033[0m"
    print(f"[{stamp}] {msg}", flush=True)


class KeyReader:
    """Read one key at a time without waiting for Enter.

    In a terminal it switches stdin to cbreak mode (restored on exit).
    When stdin is a pipe (used for automated tests) it just reads bytes.
    Use it with select(): it has a fileno(), and read_key() returns one
    lowercase character, or None once stdin is closed.
    """

    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.is_tty = os.isatty(self.fd)
        self.eof = False
        self._old = None

    def __enter__(self):
        if self.is_tty:
            self._old = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc):
        if self._old is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self._old)

    def fileno(self):
        return self.fd

    def read_key(self):
        data = os.read(self.fd, 1)
        if not data:
            self.eof = True
            return None
        return data.decode(errors="ignore").lower()

    def select_list(self):
        """What to pass to select(): nothing once stdin has hit EOF."""
        return [] if self.eof else [self]
