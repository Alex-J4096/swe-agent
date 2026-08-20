import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.runtime.agent_runner import AgentRunner
from src.runtime.context_rewind import ContextCheckpointManager
from src.runtime.session import Session
from src.tools.base import ToolContext
from src.tools.memory.rewind_context import RewindContextArgs, RewindContextTool
from src.tools.toolset import ToolSet


def carry_forward(**overrides):
    value = {
        "goal": "Finish the user's implementation request.",
        "completed": ["Inspected the implementation."],
        "files_changed": ["src/example.py"],
        "external_effects": ["The workspace file remains modified."],
        "discoveries": ["The relevant entry point is src/example.py."],
        "failed_approaches": ["Do not repeat the discarded broad search."],
        "next_steps": ["Run the focused tests."],
        "long_term_candidates": ["The project uses Python 3.13."],
    }
    value.update(overrides)
    return value


def test_checkpoint_rewind_archives_branch_and_restores_snapshot(
    tmp_path: Path,
) -> None:
    manager = ContextCheckpointManager(tmp_path)
    session = Session(
        model="test-model",
        session_id="session-1",
        history=[{"role": "user", "content": "original goal"}],
    )
    target = manager.create_checkpoint(session, reason="before exploration")
    session.history.append({"role": "assistant", "content": "noisy search"})
    future = manager.create_checkpoint(session, reason="after noisy search")
    session.history.append({"role": "assistant", "content": "more noise"})
    original_branch = session.context_branch_id
    session.pending_context_rewind = {
        "checkpoint_id": target.checkpoint_id,
        "carry_forward": carry_forward(),
    }

    result = manager.apply_pending(session)

    assert result is not None
    assert result.abandoned_branch_id == original_branch
    assert result.new_branch_id == session.context_branch_id
    assert result.discarded_messages == 2
    assert result.long_term_candidates == ("The project uses Python 3.13.",)
    assert result.transcript_path.exists()
    transcript = result.transcript_path.read_text(encoding="utf-8")
    assert "noisy search" in transcript
    assert "more noise" in transcript
    assert [item["checkpoint_id"] for item in session.context_checkpoints] == [
        target.checkpoint_id
    ]
    assert future.checkpoint_id not in {
        item["checkpoint_id"] for item in session.context_checkpoints
    }
    assert session.history[0]["content"] == "original goal"
    assert "noisy search" not in json.dumps(session.history, ensure_ascii=False)
    assert "External side effects were not rolled back" in session.history[-1][
        "content"
    ]
    assert "long_term_candidates" in session.history[-1]["content"]
    assert session.context_generation == 1
    assert session.pending_context_rewind is None


def test_corrupt_checkpoint_fails_without_replacing_current_history(
    tmp_path: Path,
) -> None:
    manager = ContextCheckpointManager(tmp_path)
    session = Session(
        model="test-model",
        history=[{"role": "user", "content": "original"}],
    )
    checkpoint = manager.create_checkpoint(session)
    session.history.append({"role": "assistant", "content": "keep me"})
    checkpoint_path = manager._checkpoint_path(session, checkpoint.checkpoint_id)
    checkpoint_path.write_text('{"role":"user","content":"tampered"}\n')
    expected = list(session.history)
    session.pending_context_rewind = {
        "checkpoint_id": checkpoint.checkpoint_id,
        "carry_forward": carry_forward(),
    }

    with pytest.raises(ValueError, match="integrity"):
        manager.apply_pending(session)

    assert session.history == expected
    assert session.pending_context_rewind is None


def test_rewind_tool_only_schedules_a_valid_request(tmp_path: Path) -> None:
    manager = ContextCheckpointManager(tmp_path)
    session = Session(model="test-model", history=[])
    checkpoint = manager.create_checkpoint(session)
    tool = RewindContextTool()
    args = RewindContextArgs.model_validate(
        {
            "checkpoint_id": checkpoint.checkpoint_id,
            "carry_forward": carry_forward(),
        }
    )

    output = tool.run(args, ToolContext(session=session))

    assert output["ok"] is True
    assert output["status"] == "rewind_scheduled"
    assert session.history == []
    assert session.pending_context_rewind is not None


