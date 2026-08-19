"""Typed wire and lifecycle protocol for Agent Teams."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AgentRole(str, Enum):
    lead = "lead"
    teammate = "teammate"


class AgentState(str, Enum):
    idle = "idle"
    work = "work"
    failed = "failed"
    stopping = "stopping"
    stopped = "stopped"


class PlanStatus(str, Enum):
    draft = "draft"
    awaiting_approval = "awaiting_approval"
    approved = "approved"
    rejected = "rejected"
    completed = "completed"


class ShutdownState(str, Enum):
    running = "running"
    requested = "requested"
    draining = "draining"
    stopped = "stopped"


class EventKind(str, Enum):
    message = "message"
    plan_proposed = "plan_proposed"
    plan_approved = "plan_approved"
    task_assigned = "task_assigned"
    task_claimed = "task_claimed"
    task_result = "task_result"
    status_changed = "status_changed"
    shutdown_requested = "shutdown_requested"
    shutdown_ack = "shutdown_ack"
    cancel_requested = "cancel_requested"


class TeamEvent(BaseModel):
    """Persisted event envelope; payload is intentionally event-specific."""

    schema_version: int = Field(default=1, ge=1)
    event_id: str = Field(default_factory=lambda: f"event_{uuid4().hex}")
    team_id: str
    sender_id: str
    recipient_id: str
    kind: EventKind
    payload: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class PlanTask(BaseModel):
    key: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    description: str = ""
    depends_on: list[str] = Field(default_factory=list)
    owner_hint: str | None = None
    workspace_mode: str = Field(default="repo", pattern="^(repo|worktree)$")


class TeamPlan(BaseModel):
    plan_id: str = Field(default_factory=lambda: f"plan_{uuid4().hex}")
    goal: str = Field(min_length=1)
    tasks: list[PlanTask] = Field(default_factory=list)
    status: PlanStatus = PlanStatus.draft
    created_at: datetime = Field(default_factory=utc_now)
    approved_at: datetime | None = None


class AgentRecord(BaseModel):
    agent_id: str
    role: AgentRole
    state: AgentState = AgentState.idle
    task_id: str | None = None
    session_id: str | None = None


class TeamState(BaseModel):
    team_id: str
    lead_id: str = "lead"
    plan: TeamPlan | None = None
    shutdown: ShutdownState = ShutdownState.running
    members: dict[str, AgentRecord] = Field(default_factory=dict)
