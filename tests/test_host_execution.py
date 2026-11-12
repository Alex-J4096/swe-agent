from contextlib import nullcontext
import json
from io import StringIO
from pathlib import Path
from threading import Event, Timer
from types import SimpleNamespace

import pytest

from main import parse_args
from src.runtime.approval import ApprovalRuntime, ApprovalSource, ApprovalResponse
from src.runtime.agent_runner import AgentRunner
from src.runtime.host_executor import HostCommandExecutor
from src.runtime.permissions import PermissionPolicy
from src.runtime.sandbox import SandboxPolicy, SandboxedCommandExecutor
from src.runtime.session import Session
from src.tools.base import ToolContext
from src.tools.shell.bash import BashArgs, BashTool
from src.tools.toolset import ToolSet
from src.ui.console import ConsoleRenderer
from src.utils import interrupt
from src.utils.slash_commands.base import CommandContext
from src.utils.slash_commands.commands.permissions import PermissionsCommand
from src.utils.slash_commands.commands.status import StatusCommand


def test_host_mode_requires_explicit_cli_flag():
    assert parse_args([]).execution_mode == "sandbox"
    assert parse_args(["--execution-mode", "host"]).execution_mode == "host"
    with pytest.raises(SystemExit):
        parse_args(["--execution-mode", "auto"])
    assert isinstance(ToolSet().registry.get("run_bash").executor, SandboxedCommandExecutor)


@pytest.mark.parametrize("command", ["pwd", "git status", "curl.exe --head https://example.com"])
def test_every_host_command_requires_once_only_even_with_saved_scope(tmp_path, command):
    policy = PermissionPolicy(tmp_path, "host")
    session = Session(model="test")
    decision = policy.evaluate("run_bash", {"command": command}, session=session)
    assert decision.outcome == "require_approval"
    assert not decision.reusable and "host.execute" in decision.required_capabilities
    assert decision.risk == "high"
    session.approved_permission_scopes.add(decision.scope_key)
    assert policy.evaluate("run_bash", {"command": command}, session=session).outcome == "require_approval"
    if command.startswith("curl"):
        assert "host.network" in decision.required_capabilities
        assert "sandbox.network" not in decision.required_capabilities


def test_host_policy_retains_hard_denials_and_cwd_boundary(tmp_path):
    policy = PermissionPolicy(tmp_path, "host")
    assert policy.evaluate("run_bash", {"command": "sudo ls"}).outcome == "deny"
    assert policy.evaluate("run_bash", {"command": "pwd", "cwd": ".."}).outcome == "deny"


def test_host_tool_cannot_execute_without_dispatch_approval(tmp_path):
    class Executor:
        execution_mode = "host"
        def execute(self, *args, **kwargs):
            pytest.fail("Unapproved command must not run")
    result = BashTool(tmp_path, Executor()).run(BashArgs(command="pwd"), ToolContext(Session(model="test")))
    assert not result["ok"] and "approval" in result["error"]