def test_missing_checkpoint_snapshot_is_not_exposed_after_restore(
    tmp_path: Path,
) -> None:
    manager = ContextCheckpointManager(tmp_path)
    session = Session(
        model="test-model",
        context_checkpoints=[
            {
                "checkpoint_id": "cp_0123456789abcdef0123456789abcdef",
                "branch_id": "branch_0123456789abcdef0123456789abcdef",
                "history_length": 1,
                "history_digest": "missing",
                "reason": "restored",
                "created_at": "2026-08-20T00:00:00+00:00",
            }
        ],
    )

    assert manager.available(session) == []
    assert manager.prompt(session) == ""


class FakeToolCall:
    def __init__(self, checkpoint_id: str) -> None:
        self.id = "call_rewind"
        self.function = SimpleNamespace(
            name="rewind_context",
            arguments=json.dumps(
                {
                    "checkpoint_id": checkpoint_id,
                    "carry_forward": carry_forward(),
                }
            ),
        )

    def model_dump(self):
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.function.name,
                "arguments": self.function.arguments,
            },
        }


class GenericToolCall:
    def __init__(self, call_id: str, name: str, arguments: dict) -> None:
        self.id = call_id
        self.function = SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments),
        )

    def model_dump(self):
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.function.name,
                "arguments": self.function.arguments,
            },
        }


def test_runner_applies_rewind_at_safe_point_and_continues(
    tmp_path: Path,
) -> None:
    manager = ContextCheckpointManager(tmp_path)
    session = Session(
        model="test-model",
        history=[{"role": "user", "content": "implement the feature"}],
    )
    target = manager.create_checkpoint(session, reason="before noisy work")
    session.history.append(
        {"role": "assistant", "content": "irrelevant exploratory reasoning"}
    )
    responses = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[FakeToolCall(target.checkpoint_id)],
                    )
                )
            ]
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="continued from the compact branch",
                        tool_calls=None,
                    )
                )
            ]
        ),
    ]
    requests = []

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
    callback_results = []
    runner = AgentRunner(
        provider=provider,
        workdir=tmp_path,
        context_checkpoint_manager=manager,
    )

    result = runner.run(
        session,
        ToolSet(),
        "system",
        on_context_rewound=lambda _session, rewind: callback_results.append(rewind),
    )

    assert result.ok is True
    assert result.content == "continued from the compact branch"
    assert len(requests) == 2
    assert "Context rewind is available" in requests[0]["messages"][0]["content"]
    rendered_history = json.dumps(session.history, ensure_ascii=False)
    assert "irrelevant exploratory reasoning" not in rendered_history
    assert "continued from the compact branch" in rendered_history
    assert session.context_generation == 1
    assert len(callback_results) == 1
    assert callback_results[0].transcript_path.exists()


def test_runner_rejects_rewind_mixed_with_other_tools(tmp_path: Path) -> None:
    manager = ContextCheckpointManager(tmp_path)
    session = Session(
        model="test-model",
        history=[{"role": "user", "content": "task"}],
    )
    target = manager.create_checkpoint(session)
    responses = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            FakeToolCall(target.checkpoint_id),
                            GenericToolCall(
                                "call_todo",
                                "todo_manager",
                                {"tasks": []},
                            ),
                        ],
                    )
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
    outputs = []

    result = AgentRunner(
        provider=provider,
        workdir=tmp_path,
        context_checkpoint_manager=manager,
    ).run(
        session,
        ToolSet(),
        "system",
        on_tool_result=lambda name, output: outputs.append((name, output)),
    )

    assert result.ok is True
    assert session.context_generation == 0
    assert session.pending_context_rewind is None
    rewind_output = next(output for name, output in outputs if name == "rewind_context")
    assert rewind_output["ok"] is False
    assert "sole tool call" in rewind_output["error"]
