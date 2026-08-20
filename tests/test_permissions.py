import json
from pathlib import Path
from types import SimpleNamespace

from src.runtime.agent_runner import AgentRunner
from src.runtime.approval import ApprovalSource
from src.runtime.session import Session


def make_runner(tmp_path: Path) -> AgentRunner:
    return AgentRunner(
        provider=SimpleNamespace(client=SimpleNamespace()),
        workdir=tmp_path,
    )


def make_approval_request(runner: AgentRunner, command: str = "rm marker"):
    return runner.approval_runtime.create_request(
        tool_call_id="call-test",
        tool_name="run_bash",
        arguments_json=json.dumps({"command": command}),
        reason="Command may destructively modify the workspace",
        preview=command,
        scope_key="workspace.destructive:test",
        capability="workspace.destructive",
        source=ApprovalSource(
            kind="foreground",
            session_id="session-test",
        ),
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


def test_workspace_local_shell_composition_runs_inside_sandbox_without_approval(
    tmp_path: Path,
) -> None:
    runner = make_runner(tmp_path)

    reason, hard_denied = runner._check_permission(
        "run_bash",
        {"command": "git status && touch marker"},
        session=Session(model="test-model"),
    )

    assert reason is None
    assert hard_denied is False


def test_network_command_requires_capability_approval(tmp_path: Path) -> None:
    runner = make_runner(tmp_path)

    reason, hard_denied = runner._check_permission(
        "run_bash",
        {"command": "curl https://example.com"},
        session=Session(model="test-model"),
    )

    assert reason == "Command requires sandbox network access"
    assert hard_denied is False


def test_explicit_network_request_requires_capability_approval(tmp_path: Path) -> None:
    runner = make_runner(tmp_path)

    reason, hard_denied = runner._check_permission(
        "run_bash",
        {"command": "python -m integration_test", "network_access": True},
        session=Session(model="test-model"),
    )

    assert reason == "Command requires sandbox network access"
    assert hard_denied is False


def test_destructive_workspace_command_requires_approval(tmp_path: Path) -> None:
    runner = make_runner(tmp_path)

    reason, hard_denied = runner._check_permission(
        "run_bash",
        {"command": "rm marker"},
        session=Session(model="test-model"),
    )

    assert reason == "Command may destructively modify the workspace"
    assert hard_denied is False


def test_rewind_context_is_a_builtin_internal_capability(tmp_path: Path) -> None:
    runner = make_runner(tmp_path)

    reason, hard_denied = runner._check_permission(
        "rewind_context",
        {"checkpoint_id": "cp_test"},
        session=Session(model="test-model"),
    )

    assert reason is None
    assert hard_denied is False


def test_readonly_shell_compositions_are_auto_allowed(tmp_path: Path) -> None:
    runner = make_runner(tmp_path)

    for command in (
        'find . -type f -name "*.md" | head -30',
        'ls -la && find . -iname "*agent*team*" 2>/dev/null | head -50',
        'find . -type f -name "*.py" | grep -v __pycache__ | sort',
        "cat -n docs/TEAMS.md",
        "pwd && ls -la",
    ):
        reason, hard_denied = runner._check_permission(
            "run_bash",
            {"command": command},
            session=Session(model="test-model"),
        )
        assert reason is None
        assert hard_denied is False


def test_legacy_session_prefix_is_honored_for_restored_sessions(
    tmp_path: Path,
) -> None:
    runner = make_runner(tmp_path)
    session = Session(
        model="test-model",
        approved_command_prefixes={("npm", "install")},
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


def test_approval_prompt_logs_only_the_allow_decision(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    runner = make_runner(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _: "y")

    response = runner._prompt_for_approval(
        make_approval_request(runner, "rm build-output")
    )

    assert response.approved is True
    output = capsys.readouterr().out
    assert "✓ Allowed run_bash: rm build-output" in output
    assert "Potentially destructive command" not in output


def test_escape_interrupts_permission_prompt(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    runner = make_runner(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _: "\x1b")

    response = runner._prompt_for_approval(
        make_approval_request(runner, "rm docs/TEAMS.md")
    )

    assert response.approved is False
    assert runner._permission_interrupted is True
    assert "Returning to the user prompt" in capsys.readouterr().out


def test_user_denial_stops_tools_and_uses_tool_free_summary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    tool_call = SimpleNamespace(
        id="call_denied",
        function=SimpleNamespace(
            name="run_bash",
            arguments='{"command":"rm marker"}',
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


def test_runner_returns_to_user_after_team_plan_proposal(tmp_path: Path) -> None:
    tool_call = SimpleNamespace(
        id="call_plan",
        function=SimpleNamespace(
            name="propose_team_plan",
            arguments='{"goal":"test","tasks":[]}',
        ),
    )
    requests: list[dict] = []

    def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[tool_call],
                    )
                )
            ]
        )

    provider = SimpleNamespace(
        client=SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=create),
            )
        )
    )
    runner = AgentRunner(provider=provider, workdir=tmp_path)
    session = Session(model="test-model")
    toolset = SimpleNamespace(
        schemas=lambda: [],
        dispatch=lambda *args, **kwargs: {
            "ok": True,
            "status": "awaiting_approval",
        },
    )

    result = runner.run(
        session,
        toolset,
        "system",
        stop_after_tool=lambda name, output: (
            name == "propose_team_plan"
            and output.get("status") == "awaiting_approval"
        ),
    )

    assert result.ok is True
    assert result.content is None
    assert len(requests) == 1


