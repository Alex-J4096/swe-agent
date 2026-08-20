from pathlib import Path
from threading import Thread

from src.runtime.approval import (
    ApprovalResponse,
    ApprovalRuntime,
    ApprovalSource,
)
from src.runtime.session import Session
from src.tools.toolset import ToolSet
from src.utils.slash_commands.base import CommandContext
from src.utils.slash_commands.commands.permissions import PermissionsCommand


def make_request(runtime: ApprovalRuntime, *, agent_id: str | None = None):
    return runtime.create_request(
        tool_call_id="call-1",
        tool_name="run_bash",
        arguments_json='{"command":"curl https://example.com"}',
        reason="Command requires sandbox network access",
        preview="curl https://example.com",
        scope_key="sandbox.network:demo",
        capability="sandbox.network",
        source=ApprovalSource(
            kind="team" if agent_id else "foreground",
            session_id="session-1",
            agent_id=agent_id,
            interactive=agent_id is None,
        ),
    )


def test_approval_runtime_wakes_exact_waiter_and_first_response_wins() -> None:
    runtime = ApprovalRuntime()
    request = make_request(runtime)
    responses = []
    waiter = Thread(
        target=lambda: responses.append(runtime.wait_for_response(request.id))
    )
    waiter.start()

    assert runtime.resolve(request.id, ApprovalResponse("approve_once")) is True
    assert runtime.resolve(request.id, ApprovalResponse("reject")) is False
    waiter.join(timeout=1)

    assert responses == [ApprovalResponse("approve_once")]
    assert runtime.list_pending() == []


def test_cancel_source_only_cancels_matching_agent() -> None:
    runtime = ApprovalRuntime()
    first = make_request(runtime, agent_id="teammate-1")
    second = make_request(runtime, agent_id="teammate-2")

    assert runtime.cancel_source(agent_id="teammate-1") == 1

    assert runtime.get_request(first.id).status == "cancelled"
    assert runtime.get_request(second.id).status == "pending"


def test_permissions_command_resolves_background_request(tmp_path: Path) -> None:
    runtime = ApprovalRuntime()
    request = make_request(runtime, agent_id="teammate-1")
    session = Session(model="test")
    context = CommandContext(
        session=session,
        toolset=ToolSet(),
        provider=object(),
        approval_runtime=runtime,
        workdir=tmp_path,
    )
    command = PermissionsCommand()

    listed = command.execute([], context)
    approved = command.execute(["approve", request.id, "session"], context)

    assert request.id in listed.message
    assert "Approved" in approved.message
    assert runtime.wait_for_response(request.id).decision == "approve_session"


def test_invalid_history_limit_is_rejected() -> None:
    try:
        ApprovalRuntime(history_limit=0)
    except ValueError as exc:
        assert "positive" in str(exc)
    else:
        raise AssertionError("Expected a positive history limit requirement")
