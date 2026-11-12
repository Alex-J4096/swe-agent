from io import StringIO
from types import SimpleNamespace
import sys

import pytest

from src.utils import terminal


@pytest.fixture
def windows_console(monkeypatch):
    class TTY(StringIO):
        def isatty(self):
            return True

    output = TTY()
    monkeypatch.setattr(terminal, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(terminal, "sys", SimpleNamespace(stdin=TTY(), stdout=output))

    def keys(values):
        iterator = iter(values)
        def getwch():
            # The confirmation must own input, excluding the turn's Esc listener.
            assert terminal.terminal_input_lock.locked()
            return next(iterator)
        monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(getwch=getwch))

    return output, keys


@pytest.mark.parametrize("key", ["y", "Y", "n", "t", "a", "s", "c", "1", "2", "3"])
def test_windows_confirmation_accepts_one_key_without_enter(windows_console, key):
    output, keys = windows_console
    keys([key])  # No newline or Enter is supplied.
    assert terminal.read_confirmation("Choose: ") == (key.lower(), True)
    assert output.getvalue() == "Choose: " + key.lower()
    assert not terminal.terminal_input_lock.locked()


@pytest.mark.parametrize("key", ["\x1b", "\x03", "\x04", "\x1a"])
def test_windows_cancel_is_immediate(windows_console, key):
    _, keys = windows_console
    keys([key])
    assert terminal.read_confirmation("Choose: ") == (terminal.ESCAPE, True)


def test_windows_ignores_navigation_invalid_keys_and_enter(windows_console):
    _, keys = windows_console
    # H is Up; a function-key scan code can itself look like a valid answer.
    keys(["\xe0", "H", "\x00", "a", "z", "\r", "\n", "y"])
    assert terminal.read_confirmation("Choose: ") == ("y", True)


def test_non_tty_keeps_line_input_fallback(monkeypatch):
    monkeypatch.setattr(terminal, "sys", SimpleNamespace(
        stdin=StringIO(), stdout=StringIO()))
    monkeypatch.setattr("builtins.input", lambda question: " YES ")
    assert terminal.read_confirmation("Choose: ") == ("yes", False)
