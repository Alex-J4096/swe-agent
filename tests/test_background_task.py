from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from src.runtime.agent_runner import AgentRunner
from src.runtime.session import Session
from src.tools.base import ToolContext
from src.tools.task.background_task import (
    BackgroundTask,
    BackgroundTaskArgs,
    BackgroundTaskManager,
)


def wait_for_status(
    manager: BackgroundTaskManager,
    task_id: str,
    status: str,
) -> BackgroundTask:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        task = manager.get_task(task_id)
        if task.status == status:
            return task
        time.sleep(0.01)
    raise AssertionError(f"Task did not reach {status}: {task_id}")


def test_background_task_runs_registered_handler_and_persists_result(
    tmp_path: Path,
) -> None:
    manager = BackgroundTaskManager(
        task_dir=tmp_path / "background_tasks",
        handlers={"double": lambda payload: payload["value"] * 2},
    )
    session = Session(model="test-model")

    result = manager.run(
        BackgroundTaskArgs(
            action="submit",
            task=BackgroundTask(
                subject="Double value",
                handler="double",
                payload={"value": 21},
            ),
        ),
        ToolContext(session=session),
    )

    assert result["ok"] is True
    task = wait_for_status(manager, result["task"]["id"], "completed")
    assert task.result == 42
    assert task.session_id == session.session_id

    manager.shutdown()


def test_runner_injects_each_completion_event_once(tmp_path: Path) -> None:
    manager = BackgroundTaskManager(
        task_dir=tmp_path / "background_tasks",
        handlers={"echo": lambda payload: payload["message"]},
    )
    session = Session(model="test-model")
    result = manager.run(
        BackgroundTaskArgs(
            action="submit",
            task=BackgroundTask(
                subject="Notify parent",
                handler="echo",
                payload={"message": "done"},
            ),
        ),
        ToolContext(session=session),
    )
    task_id = result["task"]["id"]
    wait_for_status(manager, task_id, "completed")

    runner = AgentRunner(provider=SimpleNamespace(client=object()))
    toolset = SimpleNamespace(background_task_manager=manager)
    runner._inject_background_events(session, toolset)
    runner._inject_background_events(session, toolset)

    events = [
        message
        for message in session.history
        if message.get("role") == "user"
        and "[Background task event]" in message.get("content", "")
    ]
    assert len(events) == 1
    assert task_id in events[0]["content"]
    assert '"result": "done"' in events[0]["content"]

    manager.shutdown()


def test_unknown_handler_is_rejected_without_creating_task(tmp_path: Path) -> None:
    manager = BackgroundTaskManager(task_dir=tmp_path / "background_tasks")

    result = manager.run(
        BackgroundTaskArgs(
            action="submit",
            task=BackgroundTask(subject="No handler", handler="missing"),
        ),
        ToolContext(session=Session(model="test-model")),
    )

    assert result == {"ok": False, "error": "Unknown background handler: missing"}
    assert manager.list_tasks() == []
    manager.shutdown()


def test_retry_creates_a_new_completion_event(tmp_path: Path) -> None:
    attempts = 0

    def flaky_handler(_payload: dict) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary failure")
        return "recovered"

    manager = BackgroundTaskManager(
        task_dir=tmp_path / "background_tasks",
        handlers={"flaky": flaky_handler},
    )
    session = Session(model="test-model")
    submitted = manager.run(
        BackgroundTaskArgs(
            action="submit",
            task=BackgroundTask(subject="Retry me", handler="flaky"),
        ),
        ToolContext(session=session),
    )
    task_id = submitted["task"]["id"]
    failed = wait_for_status(manager, task_id, "failed")

    runner = AgentRunner(provider=SimpleNamespace(client=object()))
    toolset = SimpleNamespace(background_task_manager=manager)
    runner._inject_background_events(session, toolset)
    assert failed.event_id in session.tool_state["background_task_event_ids"]

    retried = manager.run(
        BackgroundTaskArgs(
            action="retry",
            task=BackgroundTask(id=task_id),
        ),
        ToolContext(session=session),
    )
    assert retried["ok"] is True
    completed = wait_for_status(manager, task_id, "completed")
    runner._inject_background_events(session, toolset)

    assert completed.event_id != failed.event_id
    assert session.history[-1]["role"] == "user"
    assert '"result": "recovered"' in session.history[-1]["content"]
    manager.shutdown()
