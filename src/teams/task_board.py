"""A small process-safe shared task board for teammate workers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from src.teams.message_bus import FileLock
from src.tools.task.store import atomic_write_json


class TeamTask(BaseModel):
    task_id: str = Field(default_factory=lambda: f"teamtask_{uuid4().hex}")
    subject: str = Field(min_length=1)
    description: str = ""
    depends_on: list[str] = Field(default_factory=list)
    status: str = Field(default="pending", pattern="^(pending|in_progress|completed|failed|cancelled)$")
    owner: str | None = None
    claim_token: str | None = None
    claimed_at: float | None = None
    lease_expires_at: float | None = None
    workspace_mode: str = Field(default="repo", pattern="^(repo|worktree)$")
    workspace_root: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


class TaskBoard:
    def __init__(self, team_root: Path) -> None:
        self.root = team_root.resolve()
        self.path = self.root / "tasks.json"
        self.lock = self.root / ".locks" / "tasks.lock"

    def _read_unlocked(self) -> list[TeamTask]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("Invalid task board data")
        return [TeamTask.model_validate(item) for item in raw]

    def _write_unlocked(self, tasks: list[TeamTask]) -> None:
        atomic_write_json(
            self.path,
            [task.model_dump(mode="json") for task in tasks],
        )

    def list_tasks(self) -> list[TeamTask]:
        with FileLock(self.lock):
            return self._read_unlocked()

    def create_tasks(self, tasks: list[TeamTask]) -> list[TeamTask]:
        if not tasks:
            return []
        with FileLock(self.lock):
            current = self._read_unlocked()
            existing = {task.task_id for task in current}
            if any(task.task_id in existing for task in tasks):
                raise ValueError("Duplicate task ID")
            current.extend(tasks)
            self._write_unlocked(current)
            return list(tasks)

    @staticmethod
    def _ready(task: TeamTask, by_id: dict[str, TeamTask]) -> bool:
        return (
            task.status == "pending"
            and all(
                by_id.get(dependency) is not None
                and by_id[dependency].status == "completed"
                for dependency in task.depends_on
            )
        )

    def ready_tasks(self) -> list[TeamTask]:
        tasks = self.list_tasks()
        by_id = {task.task_id: task for task in tasks}
        return [task for task in tasks if self._ready(task, by_id)]

    def claim_ready(
        self,
        owner: str,
        task_id: str | None = None,
        *,
        lease_seconds: float = 3600,
    ) -> TeamTask | None:
        if not owner.strip():
            raise ValueError("Task owner cannot be empty")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with FileLock(self.lock):
            tasks = self._read_unlocked()
            by_id = {task.task_id: task for task in tasks}
            candidates = [
                task for task in tasks
                if self._ready(task, by_id)
                and (task_id is None or task.task_id == task_id)
            ]
            if not candidates:
                return None
            task = sorted(candidates, key=lambda item: item.task_id)[0]
            task.status = "in_progress"
            task.owner = owner
            task.claim_token = f"claim_{uuid4().hex}"
            task.claimed_at = datetime.now(timezone.utc).timestamp()
            task.lease_expires_at = task.claimed_at + lease_seconds
            self._write_unlocked(tasks)
            return task

    def renew(
        self,
        task_id: str,
        claim_token: str,
        *,
        lease_seconds: float = 3600,
    ) -> TeamTask:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with FileLock(self.lock):
            tasks = self._read_unlocked()
            task = next((item for item in tasks if item.task_id == task_id), None)
            if task is None:
                raise ValueError(f"Task not found: {task_id}")
            if task.status != "in_progress" or task.claim_token != claim_token:
                raise ValueError("Task claim is no longer valid")
            task.lease_expires_at = datetime.now(timezone.utc).timestamp() + lease_seconds
            self._write_unlocked(tasks)
            return task

    def reclaim_expired(self, *, now: float | None = None) -> list[TeamTask]:
        """Return abandoned claims to pending so a new worker can retry them."""
        timestamp = now if now is not None else datetime.now(timezone.utc).timestamp()
        reclaimed: list[TeamTask] = []
        with FileLock(self.lock):
            tasks = self._read_unlocked()
            for task in tasks:
                if (
                    task.status == "in_progress"
                    and task.lease_expires_at is not None
                    and task.lease_expires_at <= timestamp
                ):
                    task.status = "pending"
                    task.owner = None
                    task.claim_token = None
                    task.claimed_at = None
                    task.lease_expires_at = None
                    reclaimed.append(task)
            if reclaimed:
                self._write_unlocked(tasks)
        return reclaimed

    def bind_workspace(
        self,
        task_id: str,
        claim_token: str,
        workspace_root: Path,
    ) -> TeamTask:
        with FileLock(self.lock):
            tasks = self._read_unlocked()
            task = next((item for item in tasks if item.task_id == task_id), None)
            if task is None:
                raise ValueError(f"Task not found: {task_id}")
            if task.status != "in_progress" or task.claim_token != claim_token:
                raise ValueError("Task claim is no longer valid")
            task.workspace_root = str(workspace_root.resolve())
            self._write_unlocked(tasks)
            return task

    def get(self, task_id: str) -> TeamTask | None:
        return next((task for task in self.list_tasks() if task.task_id == task_id), None)

    def complete(
        self,
        task_id: str,
        claim_token: str,
        result: dict[str, Any] | None = None,
    ) -> TeamTask:
        return self._finish(task_id, claim_token, "completed", result=result)

    def fail(self, task_id: str, claim_token: str, error: str) -> TeamTask:
        return self._finish(task_id, claim_token, "failed", error=error)

    def _finish(
        self,
        task_id: str,
        claim_token: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> TeamTask:
        with FileLock(self.lock):
            tasks = self._read_unlocked()
            task = next((item for item in tasks if item.task_id == task_id), None)
            if task is None:
                raise ValueError(f"Task not found: {task_id}")
            if task.status != "in_progress" or task.claim_token != claim_token:
                raise ValueError("Task claim is no longer valid")
            task.status = status
            task.result = result
            task.error = error
            task.lease_expires_at = None
            self._write_unlocked(tasks)
            return task
