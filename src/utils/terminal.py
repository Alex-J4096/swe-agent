"""Small terminal input helpers for interruptible confirmation prompts."""

from __future__ import annotations

import os
import sys
from typing import Tuple


ESCAPE = "\x1b"


def read_confirmation(question: str) -> Tuple[str, bool]:
    """Read a one-key confirmation on TTYs, with Esc available immediately."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        try:
            return input(question).strip().lower(), False
        except (EOFError, KeyboardInterrupt):
            return ESCAPE, False

    try:
        import termios
        import tty
    except ImportError:  # pragma: no cover
        return input(question).strip().lower(), False

    sys.stdout.write(question)
    sys.stdout.flush()
    file_descriptor = sys.stdin.fileno()
    previous = termios.tcgetattr(file_descriptor)
    try:
        tty.setcbreak(file_descriptor)
        while True:
            value = os.read(file_descriptor, 1).decode("utf-8", errors="ignore")
            if not value:
                return ESCAPE, True
            if value in {ESCAPE, "\x03", "\x04"}:
                return ESCAPE, True
            answer = value.lower()
            if answer in {"y", "a", "n", "c"}:
                return answer, True
    finally:
        termios.tcsetattr(file_descriptor, termios.TCSADRAIN, previous)
