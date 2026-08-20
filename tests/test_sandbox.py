import os
from pathlib import Path

import pytest

from src.runtime.sandbox import (
    CommandExecutionResult,
    SandboxPolicy,
    SandboxedCommandExecutor,
)
from src.runtime.session import Session
from src.tools.base import ToolContext
from src.tools.shell.bash import BashArgs, BashTool


class RecordingExecutor:
    available = True
    backend_name = "test"

    def __init__(self) -> None:
        self.calls = []

    def execute(self, command, *, cwd, timeout_seconds, policy):
        self.calls.append((command, cwd, timeout_seconds, policy))
        return CommandExecutionResult("ok", "", 0, True, self.backend_name)


def test_bash_tool_always_uses_command_executor(tmp_path: Path) -> None:
    executor = RecordingExecutor()
    tool = BashTool(project_root=tmp_path, executor=executor)

    result = tool.run(
        BashArgs(command="touch marker"),
        ToolContext(session=Session(model="test")),
    )

    assert result["ok"] is True
    assert result["sandboxed"] is True
    assert executor.calls[0][3] == SandboxPolicy(
        workspace=tmp_path.resolve(),
        allow_network=False,
    )


def test_network_capability_only_changes_sandbox_network_policy(tmp_path: Path) -> None:
    executor = RecordingExecutor()
    tool = BashTool(project_root=tmp_path, executor=executor)

    tool.run(
        BashArgs(command="curl https://example.com"),
        ToolContext(
            session=Session(model="test"),
            granted_capabilities=frozenset({"sandbox.network"}),
        ),
    )

    assert executor.calls[0][3].allow_network is True
    assert executor.calls[0][3].workspace == tmp_path.resolve()


def test_sandbox_environment_drops_credentials(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    executor = SandboxedCommandExecutor(backend="unavailable")

    environment = executor._environment(tmp_path)

    assert "OPENAI_API_KEY" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert environment["HOME"] == str(tmp_path)


def test_os_sandbox_allows_workspace_write_and_blocks_escape(tmp_path: Path) -> None:
    executor = SandboxedCommandExecutor()
    if not executor.available:
        pytest.skip("No supported OS sandbox backend")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inside = executor.execute(
        "touch inside.txt",
        cwd=workspace,
        timeout_seconds=5,
        policy=SandboxPolicy(workspace),
    )
    if "sandbox_apply: Operation not permitted" in inside.stderr:
        pytest.skip("The test process is already inside a sandbox that forbids nesting")
    assert inside.exit_code == 0, inside.stderr
    assert (workspace / "inside.txt").exists()

    outside_path = tmp_path / "outside.txt"
    escaped = executor.execute(
        f"touch {outside_path}",
        cwd=workspace,
        timeout_seconds=5,
        policy=SandboxPolicy(workspace),
    )
    assert escaped.exit_code != 0
    assert not outside_path.exists()


def test_unavailable_sandbox_never_falls_back_to_host(tmp_path: Path) -> None:
    executor = SandboxedCommandExecutor(backend="unavailable")
    result = executor.execute(
        "touch marker",
        cwd=tmp_path,
        timeout_seconds=5,
        policy=SandboxPolicy(tmp_path),
    )

    assert result.exit_code == -1
    assert result.sandboxed is False
    assert not (tmp_path / "marker").exists()
