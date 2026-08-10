"""Asynchronous task submission, execution, and completion notifications."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from time import time
from threading import RLock
from typing import Any, Callable, Literal, Mapping
from uuid import uuid4

from pydantic import BaseModel, Field

from src.tools.base import BaseTool, ToolContext
from src.tools.task.store import JsonFileStore
from src.tools.task.task import DEFAULT_TASK_DIR, TASK_ID_PATTERN


DEFAULT_BACKGROUND_TASK_DIR = DEFAULT_TASK_DIR.parent / "background_tasks"
BACKGROUND_TASK_STATUSES = frozenset(
    {"pending", "in_progress", "completed", "failed", "cancelled"}
)
BackgroundTaskStatus = Literal[
    "pending",
    "in_progress",
    "completed",
    "failed",
    "cancelled",
]
BackgroundTaskAction = Literal["submit", "get", "list", "cancel", "retry"]
BackgroundHandler = Callable[[dict[str, Any]], Any]
TaskFinishedCallback = Callable[["BackgroundTask"], None]


@dataclass
class BackgroundTask:
    id: str = ""
    subject: str = ""
    description: str = ""
    handler: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    status: BackgroundTaskStatus = "pending"
    result: Any = None
    error: str | None = None
    event_id: str | None = None
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    acknowledged_at: float | None = None


class BackgroundTaskArgs(BaseModel):
    action: BackgroundTaskAction = Field(
        description=(
            "Operation to perform: submit, get, list, cancel, or retry."
        )
    )
    task: BackgroundTask | None = Field(
        default=None,
        description=(
            "Task payload. For submit, provide subject, description, handler, "
            "and JSON payload. The system assigns id and lifecycle fields. "
            "For get, cancel, or retry, provide id. Not required for list."
        ),
    )


class BackgroundTaskStore:
    def __init__(self, task_dir: Path | None = None) -> None:
        self.task_dir = (task_dir or DEFAULT_BACKGROUND_TASK_DIR).resolve()
        self._store = JsonFileStore(self.task_dir)

    @staticmethod
    def _validate_task_id(task_id: str) -> None:
        if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
            raise ValueError("Invalid background task ID")

    def _validate_task(self, task: BackgroundTask) -> None:
        self._validate_task_id(task.id)
        if not isinstance(task.subject, str) or not task.subject.strip():
            raise ValueError("Background task subject cannot be empty")
        if not isinstance(task.description, str):
            raise ValueError("Background task description must be a string")
        if not isinstance(task.handler, str) or not task.handler.strip():
            raise ValueError("Background task handler cannot be empty")
        if not isinstance(task.payload, dict):
            raise ValueError("Background task payload must be an object")
        if task.status not in BACKGROUND_TASK_STATUSES:
            raise ValueError(f"Invalid background task status: {task.status}")
        if task.session_id is not None and not isinstance(task.session_id, str):
            raise ValueError("Background task session_id must be a string or null")
        try:
            json.dumps(task.result, ensure_ascii=False)
        except TypeError as exc:
            raise ValueError("Background task result must be JSON serializable") from exc

    def save_task(self, task: BackgroundTask) -> None:
        self._validate_task(task)
        self._store.save(f"{task.id}.json", asdict(task))

    def load_task(self, task_id: str) -> BackgroundTask:
        self._validate_task_id(task_id)
        try:
            data = self._store.load(f"{task_id}.json")
        except FileNotFoundError as exc:
            raise ValueError(f"Background task not found: {task_id}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"Invalid background task data: {task_id}")
        try:
            task = BackgroundTask(**data)
        except TypeError as exc:
            raise ValueError(f"Invalid background task data: {task_id}") from exc
        self._validate_task(task)
        return task

    def list_tasks(self) -> list[BackgroundTask]:
        return [
            self.load_task(path.stem)
            for path in self._store.list("task_*.json")
        ]

    def pending_events(self, session_id: str) -> list[BackgroundTask]:
        terminal = {"completed", "failed", "cancelled"}
        return sorted(
            (
                task
                for task in self.list_tasks()
                if task.session_id == session_id
                and task.status in terminal
                and task.acknowledged_at is None
            ),
            key=lambda task: (task.finished_at or 0, task.id),
        )


class BackgroundTaskManager(BaseTool[BackgroundTaskArgs]):
    name = "background_task"
    description = (
        "Submit and monitor slow operations without blocking the agent. "
        "Use submit for registered handlers, then use get or wait for the "
        "completion event."
    )
    args_model = BackgroundTaskArgs

    def __init__(
        self,
        task_dir: Path | None = None,
        handlers: Mapping[str, BackgroundHandler] | None = None,
        max_workers: int = 4,
        on_task_finished: TaskFinishedCallback | None = None,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self.store = BackgroundTaskStore(task_dir)
        self.handlers = dict(handlers or {})
        self.executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="background-task",
        )
        self.on_task_finished = on_task_finished
        self._lock = RLock()
        self._refresh_description()

    def _refresh_description(self) -> None:
        handlers = ", ".join(sorted(self.handlers)) or "none registered"
        self.description = (
            "Submit and monitor slow operations without blocking the agent. "
            f"Registered handlers: {handlers}."
        )

    def register_handler(self, name: str, handler: BackgroundHandler) -> None:
        if not name.strip():
            raise ValueError("Background handler name cannot be empty")
        self.handlers[name] = handler
        self._refresh_description()

    @staticmethod
    def _new_task_id() -> str:
        return f"task_{uuid4().hex}"

    def submit_task(
        self,
        subject: str,
        description: str = "",
        handler: str = "",
        payload: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> BackgroundTask:
        if handler not in self.handlers:
            raise ValueError(f"Unknown background handler: {handler}")
        task = BackgroundTask(
            id=self._new_task_id(),
            subject=subject.strip(),
            description=description,
            handler=handler,
            payload=dict(payload or {}),
            session_id=session_id,
            created_at=time(),
        )
        self.store.save_task(task)
        self._start_task(task.id)
        return task

    def _start_task(self, task_id: str) -> None:
        try:
            self.executor.submit(self._execute, task_id)
        except RuntimeError as exc:
            task = self.store.load_task(task_id)
            task.status = "failed"
            task.error = str(exc)
            task.finished_at = time()
            self.store.save_task(task)
            return

    def _execute(self, task_id: str) -> None:
        with self._lock:
            task = self.store.load_task(task_id)
            if task.status != "pending":
                return
            task.status = "in_progress"
            task.started_at = time()
            self.store.save_task(task)
            handler = self.handlers.get(task.handler)

        if handler is None:
            self._finish_task(
                task_id,
                status="failed",
                error=f"Unknown background handler: {task.handler}",
            )
            return

        try:
            result = handler(task.payload)
        except Exception as exc:  # handler failures belong to the task record
            self._finish_task(
                task_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        else:
            try:
                json.dumps(result, ensure_ascii=False)
            except TypeError:
                result = repr(result)
            self._finish_task(task_id, status="completed", result=result)

    def _finish_task(
        self,
        task_id: str,
        *,
        status: Literal["completed", "failed", "cancelled"],
        result: Any = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            task = self.store.load_task(task_id)
            if task.status == "cancelled":
                return
            task.status = status
            task.result = result
            task.error = error
            task.event_id = f"event_{uuid4().hex}"
            task.finished_at = time()
            self.store.save_task(task)

        if self.on_task_finished is not None:
            try:
                self.on_task_finished(task)
            except Exception:
                # Notification hooks must not change a successful task result.
                pass

    def get_task(self, task_id: str) -> BackgroundTask:
        return self.store.load_task(task_id)

    def list_tasks(self) -> list[BackgroundTask]:
        return self.store.list_tasks()

    def cancel_task(self, task_id: str) -> BackgroundTask:
        with self._lock:
            task = self.store.load_task(task_id)
            if task.status != "pending":
                raise ValueError(
                    f"Background task {task_id} is {task.status}, cannot cancel"
                )
            task.status = "cancelled"
            task.event_id = f"event_{uuid4().hex}"
            task.finished_at = time()
            self.store.save_task(task)
        if self.on_task_finished is not None:
            try:
                self.on_task_finished(task)
            except Exception:
                pass
        return task

    def retry_task(self, task_id: str) -> BackgroundTask:
        with self._lock:
            task = self.store.load_task(task_id)
            if task.status not in {"failed", "cancelled"}:
                raise ValueError(
                    f"Background task {task_id} is {task.status}, cannot retry"
                )
            task.status = "pending"
            task.result = None
            task.error = None
            task.event_id = None
            task.started_at = None
            task.finished_at = None
            task.acknowledged_at = None
            self.store.save_task(task)
        self._start_task(task.id)
        return task

    def pending_events(self, session_id: str) -> list[BackgroundTask]:
        return self.store.pending_events(session_id)

    def acknowledge_event(self, task_id: str) -> None:
        with self._lock:
            task = self.store.load_task(task_id)
            if task.status not in {"completed", "failed", "cancelled"}:
                return
            task.acknowledged_at = time()
            self.store.save_task(task)

    def has_pending(self, session_id: str) -> bool:
        return any(
            task.session_id == session_id
            and task.status in {"pending", "in_progress"}
            for task in self.list_tasks()
        )

    def shutdown(self, wait: bool = False) -> None:
        self.executor.shutdown(wait=wait, cancel_futures=not wait)

    def run(
        self,
        args: BackgroundTaskArgs,
        context: ToolContext,
    ) -> dict[str, Any]:
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

            task_id = args.task.id
            if args.action == "submit":
                session = getattr(context, "session", None)
                session_id = getattr(session, "session_id", None)
                task = self.submit_task(
                    subject=args.task.subject,
                    description=args.task.description,
                    handler=args.task.handler,
                    payload=args.task.payload,
                    session_id=session_id,
                )
                return {"ok": True, "task": asdict(task)}

            if args.action == "get":
                return {"ok": True, "task": asdict(self.get_task(task_id))}

            if args.action == "cancel":
                return {"ok": True, "task": asdict(self.cancel_task(task_id))}

            task = self.retry_task(task_id)
            return {"ok": True, "task": asdict(task)}
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return {"ok": False, "error": str(exc)}
