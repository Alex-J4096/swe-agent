import json
from io import StringIO
from types import SimpleNamespace

import pytest

from src.runtime.agent_runner import AgentRunner
from src.runtime.approval import ApprovalResponse, ApprovalRuntime, ApprovalSource
from src.runtime.permissions import PermissionPolicy
from src.runtime.session import Session
from src.ui.console import ConsoleRenderer
from src.utils.slash_commands.base import CommandContext
from src.utils.slash_commands.commands.permissions import PermissionsCommand


def test_network_and_delete_are_reviewed_together(tmp_path):
    policy = PermissionPolicy(tmp_path)
    decision = policy.evaluate("run_bash", {"command": "curl https://example.com && rm marker"})
    assert decision.required_capabilities == {"sandbox.network", "workspace.destructive"}
    assert "network" in decision.reason and "destructively" in decision.reason
    assert decision.outcome == "require_approval"


@pytest.mark.parametrize("changed", [
    "curl https://other.example", "curl https://example.com && rm marker",
    "curl https://example.com; echo extra", "curl 'https://example.com'",
])
def test_session_grant_is_bound_to_full_source(tmp_path, changed):
    policy = PermissionPolicy(tmp_path)
    session = Session(model="test")
    first = policy.evaluate("run_bash", {"command": "curl https://example.com"})
    session.approved_permission_scopes.add(first.scope_key)
    assert policy.evaluate("run_bash", {"command": "curl https://example.com", "cwd": "./"}, session=session).outcome == "allow"
    assert policy.evaluate("run_bash", {"command": changed}, session=session).outcome == "require_approval"
    assert policy.evaluate("run_bash", {"command": "curl https://example.com", "cwd": "subdir"}, session=session).outcome == "require_approval"


@pytest.mark.parametrize("command", [
    "python -c 'print(1)'", "bash -c 'rm marker'", "echo $(rm marker)",
    "curl https://example.com > output", "echo one\nrm marker",
    "uv run python -c 'print(1)'", "env python3.13 -c 'print(1)'",
    "npm run cleanup", "./cleanup.sh", "find . -name marker -delete",
])
def test_opaque_source_never_reuses_grants(tmp_path, command):
    policy = PermissionPolicy(tmp_path)
    decision = policy.evaluate("run_bash", {"command": command})
    assert not decision.reusable
    session = Session(model="test", approved_permission_scopes={decision.scope_key})
    assert policy.evaluate("run_bash", {"command": command}, session=session).outcome == "require_approval"


def test_extension_grant_is_bound_to_arguments_and_workspace(tmp_path):
    policy = PermissionPolicy(tmp_path)
    first = policy.evaluate("extension", {"target": "a"})
    session = Session(model="test", approved_permission_scopes={first.scope_key})
    assert policy.evaluate("extension", {"target": "a"}, session=session).outcome == "allow"
    assert policy.evaluate("extension", {"target": "b"}, session=session).outcome == "require_approval"
    assert PermissionPolicy(tmp_path / "other").evaluate("extension", {"target": "a"}, session=session).outcome == "require_approval"


def make_runtime_request(runtime, *, reusable=True):
    return runtime.create_request(
        tool_call_id="call", tool_name="run_bash", arguments_json="{}",
        reason="network and deletion", preview="curl https://example.com && rm marker",
        scope_key="scope", capability=None, capabilities=("sandbox.network", "workspace.destructive"),
        cwd="/workspace", reusable=reusable,
        source=ApprovalSource(kind="team", session_id="session", agent_id="worker"),
    )


def test_runtime_enforces_once_only_and_stop_is_not_approval():
    runtime = ApprovalRuntime()
    request = make_runtime_request(runtime, reusable=False)
    assert not runtime.resolve(request.id, ApprovalResponse("approve_session"))
    assert runtime.get_request(request.id).status == "pending"
    assert runtime.resolve(request.id, ApprovalResponse("approve_once"))
    assert not ApprovalResponse("stop").approved
    assert not ApprovalResponse("unknown").approved


def test_card_precedes_answer_and_unknown_answer_does_not_approve(tmp_path, monkeypatch):
    runtime = ApprovalRuntime()
    request = make_runtime_request(runtime)
    stream = StringIO()
    runner = AgentRunner(provider=object(), workdir=tmp_path, renderer=ConsoleRenderer(output=stream))
    answers = iter(["always", "y"])
    def answer(_):
        assert "curl https://example.com && rm marker" in stream.getvalue()
        assert "sandbox.network, workspace.destructive" in stream.getvalue()
        assert "/workspace" in stream.getvalue() and "worker" in stream.getvalue()
        return next(answers)
    monkeypatch.setattr("builtins.input", answer)
    assert runner._prompt_for_approval(request).decision == "approve_once"
    assert "Choose" in stream.getvalue()


def test_grants_are_readable_and_can_be_revoked_by_number(tmp_path):
    policy = PermissionPolicy(tmp_path)
    decision = policy.evaluate("run_bash", {"command": "curl https://example.com"})
    session = Session(model="test", approved_permission_scopes={decision.scope_key})
    context = CommandContext(session=session, toolset=object(), provider=object(), approval_runtime=ApprovalRuntime(), workdir=tmp_path)
    command = PermissionsCommand()
    listed = command.execute(["grants"], context).message
    assert "curl https://example.com" in listed and str(tmp_path) in listed
    assert "not permanent rules" in listed
    command.execute(["revoke", "1"], context)
    assert not session.approved_permission_scopes


def call(identifier, command):
    return SimpleNamespace(id=identifier, function=SimpleNamespace(name="run_bash", arguments=json.dumps({"command": command})))


