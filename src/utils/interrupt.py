"""Interrupt a foreground turn with Escape while keeping the CLI alive."""

from contextlib import contextmanager
from contextvars import ContextVar, copy_context
import os
import select
import signal
import sys
from threading import Event, Lock, Thread


terminal_input_lock = Lock()
_cancel_event: ContextVar[Event | None] = ContextVar("turn_cancel_event", default=None)


class TurnInterrupted(BaseException):
    """User stopped the current turn (must bypass tool error handlers)."""


def check_interrupted():
    event = _cancel_event.get()
    if event is not None and event.is_set():
        raise TurnInterrupted()


def interruptible_sleep(seconds):
    event = _cancel_event.get()
    if event is None:
        import time
        time.sleep(seconds)
    elif event.wait(seconds):
        raise TurnInterrupted()


def run_interruptible(operation):
    """Wait for read-only network work without blocking foreground cancellation.

    A late result is discarded. The worker must not mutate Session or run tools.
    """
    event = _cancel_event.get()
    if event is None:
        return operation()
    check_interrupted()
    ready = Event()
    result = []
    errors = []
    context = copy_context()

    def work():
        try:
            check_interrupted()
            result.append(operation())
        except BaseException as exc:
            errors.append(exc)
        finally:
            ready.set()

    Thread(target=lambda: context.run(work), name="model-request", daemon=True).start()
    try:
        while not ready.wait(0.05):
            check_interrupted()
        check_interrupted()
        if errors:
            raise errors[0]
        return result[0]
    except (TurnInterrupted, KeyboardInterrupt):
        event.set()
        raise


class _EscapeConfirmation:
    def __init__(self, cancelled, on_stop_requested=None):
        self.cancelled = cancelled
        self.pending = False
        self.on_stop_requested = on_stop_requested

    def press(self):
        if not self.pending:
            self.pending = True
            if self.on_stop_requested is not None:
                self.on_stop_requested()
            else:
                print("\nPress Esc again to stop this turn; Ctrl+C stops immediately.", flush=True)
            return False
        self.cancelled.set()
        return True


def _watch_windows_keys(console, stopped, cancelled, on_stop_requested=None):
    escape = _EscapeConfirmation(cancelled, on_stop_requested)
    while not stopped.is_set():
        # Confirmation prompts own input while asking a question.
        if terminal_input_lock.acquire(blocking=False):
            try:
                if console.kbhit():
                    key = console.getwch()
                    if key in {"\x00", "\xe0"}:
                        console.getwch()  # Arrow/function key, not Escape.
                    elif key == "\x03":
                        cancelled.set()
                        return
                    elif key == "\x1b" and escape.press():
                        return
            finally:
                terminal_input_lock.release()
        stopped.wait(0.02)


@contextmanager
def interruptible_turn(cancel_event=None, on_stop_requested=None):
    cancelled = cancel_event if cancel_event is not None else Event()
    token = _cancel_event.set(cancelled)
    try:
        with _terminal_listener(cancelled, on_stop_requested):
            yield cancelled
    except (TurnInterrupted, KeyboardInterrupt):
        cancelled.set()
        raise
    finally:
        _cancel_event.reset(token)


@contextmanager
def _terminal_listener(cancelled, on_stop_requested=None):
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        yield
        return
    if os.name == "nt":
        import msvcrt
        stopped = Event()
        watcher = Thread(target=_watch_windows_keys, args=(msvcrt, stopped, cancelled, on_stop_requested),
                         name="escape-listener", daemon=True)
        watcher.start()
        try:
            yield
        finally:
            stopped.set()
            watcher.join()
        return
    if os.name != "posix":
        yield
        return

    import termios
    import tty

    fd = sys.stdin.fileno()
    previous_terminal = termios.tcgetattr(fd)
    previous_handler = signal.getsignal(signal.SIGUSR1)
    stopped = Event()
    escape = _EscapeConfirmation(cancelled, on_stop_requested)

    def interrupt(signum, frame):
        cancelled.set()
        raise TurnInterrupted()

    def watch():
        while not stopped.is_set():
            if terminal_input_lock.acquire(blocking=False):
                try:
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
                                for _ in range(2 if following == b"\x1b" else 1):
                                    if escape.press():
                                        os.kill(os.getpid(), signal.SIGUSR1)
                                        return
                finally:
                    terminal_input_lock.release()
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
        "content": "[User stopped this turn. Wait for the next user request. Previous changes are retained.]",
    })
