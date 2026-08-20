"""Thread-safe approval request coordination shared by all agent runners."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from threading import Condition, RLock
from time import time
from typing import Literal
from uuid import uuid4


ApprovalDecision = Literal["approve_once", "approve_turn", "approve_session", "reject"]
ApprovalStatus = Literal["pending", "resolved", "cancelled"]


@dataclass(frozen=True)
class ApprovalSource:
    kind: str
    session_id: str
    agent_id: str | None = None
    task_id: str | None = None
    interactive: bool = True


@dataclass(frozen=True)
class ApprovalResponse:
    decision: ApprovalDecision
    feedback: str = ""

    @property
    def approved(self) -> bool:
        return self.decision != "reject"


@dataclass(frozen=True)
class ApprovalRequestRecord:
    id: str
    tool_call_id: str
    tool_name: str
    arguments_json: str
    reason: str
    preview: str
    scope_key: str | None
    capability: str | None
    source: ApprovalSource
    status: ApprovalStatus = "pending"
    response: ApprovalResponse | None = None
    created_at: float = 0.0
    resolved_at: float | None = None


@dataclass(frozen=True)
class ApprovalRuntimeEvent:
    kind: Literal["request_created", "request_resolved", "request_cancelled"]
    request: ApprovalRequestRecord


ApprovalSubscriber = Callable[[ApprovalRuntimeEvent], None]


class ApprovalRuntime:
    """Own pending approvals and wake the exact blocked tool call on resolution."""

    def __init__(self, history_limit: int = 256) -> None:
        if history_limit < 1:
            raise ValueError("history_limit must be positive")
        self._condition = Condition(RLock())
        self._records: dict[str, ApprovalRequestRecord] = {}
        self._order: list[str] = []
        self._subscribers: list[ApprovalSubscriber] = []
        self._history_limit = history_limit

    def subscribe(self, callback: ApprovalSubscriber) -> Callable[[], None]:
        with self._condition:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._condition:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def _emit(self, event: ApprovalRuntimeEvent) -> None:
        with self._condition:
            subscribers = tuple(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                # Observers must never change approval semantics.
                continue

    def _trim_history_locked(self) -> None:
        while len(self._order) > self._history_limit:
            request_id = self._order[0]
            record = self._records[request_id]
            if record.status == "pending":
                return
            self._order.pop(0)
            self._records.pop(request_id, None)

    def create_request(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        arguments_json: str,
        reason: str,
        preview: str,
        scope_key: str | None,
        capability: str | None,
        source: ApprovalSource,
    ) -> ApprovalRequestRecord:
        record = ApprovalRequestRecord(
            id=f"approval_{uuid4().hex}",
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments_json=arguments_json,
            reason=reason,
            preview=preview,
            scope_key=scope_key,
            capability=capability,
            source=source,
            created_at=time(),
        )
        with self._condition:
            self._records[record.id] = record
            self._order.append(record.id)
            self._trim_history_locked()
        self._emit(ApprovalRuntimeEvent("request_created", record))
        return record

    def get_request(self, request_id: str) -> ApprovalRequestRecord | None:
        with self._condition:
            return self._records.get(request_id)

    def list_pending(self) -> list[ApprovalRequestRecord]:
        with self._condition:
            return [
                self._records[request_id]
                for request_id in self._order
                if self._records[request_id].status == "pending"
            ]

    def resolve(self, request_id: str, response: ApprovalResponse) -> bool:
        with self._condition:
            current = self._records.get(request_id)
            if current is None or current.status != "pending":
                return False
            resolved = replace(
                current,
                status="resolved",
                response=response,
                resolved_at=time(),
            )
            self._records[request_id] = resolved
            # Publish state changes before waking the tool waiter. This prevents
            # a fast worker from finishing and setting itself idle before the
            # UI/team observer has left awaiting_approval.
            self._emit(ApprovalRuntimeEvent("request_resolved", resolved))
            self._condition.notify_all()
            self._trim_history_locked()
        return True

    def cancel(self, request_id: str, reason: str = "Approval cancelled") -> bool:
        with self._condition:
            current = self._records.get(request_id)
            if current is None or current.status != "pending":
                return False
            cancelled = replace(
                current,
                status="cancelled",
                response=ApprovalResponse("reject", reason),
                resolved_at=time(),
            )
            self._records[request_id] = cancelled
            self._emit(ApprovalRuntimeEvent("request_cancelled", cancelled))
            self._condition.notify_all()
            self._trim_history_locked()
        return True

    def cancel_source(
        self,
        *,
        session_id: str | None = None,
        agent_id: str | None = None,
        task_id: str | None = None,
        reason: str = "Approval source stopped",
    ) -> int:
        pending = self.list_pending()
        matches = [
            record
            for record in pending
            if (session_id is None or record.source.session_id == session_id)
            and (agent_id is None or record.source.agent_id == agent_id)
            and (task_id is None or record.source.task_id == task_id)
        ]
        return sum(self.cancel(record.id, reason) for record in matches)

    def wait_for_response(self, request_id: str) -> ApprovalResponse:
        with self._condition:
            while True:
                record = self._records.get(request_id)
                if record is None:
                    return ApprovalResponse("reject", "Approval request disappeared")
                if record.status != "pending":
                    return record.response or ApprovalResponse(
                        "reject", "Approval request was cancelled"
                    )
                self._condition.wait()