def runner_fixture(tmp_path, batches):
    messages = [SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=batch))]) for batch in batches]
    messages.append(SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="done", tool_calls=None))]))
    provider = SimpleNamespace(client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: messages.pop(0)))))
    dispatched = []
    toolset = SimpleNamespace(schemas=lambda: [], dispatch=lambda name, arguments, **kwargs: dispatched.append((arguments, kwargs["context"].granted_capabilities)) or {"ok": True})
    return AgentRunner(provider=provider, workdir=tmp_path), toolset, dispatched


def test_reject_skips_batch_then_executes_permitted_alternative(tmp_path):
    runner, toolset, dispatched = runner_fixture(tmp_path, [[call("reject", "rm marker"), call("skip", "touch stale")], [call("alternative", "git status")]])
    session = Session(model="test")
    result = runner.run(session, toolset, "system", confirm_permission=lambda *_: False)
    assert result.content == "done" and not result.stopped_by_permission
    assert len(dispatched) == 1 and "git status" in dispatched[0][0]
    results = [item for item in session.history if item["role"] == "tool"]
    assert [item["tool_call_id"] for item in results] == ["reject", "skip", "alternative"]


def test_rejected_operation_does_not_prompt_again(tmp_path):
    runner, toolset, dispatched = runner_fixture(tmp_path, [[call("one", "rm marker")], [call("two", "rm marker")]])
    prompts = []
    result = runner.run(Session(model="test"), toolset, "system", confirm_permission=lambda *_: prompts.append(1) or False)
    assert result.content == "done" and prompts == [1] and dispatched == []


def test_stop_resolves_remaining_tool_messages_without_dispatch(tmp_path, monkeypatch):
    runner, toolset, dispatched = runner_fixture(tmp_path, [[call("stop", "rm marker"), call("skip", "touch stale")]])
    monkeypatch.setattr("builtins.input", lambda _: "s")
    session = Session(model="test")
    result = runner.run(session, toolset, "system")
    assert result.stopped_by_permission and not dispatched
    assert len([item for item in session.history if item["role"] == "tool"]) == 2


def test_runner_dispatches_all_approved_capabilities(tmp_path):
    runner, toolset, dispatched = runner_fixture(tmp_path, [[call("combined", "curl https://example.com && rm marker")]])
    runner.run(Session(model="test"), toolset, "system", confirm_permission=lambda *_: True)
    assert dispatched[0][1] == {"sandbox.network", "workspace.destructive"}


@pytest.mark.parametrize("ok", [True, False])
def test_approval_transitions_to_tool_animation_then_back_to_model(tmp_path, ok):
    runner, toolset, _ = runner_fixture(tmp_path, [[call("network", "curl https://example.com")]])
    events = []
    runner.renderer = SimpleNamespace(
        start_thinking=lambda message: events.append(message),
        stop_thinking=lambda: events.append("stopped"),
    )

    def approve(*args):
        assert events[-1] == "stopped"  # No animation over the approval prompt.
        events.append("approved")
        return True

    def dispatch(*args, **kwargs):
        assert events[-1].startswith("Executing run_bash")
        events.append("dispatch")
        return {"ok": ok, "stderr": "test failure" if not ok else ""}

    def show_result(*args):
        assert events[-1] == "stopped"  # Clear animation before printing output.
        events.append("result")

    toolset.dispatch = dispatch
    result = runner.run(Session(model="test"), toolset, "system",
                        confirm_permission=approve, on_tool_result=show_result)
    assert result.content == "done"
    assert events == [
        "Thinking · Esc/Ctrl+C to stop", "stopped", "approved",
        "Executing run_bash · Esc/Ctrl+C to stop", "dispatch", "stopped", "result",
        "Thinking · Esc/Ctrl+C to stop", "stopped",
    ]


def test_tool_animation_is_cleared_on_interruption(tmp_path):
    from src.utils.interrupt import TurnInterrupted

    runner, toolset, _ = runner_fixture(tmp_path, [[call("network", "curl https://example.com")]])
    events = []
    runner.renderer = SimpleNamespace(
        start_thinking=lambda message: events.append(message),
        stop_thinking=lambda: events.append("stopped"),
    )

    def dispatch(*args, **kwargs):
        assert events[-1].startswith("Executing run_bash")
        raise TurnInterrupted()

    toolset.dispatch = dispatch
    with pytest.raises(TurnInterrupted):
        runner.run(Session(model="test"), toolset, "system", confirm_permission=lambda *_: True)
    assert events[-1] == "stopped"


def test_rejection_limit_stops_loop_without_reprompting(tmp_path, monkeypatch):
    runner, toolset, dispatched = runner_fixture(tmp_path, [[call(str(i), "rm marker")] for i in range(3)])
    monkeypatch.setattr(runner, "_request_permission_summary", lambda **_: "Stopped after repeated rejections")
    prompts = []
    result = runner.run(Session(model="test"), toolset, "system", confirm_permission=lambda *_: prompts.append(1) or False)
    assert result.stopped_by_permission
    assert prompts == [1] and dispatched == []


def test_loading_legacy_database_does_not_restore_grants(tmp_path):
    from src.infrastructure.database import AppDatabase
    from src.infrastructure.session_store import SessionStore

    database = AppDatabase(tmp_path / "state.db")
    try:
        store = SessionStore(database)
        session = Session(model="test")
        store.save(session, workdir=tmp_path)
        with database.transaction() as connection:
            connection.execute(
                "UPDATE session_state SET approved_prefixes_json = ?, approved_scopes_json = ? WHERE session_id = ?",
                ('[["npm", "install"]]', '["sandbox.network:legacy"]', session.session_id),
            )
        restored = store.load(session.session_id)
        assert restored.approved_permission_scopes == set()
        assert restored.approved_command_prefixes == set()
    finally:
        database.close()
