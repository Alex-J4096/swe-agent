"""Interrupt a foreground turn with Escape while keeping the CLI alive."""

from contextlib import contextmanager
import os
import select
import signal
import sys
from threading import Event, Lock, Thread


terminal_input_lock = Lock()


class TurnInterrupted(BaseException):
    """User stopped the current turn (must bypass tool error handlers)."""


@contextmanager
def interruptible_turn():
    if not (sys.stdin.isatty() and sys.stdout.isatty()) or os.name != "posix":
        yield
        return

    import termios
    import tty

    fd = sys.stdin.fileno()
    previous_terminal = termios.tcgetattr(fd)
    previous_handler = signal.getsignal(signal.SIGUSR1)
    stopped = Event()

    def interrupt(signum, frame):
        raise TurnInterrupted()

    def watch():
        while not stopped.is_set():
            with terminal_input_lock:
                if select.select([fd], [], [], 0.05)[0]:
                    key = os.read(fd, 1)
                    if key == b"\x1b":
                        # Consume arrow/function key sequences without stopping.
                        following = b""
                        if select.select([fd], [], [], 0.03)[0]:
                            following = os.read(fd, 1)
                        if following in {b"[", b"O"}:
                            while select.select([fd], [], [], 0.01)[0]:
                                os.read(fd, 1)
                        else:
                            os.kill(os.getpid(), signal.SIGUSR1)
                            return
            stopped.wait(0.01)

    signal.signal(signal.SIGUSR1, interrupt)
    tty.setcbreak(fd)
    watcher = Thread(target=watch, name="escape-listener", daemon=True)
    watcher.start()
    try:
        yield
    finally:
        # Ignore repeated Escape while restoring the terminal.
        signal.signal(signal.SIGUSR1, signal.SIG_IGN)
        stopped.set()
        watcher.join()
        termios.tcflush(fd, termios.TCIFLUSH)
        termios.tcsetattr(fd, termios.TCSADRAIN, previous_terminal)
        signal.signal(signal.SIGUSR1, previous_handler)


def finish_interrupted_turn(session):
    """Supply missing tool replies so the next model request stays valid."""
    pending = {}
    for message in session.history:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls", []):
                pending[call["id"]] = call
        elif message.get("role") == "tool":
            pending.pop(message.get("tool_call_id"), None)
    for call_id in pending:
        session.history.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": '{"ok": false, "error": "Interrupted by user. Execution may have partially completed; verify state before retrying."}',
        })
    session.history.append({
        "role": "user",
        "content": "[User pressed Escape to stop this turn. Wait for the next user request. Previous changes are retained.]",
    })
