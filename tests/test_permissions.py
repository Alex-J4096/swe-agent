from pathlib import Path
from types import SimpleNamespace

from src.runtime.agent_runner import AgentRunner
from src.runtime.session import Session


def make_runner(tmp_path: Path) -> AgentRunner:
    return AgentRunner(
        provider=SimpleNamespace(client=SimpleNamespace()),
        workdir=tmp_path,
    )


def test_deny_list_reads_command_from_parsed_arguments(tmp_path: Path) -> None:
    runner = make_runner(tmp_path)

    reason, hard_denied = runner._check_permission(
        "run_bash",
        {"command": "sudo ls"},
    )

    assert reason == "Blocked 'sudo' is on the deny list"
    assert hard_denied is True


def test_non_denied_bash_command_requires_permission(tmp_path: Path) -> None:
    runner = make_runner(tmp_path)

    reason, hard_denied = runner._check_permission(
        "run_bash",
        {"command": "git status"},
    )

    assert reason is None
    assert hard_denied is False


def test_shell_operator_requires_permission(tmp_path: Path) -> None:
    runner = make_runner(tmp_path)

    reason, hard_denied = runner._check_permission(
        "run_bash",
        {"command": "git status && touch marker"},
        session=Session(model="test-model"),
    )

    assert reason == "Shell command execution requires permission"
    assert hard_denied is False


def test_session_approval_allows_same_command_prefix(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = make_runner(tmp_path)
    session = Session(model="test-model")
    monkeypatch.setattr("builtins.input", lambda _: "a")

    assert runner._ask_user(
        "run_bash",
        {"command": "npm install package-a"},
        "Shell command execution requires permission",
        session=session,
    )
    reason, hard_denied = runner._check_permission(
        "run_bash",
        {"command": "npm install package-b"},
        session=session,
    )

    assert reason is None
    assert hard_denied is False


def test_bash_rejects_sibling_directory_prefix(tmp_path: Path) -> None:
    from src.tools.shell.bash import BashTool

    tool = BashTool(project_root=tmp_path)
    sibling = f"../{tmp_path.name}-secrets"

    result = tool.run(
        type("Args", (), {
            "command": "pwd",
            "cwd": sibling,
            "timeout_seconds": 1,
        })(),
        context=None,
    )

    assert result["ok"] is False
    assert result["stderr"] == "cwd escapes project root"


def test_ask_user_logs_only_the_allow_decision(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    runner = make_runner(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _: "y")

    assert runner._ask_user(
        "run_bash",
        {"command": "rm build-output"},
        "Potentially destructive command",
    )

    output = capsys.readouterr().out
    assert "✓ Allowed run_bash: rm build-output" in output
    assert "Potentially destructive command" not in output


def test_user_denial_stops_tools_and_uses_tool_free_summary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    tool_call = SimpleNamespace(
        id="call_denied",
        function=SimpleNamespace(
            name="run_bash",
            arguments='{"command":"touch marker"}',
        ),
    )
    responses = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[tool_call],
                    )
                )
            ]
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="操作已拒绝，未继续执行其他工具。",
                        tool_calls=None,
                    )
                )
            ]
        ),
    ]
    requests: list[dict] = []

    def create(**kwargs):
        requests.append(kwargs)
        return responses.pop(0)

    provider = SimpleNamespace(
        client=SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=create),
            )
        )
    )
    runner = AgentRunner(provider=provider, workdir=tmp_path)
    session = Session(model="test-model")
    dispatch_count = 0

    def dispatch(*args, **kwargs):
        nonlocal dispatch_count
        dispatch_count += 1
        return {"ok": True}

    toolset = SimpleNamespace(
        schemas=lambda: [],
        dispatch=dispatch,
    )
    monkeypatch.setattr("builtins.input", lambda _: "n")

    result = runner.run(session, toolset, "system")

    assert result.ok is True
    assert result.stopped_by_permission is True
    assert result.content == "操作已拒绝，未继续执行其他工具。"
    assert dispatch_count == 0
    assert len(requests) == 2
    assert requests[0]["tools"] == []
    assert requests[0]["tool_choice"] == "auto"
    assert requests[1]["tools"] == []
    assert requests[1]["tool_choice"] == "none"
    assert session.history[-1] == {
        "role": "assistant",
        "content": "操作已拒绝，未继续执行其他工具。",
    }
