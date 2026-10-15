"""OS-backed command execution sandbox.

The sandbox is deliberately fail-closed: when no supported backend is
available, commands are not silently executed on the host.
"""

from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


MAX_CAPTURE_CHARS = 12_000


@dataclass(frozen=True)
class SandboxPolicy:
    workspace: Path
    allow_network: bool = False


@dataclass(frozen=True)
class CommandExecutionResult:
    stdout: str
    stderr: str
    exit_code: int
    sandboxed: bool
    backend: str


class CommandExecutor(Protocol):
    @property
    def available(self) -> bool: ...

    @property
    def backend_name(self) -> str: ...

    def execute(
        self,
        command: str,
        *,
        cwd: Path,
        timeout_seconds: int,
        policy: SandboxPolicy,
    ) -> CommandExecutionResult: ...


class SandboxedCommandExecutor:
    """Execute shell commands through sandbox-exec or bubblewrap."""

    SAFE_ENV_KEYS = frozenset(
        {
            "PATH",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TERM",
            "COLORTERM",
            "TZ",
        }
    )
    SENSITIVE_HOME_PATHS = (
        ".ssh",
        ".aws",
        ".kube",
        ".gnupg",
        ".docker",
        ".config/gcloud",
        ".netrc",
        ".npmrc",
        ".pypirc",
    )

    def __init__(self, backend: str | None = None) -> None:
        self._backend = backend or self._detect_backend()

    @staticmethod
    def _detect_backend() -> str:
        if platform.system() == "Darwin" and Path("/usr/bin/sandbox-exec").is_file():
            return "sandbox-exec"
        if platform.system() == "Linux" and shutil.which("bwrap"):
            return "bubblewrap"
        return "unavailable"

    @property
    def available(self) -> bool:
        return self._backend != "unavailable"

    @property
    def backend_name(self) -> str:
        return self._backend

    @staticmethod
    def _seatbelt_string(value: Path | str) -> str:
        text = str(value)
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _macos_profile(self, policy: SandboxPolicy, private_home: Path) -> str:
        workspace = policy.workspace.resolve()
        writable = (workspace, private_home)
        lines = [
            "(version 1)",
            "(allow default)",
            "(deny file-write*)",
        ]
        for path in writable:
            lines.append(
                f"(allow file-write* (subpath {self._seatbelt_string(path)}))"
            )
        lines.append('(allow file-write* (literal "/dev/null"))')
        if not policy.allow_network:
            lines.append("(deny network*)")

        real_home = Path.home().resolve()
        for relative in self.SENSITIVE_HOME_PATHS:
            sensitive = real_home / relative
            clause = "subpath" if sensitive.is_dir() else "literal"
            lines.append(
                f"(deny file-read* ({clause} {self._seatbelt_string(sensitive)}))"
            )
        return "\n".join(lines)

    def _bubblewrap_command(
        self,
        command: str,
        *,
        cwd: Path,
        policy: SandboxPolicy,
        private_home: Path,
    ) -> list[str]:
        args = [
            "bwrap",
            "--die-with-parent",
            "--new-session",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--bind",
            str(policy.workspace.resolve()),
            str(policy.workspace.resolve()),
            "--bind",
            str(private_home),
            str(private_home),
            "--chdir",
            str(cwd),
        ]
        if not policy.allow_network:
            args.append("--unshare-net")
        real_home = Path.home().resolve()
        for relative in self.SENSITIVE_HOME_PATHS:
            sensitive = real_home / relative
            if sensitive.is_dir():
                args.extend(["--tmpfs", str(sensitive)])
            elif sensitive.exists():
                args.extend(["--ro-bind", "/dev/null", str(sensitive)])
        args.extend(["/bin/sh", "-lc", command])
        return args

    def _command(
        self,
        command: str,
        *,
        cwd: Path,
        policy: SandboxPolicy,
        private_home: Path,
    ) -> list[str]:
        if self._backend == "sandbox-exec":
            return [
                "/usr/bin/sandbox-exec",
                "-p",
                self._macos_profile(policy, private_home),
                "/bin/sh",
                "-lc",
                command,
            ]
        if self._backend == "bubblewrap":
            return self._bubblewrap_command(
                command,
                cwd=cwd,
                policy=policy,
                private_home=private_home,
            )
        raise RuntimeError(
            "No supported sandbox backend is available. Install bubblewrap on Linux."
        )

    def _environment(self, private_home: Path) -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in self.SAFE_ENV_KEYS
        }
        environment.update(
            {
                "HOME": str(private_home),
                "TMPDIR": str(private_home / "tmp"),
                "XDG_CACHE_HOME": str(private_home / ".cache"),
                "XDG_CONFIG_HOME": str(private_home / ".config"),
                "XDG_DATA_HOME": str(private_home / ".local" / "share"),
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        for key in ("TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
            Path(environment[key]).mkdir(parents=True, exist_ok=True)
        return environment

    def execute(
        self,
        command: str,
        *,
        cwd: Path,
        timeout_seconds: int,
        policy: SandboxPolicy,
    ) -> CommandExecutionResult:
        workspace = policy.workspace.resolve()
        resolved_cwd = cwd.resolve()
        if not resolved_cwd.is_relative_to(workspace):
            return CommandExecutionResult(
                stdout="",
                stderr="cwd escapes sandbox workspace",
                exit_code=-1,
                sandboxed=True,
                backend=self.backend_name,
            )
        if not self.available:
            return CommandExecutionResult(
                stdout="",
                stderr=(
                    "No supported sandbox backend is available; host execution "
                    "was not attempted"
                ),
                exit_code=-1,
                sandboxed=False,
                backend=self.backend_name,
            )

        with tempfile.TemporaryDirectory(prefix="swe-agent-sandbox-") as directory:
            private_home = Path(directory).resolve()
            argv = self._command(
                command,
                cwd=resolved_cwd,
                policy=SandboxPolicy(
                    workspace=workspace,
                    allow_network=policy.allow_network,
                ),
                private_home=private_home,
            )
            process = subprocess.Popen(
                argv,
                cwd=resolved_cwd,
                env=self._environment(private_home),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = process.communicate()
                timeout_message = f"Timeout after {timeout_seconds}s"
                stderr = f"{stderr}\n{timeout_message}" if stderr else timeout_message
                return CommandExecutionResult(
                    stdout=stdout[-MAX_CAPTURE_CHARS:],
                    stderr=stderr[-MAX_CAPTURE_CHARS:],
                    exit_code=-1,
                    sandboxed=True,
                    backend=self.backend_name,
                )

            except BaseException:
                # A stopped foreground turn must not leave its shell running.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
                raise

            return CommandExecutionResult(
                stdout=stdout[-MAX_CAPTURE_CHARS:],
                stderr=stderr[-MAX_CAPTURE_CHARS:],
                exit_code=process.returncode,
                sandboxed=True,
                backend=self.backend_name,
            )