def test_runner_can_continue_after_user_confirms_team_plan(
    tmp_path: Path,
) -> None:
    tool_call = SimpleNamespace(
        id="call_plan",
        function=SimpleNamespace(
            name="propose_team_plan",
            arguments='{"goal":"test","tasks":[]}',
        ),
    )
    responses = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=None, tool_calls=[tool_call])
                )
            ]
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="continued", tool_calls=None)
                )
            ]
        ),
    ]

    def create(**kwargs):
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
    toolset = SimpleNamespace(
        schemas=lambda: [],
        dispatch=lambda *args, **kwargs: {
            "ok": True,
            "status": "awaiting_approval",
        },
    )

    result = runner.run(
        session,
        toolset,
        "system",
        stop_after_tool=lambda name, output: name == "propose_team_plan",
        continue_after_tool=lambda name, output: True,
    )

    assert result.content == "continued"


def test_session_approval_grants_only_required_sandbox_capability(
    tmp_path: Path,
    monkeypatch,
) -> None:
    tool_call = SimpleNamespace(
        id="call_network",
        function=SimpleNamespace(
            name="run_bash",
            arguments=(
                '{"command":"curl https://example.com",'
                '"network_access":true}'
            ),
        ),
    )
    responses = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=None, tool_calls=[tool_call])
                )
            ]
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="done", tool_calls=None)
                )
            ]
        ),
    ]
    provider = SimpleNamespace(
        client=SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **_: responses.pop(0)),
            )
        )
    )
    captured_contexts = []
    toolset = SimpleNamespace(
        schemas=lambda: [],
        dispatch=lambda *_, context: (
            captured_contexts.append(context) or {"ok": True}
        ),
    )
    runner = AgentRunner(provider=provider, workdir=tmp_path)
    session = Session(model="test")
    monkeypatch.setattr("builtins.input", lambda _: "a")

    result = runner.run(session, toolset, "system")

    assert result.content == "done"
    assert len(session.approved_permission_scopes) == 1
    assert captured_contexts[0].granted_capabilities == frozenset(
        {"sandbox.network"}
    )


def test_turn_approval_reuses_same_scoped_capability(
    tmp_path: Path,
    monkeypatch,
) -> None:
    tool_calls = [
        SimpleNamespace(
            id=f"call_{branch}",
            function=SimpleNamespace(
                name="run_bash",
                arguments=json.dumps({"command": f"git push origin {branch}"}),
            ),
        )
        for branch in ("main", "release")
    ]
    responses = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=None, tool_calls=tool_calls)
                )
            ]
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="done", tool_calls=None)
                )
            ]
        ),
    ]
    provider = SimpleNamespace(
        client=SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **_: responses.pop(0)),
            )
        )
    )
    dispatched = []
    toolset = SimpleNamespace(
        schemas=lambda: [],
        dispatch=lambda *_, context: (
            dispatched.append(context) or {"ok": True}
        ),
    )
    answers = iter(["t"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    result = AgentRunner(provider=provider, workdir=tmp_path).run(
        Session(model="test"),
        toolset,
        "system",
    )

    assert result.content == "done"
    assert len(dispatched) == 2
    assert all(
        context.granted_capabilities == frozenset({"sandbox.network"})
        for context in dispatched
    )
