import os
import select
import subprocess
import sys
import time
from contextlib import nullcontext
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from src.utils.interrupt import finish_interrupted_turn
from src.utils import interrupt


@pytest.fixture
def cancellation_scope(monkeypatch):
    # Exercise foreground cancellation without requiring a physical terminal.
    monkeypatch.setattr(interrupt, "_terminal_listener", lambda event, on_stop_requested=None: nullcontext())
    return interrupt.interruptible_turn


def test_blocked_request_stops_before_response_and_discards_late_tool_calls(tmp_path, cancellation_scope):
    from src.runtime.agent_runner import AgentRunner
    from src.runtime.session import Session

    started, release, finished = Event(), Event(), Event()
    cancelled = Event()
    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        started.set()
        try:
            assert release.wait(5)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="late response", tool_calls=[SimpleNamespace()]))])
        finally:
            finished.set()

    def cancel_when_started():
        if started.wait(2):
            cancelled.set()

    canceller = Thread(target=cancel_when_started)
    canceller.start()
    renderer_events, dispatched = [], []
    renderer = SimpleNamespace(
        start_thinking=lambda *args: renderer_events.append("start"),
        stop_thinking=lambda: renderer_events.append("stop"),
    )
    provider = SimpleNamespace(client=SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create))))
    runner = AgentRunner(provider, workdir=tmp_path, renderer=renderer)
    session = Session(model="before", history=[{"role": "user", "content": "task"}])
    tools = SimpleNamespace(schemas=lambda: [], dispatch=lambda *a, **kw: dispatched.append(a))
    try:
        with pytest.raises(interrupt.TurnInterrupted):
            with cancellation_scope(cancelled):
                runner.run(session, tools, "system")
        assert not finished.is_set()  # Cancellation did not wait for the network.
        assert renderer_events == ["start", "stop"]
        finish_interrupted_turn(session)
        session.model = "after"
        session.history[0]["content"] = "changed"
        stopped_history = list(session.history)
        release.set()
        assert finished.wait(2)
        assert session.history == stopped_history
        assert dispatched == []
        assert requests[0]["model"] == "before"
        assert requests[0]["messages"][1]["content"] == "task"
        # A new turn has its own cancellation state.
        with cancellation_scope():
            assert interrupt.run_interruptible(lambda: "next request") == "next request"
    finally:
        release.set()
        canceller.join(timeout=2)


def test_cancellation_interrupts_retry_wait_without_another_request(cancellation_scope):
    from openai import OpenAIError
    from src.infrastructure.retry import request_with_retry

    cancelled, first_attempt = Event(), Event()
    calls = []

    def request():
        calls.append(1)
        first_attempt.set()
        error = OpenAIError("limited")
        error.status_code = 429
        error.headers = {"Retry-After": "30"}
        raise error

    def cancel():
        if first_attempt.wait(2):
            cancelled.set()

    canceller = Thread(target=cancel)
    canceller.start()
    try:
        with cancellation_scope(cancelled), pytest.raises(interrupt.TurnInterrupted):
            request_with_retry(request)
        assert calls == [1]
    finally:
        cancelled.set()
        canceller.join(timeout=2)


@pytest.mark.parametrize("key", ["\x1b", "\x03"])
def test_windows_listener_stops_and_ignores_extended_keys(key):
    keys = iter(["\xe0", "H", "\x00", ";", *([key, key] if key == "\x1b" else [key])])
    cancelled, stopped = Event(), Event()
    console = SimpleNamespace(kbhit=lambda: True, getwch=lambda: next(keys))
    interrupt._watch_windows_keys(console, stopped, cancelled)
    assert cancelled.is_set()


def test_windows_listener_yields_input_to_confirmation_and_exits():
    stopped, cancelled, polled = Event(), Event(), Event()
    console = SimpleNamespace(kbhit=lambda: polled.set(), getwch=lambda: "\x1b")
    watcher = Thread(target=interrupt._watch_windows_keys, args=(console, stopped, cancelled))
    with interrupt.terminal_input_lock:
        watcher.start()
        try:
            assert not polled.wait(0.05)
            stopped.set()
            watcher.join(timeout=1)
            assert not watcher.is_alive()
            assert not cancelled.is_set()
        finally:
            stopped.set()
    watcher.join(timeout=1)


def test_first_windows_escape_only_shows_hint():
    stopped, cancelled = Event(), Event()
    hints = []

    def hint():
        hints.append("press again")
        stopped.set()

    console = SimpleNamespace(kbhit=lambda: True, getwch=lambda: "\x1b")
    interrupt._watch_windows_keys(console, stopped, cancelled, hint)
    assert hints == ["press again"]
    assert not cancelled.is_set()


