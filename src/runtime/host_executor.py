"""Explicit, approved execution on the host; this is not an OS sandbox."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
from time import monotonic

from src.runtime.sandbox import CommandExecutionResult, MAX_CAPTURE_CHARS, SandboxPolicy
from src.utils.interrupt import check_interrupted


class HostCommandExecutor:
    execution_mode = "host"

    def __init__(self):
        self.windows = os.name == "nt"
        self.shell = (
            (shutil.which("pwsh.exe") or shutil.which("powershell.exe"))
            if self.windows else shutil.which("sh")
        )

    @property
    def available(self):
        return self.shell is not None

    @property
    def backend_name(self):
        return "host-powershell" if self.windows else "host-sh"

    def _command(self, command):
        if self.windows:
            # Keep native-command output UTF-8 and propagate its exit status.
            source = (
                "$ErrorActionPreference = 'Stop'; "
                "$OutputEncoding = [Console]::OutputEncoding = "
                "[System.Text.UTF8Encoding]::new(); & {\n" + command +
                "\n}; if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }"
            )
            return [self.shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", source]
        return [self.shell, "-c", command]

    def _environment(self, home):
        allowed = {
            "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE",
            "LANG", "LC_ALL", "LC_CTYPE", "TERM", "COLORTERM", "TZ",
            "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
        }
        env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
        for relative in ("tmp", "cache", "config", "data", "appdata", "localappdata"):
            (home / relative).mkdir()
        env.update({
            "HOME": str(home), "USERPROFILE": str(home),
            "TMP": str(home / "tmp"), "TEMP": str(home / "tmp"), "TMPDIR": str(home / "tmp"),
            "APPDATA": str(home / "appdata"), "LOCALAPPDATA": str(home / "localappdata"),
            "XDG_CACHE_HOME": str(home / "cache"), "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_DATA_HOME": str(home / "data"),
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "PYTHONIOENCODING": "utf-8",
        })
        return env

    def _terminate(self, process):
        if self.windows:
            # Built-in Windows utility, no additional sandbox/service required.
            taskkill = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "taskkill.exe")
            try:
                subprocess.run([taskkill, "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            finally:
                if process.poll() is None:
                    process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def execute(self, command, *, cwd, timeout_seconds, policy: SandboxPolicy):
        cwd = Path(cwd).resolve()
        if not cwd.is_relative_to(policy.workspace.resolve()):
            return CommandExecutionResult("", "cwd escapes workspace", -1, False, self.backend_name)
        if not self.available:
            return CommandExecutionResult("", "Host shell is unavailable", -1, False, self.backend_name)
        check_interrupted()
        with tempfile.TemporaryDirectory(prefix="swe-agent-host-") as directory:
            options = (
                {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
                if self.windows else {"start_new_session": True}
            )
            process = subprocess.Popen(
                self._command(command), cwd=cwd, env=self._environment(Path(directory)),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", **options,
            )
            deadline = monotonic() + timeout_seconds
            try:
                while True:
                    check_interrupted()
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        self._terminate(process)
                        stdout, stderr = process.communicate(timeout=5)
                        stderr = f"{stderr}\nTimeout after {timeout_seconds}s"
                        code = -1
                        break
                    try:
                        stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                        code = process.returncode
                        break
                    except subprocess.TimeoutExpired:
                        continue
            except BaseException:
                self._terminate(process)
                process.communicate(timeout=5)
                raise
            return CommandExecutionResult(stdout[-MAX_CAPTURE_CHARS:], stderr[-MAX_CAPTURE_CHARS:],
                                          code, False, self.backend_name)
