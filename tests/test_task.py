import re
from pathlib import Path

from src.tools.task.task import Task, TaskArgs, TaskManager
from src.tools.toolset import ToolSet


def make_manager(tmp_path: Path) -> TaskManager:
    return TaskManager(task_dir=tmp_path / ".swe-agent" / "tasks")


def test_create_initializes_store_generates_safe_id_and_round_trips(
    tmp_path: Path,
) -> None:
    manager = make_manager(tmp_path)

    task = manager.create_task("Write tests", "Add task lifecycle coverage")

    assert re.fullmatch(r"task_[0-9a-f]{32}", task.id)
    assert (tmp_path / ".swe-agent" / "tasks" / f"{task.id}.json").is_file()
    assert manager.load_task(task.id) == task
    assert manager.list_tasks() == [task]


def test_run_handles_dependency_lifecycle(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)

    upstream = manager.run(
        TaskArgs(action="create", task=Task(subject="Implement storage")),
        context=None,
    )
    assert upstream["ok"] is True
    upstream_id = upstream["task"]["id"]

    downstream = manager.run(
        TaskArgs(
            action="create",
            task=Task(subject="Integrate tool", blockedBy=[upstream_id]),
        ),
        context=None,
    )
    assert downstream["ok"] is True
    downstream_id = downstream["task"]["id"]

    blocked = manager.run(
        TaskArgs(action="claim", task=Task(id=downstream_id)),
        context=None,
    )
    assert blocked == {"ok": False, "content": f"Blocked by: ['{upstream_id}']"}

    assert manager.run(
        TaskArgs(action="claim", task=Task(id=upstream_id, owner="worker-a")),
        context=None,
    )["ok"] is True
    assert manager.run(
        TaskArgs(action="complete", task=Task(id=upstream_id)),
        context=None,
    )["ok"] is True
    assert manager.run(
        TaskArgs(action="claim", task=Task(id=downstream_id)),
        context=None,
    )["ok"] is True

    listed = manager.run(TaskArgs(action="list"), context=None)
    assert listed["ok"] is True
    assert [task["id"] for task in listed["tasks"]] == sorted(
        [upstream_id, downstream_id]
    )


def test_run_rejects_task_id_path_traversal(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)

    result = manager.run(
        TaskArgs(action="claim", task=Task(id="../outside")),
        context=None,
    )

    assert result == {"ok": False, "error": "Invalid task ID"}
    assert not (tmp_path / "outside.json").exists()


def test_default_toolset_registers_task_manager() -> None:
    assert "task_manager" in ToolSet().tools