def test_escape_confirmation_is_reset_for_each_turn():
    hints = []
    first, second = Event(), Event()
    gate = interrupt._EscapeConfirmation(first, lambda: hints.append(1))
    assert gate.press() is False
    assert not first.is_set()
    assert gate.press() is True
    assert first.is_set()
    new_gate = interrupt._EscapeConfirmation(second, lambda: hints.append(2))
    assert new_gate.press() is False
    assert not second.is_set()
    assert hints == [1, 2]


def test_windows_turn_installs_listener_and_restores_scope(monkeypatch):
    keys = SimpleNamespace(kbhit=lambda: True, getwch=lambda: "\x1b")
    monkeypatch.setitem(sys.modules, "msvcrt", keys)
    monkeypatch.setattr(interrupt, "os", SimpleNamespace(name="nt"))
    terminal = SimpleNamespace(isatty=lambda: True)
    monkeypatch.setattr(interrupt, "sys", SimpleNamespace(stdin=terminal, stdout=terminal))
    with interrupt.interruptible_turn() as cancelled:
        assert cancelled.wait(1)
        with pytest.raises(interrupt.TurnInterrupted):
            interrupt.check_interrupted()
    interrupt.check_interrupted()  # No cancelled state leaks into the next prompt.


def test_request_error_is_propagated_from_worker(cancellation_scope):
    def fail():
        raise ValueError("test failure")
    with cancellation_scope(), pytest.raises(ValueError, match="test failure"):
        interrupt.run_interruptible(fail)


def test_cancelled_turn_does_not_start_request(cancellation_scope):
    cancelled = Event()
    cancelled.set()
    calls = []
    with cancellation_scope(cancelled), pytest.raises(interrupt.TurnInterrupted):
        interrupt.run_interruptible(lambda: calls.append(1))
    assert calls == []


def test_interrupted_tool_batch_has_one_reply_per_call():
    session = SimpleNamespace(history=[
        {"role": "assistant", "tool_calls": [{"id": "done"}, {"id": "pending"}]},
        {"role": "tool", "tool_call_id": "done", "content": "ok"},
    ])
    finish_interrupted_turn(session)
    replies = [m["tool_call_id"] for m in session.history if m["role"] == "tool"]
    assert replies == ["done", "pending"]
    assert "partially completed" in session.history[-2]["content"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX terminal required")
def test_escape_restores_terminal_and_accepts_next_request():
    import pty
    import termios

    master, slave = pty.openpty()
    previous = termios.tcgetattr(slave)
    code = '''
import time
from src.utils.interrupt import interruptible_turn, TurnInterrupted
try:
    with interruptible_turn():
        print("READY", flush=True)
        time.sleep(30)
except TurnInterrupted:
    print("STOPPED", flush=True)
print("NEXT:" + input(), flush=True)
'''
    process = subprocess.Popen([sys.executable, "-c", code], stdin=slave, stdout=slave, stderr=slave)
    output = b""

    def until(marker):
        nonlocal output
        deadline = time.monotonic() + 5
        while marker not in output and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                output += os.read(master, 4096)
        assert marker in output, output

    try:
        until(b"READY")
        os.write(master, b"\x1b")
        until(b"Press Esc again")
        assert process.poll() is None
        assert b"STOPPED" not in output
        os.write(master, b"\x1b")
        until(b"STOPPED")
        assert termios.tcgetattr(slave) == previous
        os.write(master, b"new task\n")
        until(b"NEXT:new task")
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
        os.close(slave)


def test_interrupted_shell_kills_process_group(tmp_path, monkeypatch):
    from src.runtime import sandbox
    from src.utils.interrupt import TurnInterrupted

    calls = []

    class Process:
        pid = 12345

        def communicate(self, timeout=None):
            calls.append(timeout)
            if timeout is not None:
                raise TurnInterrupted()
            return "", ""

    executor = sandbox.SandboxedCommandExecutor(backend="sandbox-exec")
    monkeypatch.setattr(sandbox.subprocess, "Popen", lambda *a, **kw: Process())
    killed = []
    # The executor backend is simulated; Windows has no native os.killpg.
    monkeypatch.setattr(sandbox.os, "killpg", lambda *args: killed.append(args), raising=False)
    monkeypatch.setattr(sandbox.signal, "SIGKILL", 9, raising=False)
    with pytest.raises(TurnInterrupted):
        executor.execute("sleep 30", cwd=tmp_path, timeout_seconds=60,
                         policy=sandbox.SandboxPolicy(workspace=tmp_path))
    assert killed == [(12345, sandbox.signal.SIGKILL)]
    assert calls == [60, None]
