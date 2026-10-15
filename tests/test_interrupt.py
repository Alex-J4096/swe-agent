import os
import select
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from src.utils.interrupt import finish_interrupted_turn


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
        os.write(master, b"\x1b\x1b\x1b")
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
    monkeypatch.setattr(sandbox.os, "killpg", lambda *args: killed.append(args))
    with pytest.raises(TurnInterrupted):
        executor.execute("sleep 30", cwd=tmp_path, timeout_seconds=60,
                         policy=sandbox.SandboxPolicy(workspace=tmp_path))
    assert killed == [(12345, sandbox.signal.SIGKILL)]
    assert calls == [60, None]
