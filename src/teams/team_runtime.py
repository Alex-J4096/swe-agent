"""Team lifecycle, plan approval, event delivery, and mutation gating."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.runtime.session import Session
from src.teams.message_bus import FileLock, FileMessageBus
from src.teams.protocol import (
    AgentRecord,
    AgentRole,
    AgentState,
    EventKind,
    PlanStatus,
    ShutdownState,
    TeamEvent,
    TeamPlan,
    TeamState,
)
from src.teams.task_board import TaskBoard, TeamTask
from src.teams.worktree import WorktreeManager
from src.utils.shell import is_readonly_command


class TeamRuntime:
    """Persistent coordination boundary shared by Lead and teammates."""

    MUTATING_TOOLS = {"write_file", "edit_file", "background_task"}

    def __init__(
        self,
        control_root: Path,
        team_id: str,
        *,
        lead_id: str = "lead",
    ) -> None:
        self.root = (control_root / ".swe-agent" / "teams" / team_id).resolve()
        self.team_id = team_id
        self.lead_id = lead_id
        self.state_path = self.root / "state.json"
        self.state_lock = self.root / ".locks" / "state.lock"
        self.bus = FileMessageBus(self.root)
        self.board = TaskBoard(self.root)
        self.worktrees = WorktreeManager(control_root, self.root)
        if not self.state_path.exists():
            self._save(TeamState(team_id=team_id, lead_id=lead_id))
        # Recover files and claims left behind by a terminated runtime before
        # any new teammate starts consuming the team state.
        self.bus.recover_processing()
        self.board.reclaim_expired()
        if lead_id not in self.state().members:
            self.register_member(lead_id, AgentRole.lead)

    def _save(self, state: TeamState) -> None:
        from src.tools.task.store import atomic_write_json

        with FileLock(self.state_lock):
            atomic_write_json(self.state_path, state.model_dump(mode="json"))

    def _update_state(self, update) -> Any:
        from src.tools.task.store import atomic_write_json

        with FileLock(self.state_lock):
            state = TeamState.model_validate(
                json.loads(self.state_path.read_text(encoding="utf-8"))
            )
            result = update(state)
            atomic_write_json(self.state_path, state.model_dump(mode="json"))
            return result

    def state(self) -> TeamState:
        return TeamState.model_validate(
            json.loads(self.state_path.read_text(encoding="utf-8"))
        )

    def register_member(
        self,
        agent_id: str,
        role: AgentRole = AgentRole.teammate,
        *,
        session_id: str | None = None,
    ) -> AgentRecord:
        record = AgentRecord(
            agent_id=agent_id,
            role=role,
            session_id=session_id,
        )
        def update(current: TeamState) -> None:
            current.members[agent_id] = record

        self._update_state(update)
        return record

    def propose_plan(self, plan: TeamPlan) -> TeamPlan:
        plan.status = PlanStatus.awaiting_approval
        def update(current: TeamState) -> None:
            if current.shutdown != ShutdownState.running:
                raise ValueError("Team is shutting down")
            if current.plan is not None and current.plan.status == PlanStatus.approved:
                raise ValueError("Team plan is already approved")
            current.plan = plan

        self._update_state(update)
        return plan

    def approve_plan(self, plan_id: str) -> list[TeamTask]:
        current = self.state()
        if current.plan is None or current.plan.plan_id != plan_id:
            raise ValueError("Plan not found")
        if current.plan.status != PlanStatus.awaiting_approval:
            raise ValueError(f"Plan is {current.plan.status.value}")

        if len({item.key for item in current.plan.tasks}) != len(current.plan.tasks):
            raise ValueError("Plan task keys must be unique")

        team_tasks = [
            TeamTask(
                subject=item.subject,
                description=item.description,
                workspace_mode=item.workspace_mode,
            )
            for item in current.plan.tasks
        ]
        task_ids_by_key = {
            item.key: task.task_id
            for item, task in zip(current.plan.tasks, team_tasks)
        }
        for item, task in zip(current.plan.tasks, team_tasks):
            unknown = [
                dependency
                for dependency in item.depends_on
                if dependency not in task_ids_by_key
            ]
            if unknown:
                raise ValueError(f"Unknown plan dependency: {unknown[0]}")
            task.depends_on = [task_ids_by_key[dependency] for dependency in item.depends_on]
        created = self.board.create_tasks(team_tasks)
        current.plan.status = PlanStatus.approved
        current.plan.approved_at = datetime.now(timezone.utc)
        self._save(current)
        for task in created:
            self.send(
                recipient_id=self.lead_id,
                kind=EventKind.task_assigned,
                payload=task.model_dump(mode="json"),
            )
        return created

    def send(
        self,
        *,
        recipient_id: str,
        kind: EventKind,
        payload: dict[str, Any],
        sender_id: str | None = None,
        correlation_id: str | None = None,
    ) -> TeamEvent:
        event = TeamEvent(
            team_id=self.team_id,
            sender_id=sender_id or self.lead_id,
            recipient_id=recipient_id,
            kind=kind,
            payload=payload,
            correlation_id=correlation_id,
        )
        self.bus.send(event)
        return event

    def claim_task(
        self,
        agent_id: str,
        task_id: str | None = None,
        *,
        lease_seconds: float = 3600,
    ) -> TeamTask | None:
        self.board.reclaim_expired()
        return self.board.claim_ready(
            agent_id,
            task_id,
            lease_seconds=lease_seconds,
        )

    def prepare_task_workspace(self, task: TeamTask) -> TeamTask:
        """Create or reuse the workspace selected by the approved plan."""
        workspace = self.worktrees.prepare(task.task_id, task.workspace_mode)
        if task.claim_token is None:
            raise ValueError("Cannot bind workspace without a task claim")
        if task.workspace_mode == "worktree":
            return self.board.bind_workspace(
                task.task_id,
                task.claim_token,
                workspace,
            )
        return task

    def renew_task(
        self,
        task: TeamTask,
        *,
        lease_seconds: float = 3600,
    ) -> TeamTask:
        if task.claim_token is None:
            raise ValueError("Cannot renew an unclaimed task")
        return self.board.renew(
            task.task_id,
            task.claim_token,
            lease_seconds=lease_seconds,
        )

    def deliver_pending(self, session: Session, recipient_id: str) -> int:
        delivered_ids = set(session.tool_state.get("team_event_ids", []))
        count = 0
        for delivery in self.bus.poll(recipient_id):
            event = delivery.event
            if event.event_id in delivered_ids:
                self.bus.ack(delivery)
                continue
            session.history.append(
                {
                    "role": "user",
                    "content": (
                        "[Team event]\n"
                        "This is an external team event, not a new user request.\n"
                        f"{event.model_dump_json()}"
                    ),
                }
            )
            delivered_ids.add(event.event_id)
            count += 1
            self.bus.ack(delivery)
        session.tool_state["team_event_ids"] = sorted(delivered_ids)
        return count

    def request_shutdown(self) -> None:
        current = self.state()
        if current.shutdown == ShutdownState.stopped:
            return
        self._update_state(
            lambda state: setattr(state, "shutdown", ShutdownState.requested)
        )
        for agent_id, member in current.members.items():
            if agent_id != self.lead_id and member.state not in {
                AgentState.stopped,
            }:
                self.send(
                    recipient_id=agent_id,
                    kind=EventKind.shutdown_requested,
                    payload={"team_id": self.team_id},
                )

    def mark_stopped(self) -> None:
        def update(current: TeamState) -> None:
            current.shutdown = ShutdownState.stopped
            for member in current.members.values():
                member.state = AgentState.stopped

        self._update_state(update)

    def set_member_state(
        self,
        agent_id: str,
        state: AgentState,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        def update(current: TeamState) -> None:
            member = current.members.get(agent_id)
            if member is None:
                raise ValueError(f"Unknown team member: {agent_id}")
            member.state = state
            member.task_id = task_id
            if session_id is not None:
                member.session_id = session_id

        self._update_state(update)

    def guard(self, tool_name: str, args: dict[str, Any], context: Any) -> str | None:
        """Return a denial reason for a mutation outside the approved task scope."""
        if not self._is_mutation(tool_name, args):
            return None
        if tool_name == "task_manager":
            return "Use the shared TeamTask board instead of task_manager in a team"
        current = self.state()
        if current.plan is None or current.plan.status != PlanStatus.approved:
            return "Team plan must be approved before modifying tools can run"

        agent_id = getattr(context, "agent_id", None)
        task_id = getattr(context, "task_id", None)
        if agent_id == self.lead_id:
            return "Lead is orchestration-only; delegate workspace changes to a teammate"
        if not agent_id or not task_id:
            return "A teammate must have a claimed task before modifying the workspace"
        task = self.board.get(task_id)
        if task is None or task.owner != agent_id or task.status != "in_progress":
            return "The teammate does not own an active task"
        return None

    @classmethod
    def _is_mutation(cls, tool_name: str, args: dict[str, Any]) -> bool:
        if tool_name in {"write_file", "edit_file"}:
            return True
        if tool_name == "task_manager":
            return args.get("action") != "list"
        if tool_name == "background_task":
            return args.get("action") not in {"get", "list"}
        if tool_name != "run_bash":
            return False
        command = args.get("command")
        return not is_readonly_command(command)
