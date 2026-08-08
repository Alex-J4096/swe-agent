import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from src.tools.base import BaseTool, ToolContext


DEFAULT_TASK_DIR = Path(".swe-agent/tasks")
TASK_ID_PATTERN = re.compile(r"task_[0-9a-f]{32}")
TASK_STATUSES = frozenset({"pending", "in_progress", "completed"})
TaskAction = Literal["create", "list", "claim", "complete"]


@dataclass
class Task:
    id: str = ""
    subject: str = ""
    description: str = ""
    status: Literal["pending", "in_progress", "completed"] = "pending"
    owner: str | None = None
    blockedBy: list[str] = field(default_factory=list)


class TaskArgs(BaseModel):
    action: TaskAction = Field(
        description="Operation to perform: create, list, claim, or complete."
    )
    task: Task | None = Field(
        default=None,
        description=(
            "Task payload. For create, provide subject, optional description, "
            "and blockedBy; id, status, and owner are assigned by the system. "
            "For claim, provide id and optionally owner. For complete, provide id. "
            "Not required for list."
        ),
    )


class TaskManager(BaseTool[TaskArgs]):
    name = "task_manager"
    description = (
        "Manage persistent workspace tasks. Create tasks with dependency IDs, "
        "list all tasks, claim a ready task, or complete a claimed task."
    )
    args_model = TaskArgs

    def __init__(self, task_dir: Path | None = None) -> None:
        self.task_dir = (task_dir or DEFAULT_TASK_DIR).resolve()

    @staticmethod
    def _new_task_id() -> str:
        """Return a collision-resistant, filesystem-safe task identifier."""
        return f"task_{uuid4().hex}"

    @staticmethod
    def _validate_task_id(task_id: str) -> None:
        if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
            raise ValueError("Invalid task ID")

    def _get_task_path(self, task_id: str) -> Path:
        self._validate_task_id(task_id)
        return self.task_dir / f"{task_id}.json"

    def _validate_task(self, task: Task) -> None:
        self._validate_task_id(task.id)
        if not isinstance(task.subject, str) or not task.subject.strip():
            raise ValueError("Task subject cannot be empty")
        if not isinstance(task.description, str):
            raise ValueError("Task description must be a string")
        if task.status not in TASK_STATUSES:
            raise ValueError(f"Invalid task status: {task.status}")
        if task.owner is not None and (
            not isinstance(task.owner, str) or not task.owner.strip()
        ):
            raise ValueError("Task owner must be a non-empty string or null")
        if not isinstance(task.blockedBy, list):
            raise ValueError("blockedBy must be a list of task IDs")
        for dependency in task.blockedBy:
            self._validate_task_id(dependency)

    def create_task(
        self,
        subject: str,
        description: str = "",
        blocked_by: list[str] | None = None,
    ) -> Task:
        task = Task(
            id=self._new_task_id(),
            subject=subject.strip(),
            description=description,
            status="pending",
            owner=None,
            blockedBy=list(dict.fromkeys(blocked_by or [])),
        )
        self._validate_task(task)
        self.save_task(task)
        return task

    def save_task(self, task: Task) -> None:
        self._validate_task(task)
        self.task_dir.mkdir(parents=True, exist_ok=True)
        task_path = self._get_task_path(task.id)
        temporary_path = self.task_dir / f".{task.id}.{uuid4().hex}.tmp"
        try:
            temporary_path.write_text(
                json.dumps(asdict(task), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary_path, task_path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def load_task(self, task_id: str) -> Task:
        task_path = self._get_task_path(task_id)
        try:
            data = json.loads(task_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(f"Task not found: {task_id}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"Invalid task data: {task_id}")
        try:
            task = Task(**data)
        except TypeError as exc:
            raise ValueError(f"Invalid task data: {task_id}") from exc
        self._validate_task(task)
        return task

    def list_tasks(self) -> list[Task]:
        if not self.task_dir.exists():
            return []
        return [
            self.load_task(path.stem)
            for path in sorted(self.task_dir.glob("task_*.json"))
        ]

    def can_start(self, task_id: str) -> bool:
        """Return whether every declared dependency has completed."""
        task = self.load_task(task_id)
        for dependency in task.blockedBy:
            try:
                if self.load_task(dependency).status != "completed":
                    return False
            except ValueError:
                return False
        return True

    def claim_task(self, task_id: str, owner: str = "agent") -> str:
        task = self.load_task(task_id)
        if task.status != "pending":
            return f"Task {task_id} is {task.status}, cannot claim."
        if not self.can_start(task_id):
            blocked_by = []
            for dependency in task.blockedBy:
                try:
                    if self.load_task(dependency).status != "completed":
                        blocked_by.append(dependency)
                except ValueError:
                    blocked_by.append(dependency)
            return f"Blocked by: {blocked_by}"

        task.owner = owner
        task.status = "in_progress"
        self.save_task(task)
        print(f"  \033[36m[claim] {task.subject} → in_progress (owner: {owner})\033[0m")
        return f"Claimed {task.id} ({task.subject})"

    def complete_task(self, task_id: str) -> str:
        """Mark a claimed task as completed and report newly ready tasks."""
        task = self.load_task(task_id)
        if task.status != "in_progress":
            return f"Task {task_id} is {task.status}, cannot complete"
        task.status = "completed"
        self.save_task(task)

        unblocked = [
            candidate.subject
            for candidate in self.list_tasks()
            if candidate.status == "pending"
            and candidate.blockedBy
            and self.can_start(candidate.id)
        ]
        print(f"  \033[32m[complete] {task.subject} ✓\033[0m")
        message = f"Completed {task.id} ({task.subject})"

        if unblocked:
            message += f"\nUnblocked: {', '.join(unblocked)}"
            print(f"  \033[33m[unblocked] {', '.join(unblocked)}\033[0m")

        return message

    def run(self, args: TaskArgs, context: ToolContext) -> dict:
        try:
            if args.action == "list":
                return {
                    "ok": True,
                    "tasks": [asdict(task) for task in self.list_tasks()],
                }

            if args.task is None:
                return {
                    "ok": False,
                    "error": f"task is required for {args.action}",
                }

            if args.action == "create":
                task = self.create_task(
                    subject=args.task.subject,
                    description=args.task.description,
                    blocked_by=args.task.blockedBy,
                )
                return {"ok": True, "task": asdict(task)}

            if args.action == "claim":
                message = self.claim_task(
                    task_id=args.task.id,
                    owner=args.task.owner or "agent",
                )
                return {
                    "ok": message.startswith("Claimed "),
                    "content": message,
                }

            message = self.complete_task(args.task.id)
            return {
                "ok": message.startswith("Completed "),
                "content": message,
            }
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return {"ok": False, "error": str(exc)}