def test_host_environment_drops_credentials_and_uses_private_home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret-test")
    monkeypatch.setenv("SSH_AUTH_SOCK", "secret-test")
    env = HostCommandExecutor()._environment(tmp_path)
    assert not ({"OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "SSH_AUTH_SOCK"} & env.keys())
    assert env["HOME"] == env["USERPROFILE"] == str(tmp_path)
    assert Path(env["TEMP"]).is_dir()


@pytest.fixture
def host():
    executor = HostCommandExecutor()
    if not executor.available:
        pytest.skip("No host shell installed")
    return executor


def test_host_executes_local_command_in_workspace_with_utf8(host, tmp_path):
    command = "Write-Output '中文'; (Get-Location).Path" if host.windows else "printf '中文\\n'; pwd"
    result = host.execute(command, cwd=tmp_path, timeout_seconds=5, policy=SandboxPolicy(tmp_path))
    assert result.exit_code == 0, result.stderr
    assert "中文" in result.stdout and str(tmp_path) in result.stdout
    assert not result.sandboxed and result.backend.startswith("host-")


def test_host_reports_exit_status_and_limits_output(host, tmp_path):
    command = "Write-Output ('x' * 15000); exit 7" if host.windows else "printf '%15000s' ''; exit 7"
    result = host.execute(command, cwd=tmp_path, timeout_seconds=5, policy=SandboxPolicy(tmp_path))
    assert result.exit_code == 7
    assert len(result.stdout) == 12000


def test_host_propagates_native_command_failure(host, tmp_path):
    command = "cmd.exe /c exit 9" if host.windows else "sh -c 'exit 9'"
    result = host.execute(command, cwd=tmp_path, timeout_seconds=5, policy=SandboxPolicy(tmp_path))
    assert result.exit_code == 9


def test_missing_host_shell_does_not_fall_back(tmp_path, monkeypatch):
    executor = HostCommandExecutor()
    executor.shell = None
    monkeypatch.setattr("src.runtime.host_executor.subprocess.Popen", lambda *a, **kw: pytest.fail("must not spawn"))
    result = executor.execute("exit 0", cwd=tmp_path, timeout_seconds=5, policy=SandboxPolicy(tmp_path))
    assert result.exit_code == -1 and "unavailable" in result.stderr


def test_host_rejects_cwd_outside_workspace(host, tmp_path):
    result = host.execute("exit 0", cwd=tmp_path.parent, timeout_seconds=5, policy=SandboxPolicy(tmp_path))
    assert result.exit_code == -1 and "cwd escapes" in result.stderr


def test_host_timeout_stops_waiting(host, tmp_path):
    command = "Start-Sleep -Seconds 10" if host.windows else "sleep 10"
    result = host.execute(command, cwd=tmp_path, timeout_seconds=0.2, policy=SandboxPolicy(tmp_path))
    assert result.exit_code == -1 and "Timeout" in result.stderr


def test_host_cancellation_terminates_process(host, tmp_path, monkeypatch):
    monkeypatch.setattr(interrupt, "_terminal_listener", lambda *args: nullcontext())
    cancelled = Event()
    timer = Timer(0.3, cancelled.set)
    timer.start()
    command = "Start-Sleep -Seconds 10" if host.windows else "sleep 10"
    try:
        with interrupt.interruptible_turn(cancelled), pytest.raises(interrupt.TurnInterrupted):
            host.execute(command, cwd=tmp_path, timeout_seconds=5, policy=SandboxPolicy(tmp_path))
    finally:
        timer.cancel()
        timer.join()


def test_powershell_runs_without_profile_and_propagates_native_exit(host):
    host.windows = True
    argv = host._command("curl.exe --head https://example.com")
    assert argv[1:5] == ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"]
    assert "curl.exe --head" in argv[-1] and "exit $LASTEXITCODE" in argv[-1]


def test_host_approval_card_background_command_and_status_are_explicit(tmp_path):
    runtime = ApprovalRuntime()
    request = runtime.create_request(
        tool_call_id="call", tool_name="run_bash", arguments_json='{"command":"pwd"}',
        reason="host", preview="pwd", scope_key="host:test", capability="host.execute",
        capabilities=("host.execute",), reusable=False, cwd=str(tmp_path), risk="high",
        source=ApprovalSource(kind="team", session_id="session"),
    )
    stream = StringIO()
    renderer = ConsoleRenderer(output=stream, use_color=False)
    renderer.permission_request(request)
    assert "NO OS SANDBOX" in stream.getvalue()
    assert "sandbox remains enforced" not in stream.getvalue()
    ctx = CommandContext(session=Session(model="test"), provider=object(),
                         toolset=ToolSet(command_executor=HostCommandExecutor()),
                         approval_runtime=runtime, workdir=tmp_path)
    assert "no OS filesystem/network isolation" in PermissionsCommand().execute([], ctx).message
    assert "NO OS SANDBOX" in StatusCommand().execute([], ctx).message
    assert not runtime.resolve(request.id, ApprovalResponse("approve_session"))
    assert runtime.get_request(request.id).status == "pending"


def test_runner_infers_host_mode_from_tool_and_does_not_reuse_grants(tmp_path):
    from src.runtime.sandbox import CommandExecutionResult

    class Executor:
        execution_mode = "host"
        backend_name = "host-test"
        windows = True
        def execute(self, *args, **kwargs):
            return CommandExecutionResult("ok", "", 0, False, self.backend_name)

    call = SimpleNamespace(id="call", function=SimpleNamespace(name="run_bash", arguments='{"command":"pwd"}'))
    batches = iter([[call], [call], None])
    provider = SimpleNamespace(client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="done", tool_calls=next(batches)))])
    ))))
    runtime = ApprovalRuntime()
    runner = AgentRunner(provider, workdir=tmp_path, approval_runtime=runtime)
    prompts = []
    toolset = ToolSet(command_executor=Executor())
    result = runner.run(Session(model="test"), toolset, "system", confirm_permission=lambda *a: prompts.append(1) or True)
    assert result.content == "done" and len(prompts) == 2
    assert "PowerShell" in toolset.registry.get("run_bash").description


def test_runner_executes_host_tool_in_bound_workspace_after_approval(host, tmp_path):
    workspace = tmp_path / "task-workspace"
    workspace.mkdir()
    command = "(Get-Location).Path" if host.windows else "pwd"
    call = SimpleNamespace(id="call", function=SimpleNamespace(name="run_bash", arguments=json.dumps({"command": command})))
    batches = iter([[call], None])
    provider = SimpleNamespace(client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="done", tool_calls=next(batches)))])
    ))))
    session = Session(model="test")
    runner = AgentRunner(provider, workdir=tmp_path)
    requests = []
    runner.approval_runtime.subscribe(
        lambda event: requests.append(event.request) if event.kind == "request_created" else None
    )
    results = []
    result = runner.run(
        session, ToolSet(command_executor=host), "system",
        tool_context=ToolContext(session, workspace_root=workspace),
        confirm_permission=lambda *args: True,
        on_tool_result=lambda name, output: results.append(output),
    )
    assert result.ok and results[0]["ok"]
    assert str(workspace) in results[0]["stdout"]
    assert results[0]["execution_mode"] == "host" and not results[0]["sandboxed"]
    request = requests[0]
    assert request.cwd == str(workspace) and not request.reusable
