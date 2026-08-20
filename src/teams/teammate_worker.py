"""Independent teammate Agent Loop backed by the shared team runtime."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from time import monotonic, sleep
from typing import Callable

from src.runtime.agent_runner import AgentRunResult, AgentRunner
from src.runtime.session import Session
from src.teams.protocol import AgentRole, AgentState, EventKind, ShutdownState
from src.teams.team_runtime import TeamRuntime
from src.tools.base import ToolContext
from src.tools.toolset import ToolSet


class TeammateWorker:
    """Run one teammate until the team requests shutdown."""

    def __init__(
        self,
        runtime: TeamRuntime,
        agent_id: str,
        model: str,
        runner: AgentRunner,
        toolset: ToolSet,
        system_prompt: str,
        *,
        poll_interval: float = 0.2,
        task_lease_seconds: float = 3600,
        on_result: Callable[[AgentRunResult], None] | None = None,
    ) -> None:
        if not agent_id.strip():
            raise ValueError("agent_id cannot be empty")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if task_lease_seconds <= 0:
            raise ValueError("task_lease_seconds must be positive")
        self.runtime = runtime
        self.agent_id = agent_id
        self.model = model
        self.runner = runner
        self.toolset = toolset
        self.system_prompt = system_prompt
        self.poll_interval = poll_interval
        self.task_lease_seconds = task_lease_seconds
        self.on_result = on_result
        self.stop_event = Event()
        self._stop_requested = False
        self._deferred_deliveries = []
        self.runtime.register_member(agent_id, AgentRole.teammate)

    def stop(self) -> None:
        self.stop_event.set()

    def run_forever(self) -> None:
        self.runtime.bus.recover_processing(self.agent_id)
        self.runtime.board.reclaim_expired()
        while not self.stop_event.is_set():
            self._consume_control_events()
            if self._stop_requested or self.runtime.state().shutdown != ShutdownState.running:
                break

            task = self.runtime.claim_task(
                self.agent_id,
                lease_seconds=self.task_lease_seconds,
            )
            if task is None:
                sleep(self.poll_interval)
                continue

            try:
                task = self.runtime.prepare_task_workspace(task)
            except Exception as exc:
                self.runtime.board.fail(
                    task.task_id,
                    task.claim_token or "",
                    f"workspace preparation failed: {exc}",
                )
                self.runtime.send(
                    recipient_id=self.runtime.lead_id,
                    sender_id=self.agent_id,
                    kind=EventKind.task_result,
                    payload={
                        "task_id": task.task_id,
                        "ok": False,
                        "content": None,
                        "error": f"workspace preparation failed: {exc}",
                    },
                )
                continue

            self.runtime.send(
                recipient_id=self.runtime.lead_id,
                sender_id=self.agent_id,
                kind=EventKind.task_claimed,
                payload={
                    "task_id": task.task_id,
                    "owner": self.agent_id,
                },
            )
            self._run_task(task)

        self.runtime.set_member_state(self.agent_id, AgentState.stopped)

    def _consume_control_events(self) -> None:
        for delivery in self.runtime.bus.poll(self.agent_id):
            try:
                if delivery.event.kind in {
                    EventKind.shutdown_requested,
                    EventKind.cancel_requested,
                }:
                    self._stop_requested = True
                else:
                    self._deferred_deliveries.append(delivery)
            finally:
                if delivery.event.kind in {
                    EventKind.shutdown_requested,
                    EventKind.cancel_requested,
                }:
                    self.runtime.bus.ack(delivery)

    def _deliver_deferred(self, session: Session) -> None:
        while self._deferred_deliveries:
            delivery = self._deferred_deliveries.pop(0)
            session.history.append(
                {
                    "role": "user",
                    "content": (
                        "[Team event]\n"
                        "This is an external team event, not a new user request.\n"
                        f"{delivery.event.model_dump_json()}"
                    ),
                }
            )
            self.runtime.bus.ack(delivery)

    def _run_task(self, task) -> None:
        workspace_root = Path(task.workspace_root or self.runner.workdir).resolve()
        session = Session(
            model=self.model,
            history=[
                {
                    "role": "user",
                    "content": (
                        f"Task: {task.subject}\n"
                        f"Description: {task.description}\n"
                        "Complete this task and report the concrete result."
                    ),
                }
            ],
        )
        self.runtime.set_member_state(
            self.agent_id,
            AgentState.work,
            task_id=task.task_id,
            session_id=session.session_id,
        )

        context = ToolContext(
            session=session,
            team_runtime=self.runtime,
            agent_id=self.agent_id,
            task_id=task.task_id,
            workspace_root=workspace_root,
            approval_source_kind="team",
            approval_interactive=False,
        )

        def before_model_request(current_session: Session) -> None:
            self._consume_control_events()
            self._deliver_deferred(current_session)
            self.runtime.renew_task(
                task,
                lease_seconds=self.task_lease_seconds,
            )

        result = self.runner.run(
            session=session,
            toolset=self.toolset,
            system_prompt=self.system_prompt,
            before_model_request=before_model_request,
            tool_context=context,
        )
        if result.ok and not result.stopped_by_permission:
            self.runtime.board.complete(
                task.task_id,
                task.claim_token or "",
                result={"ok": True, "content": result.content},
            )
        else:
            failure = (
                "Task stopped because a required permission was denied"
                if result.stopped_by_permission
                else (result.error or "teammate run failed")
            )
            self.runtime.board.fail(
                task.task_id,
                task.claim_token or "",
                failure,
            )
        self.runtime.send(
            recipient_id=self.runtime.lead_id,
            sender_id=self.agent_id,
            kind=EventKind.task_result,
            payload={
                "task_id": task.task_id,
                "ok": result.ok and not result.stopped_by_permission,
                "content": result.content,
                "error": (
                    "Task stopped because a required permission was denied"
                    if result.stopped_by_permission
                    else result.error
                ),
            },
        )
        self.runtime.set_member_state(self.agent_id, AgentState.idle)
        self.runtime.send(
            recipient_id=self.runtime.lead_id,
            sender_id=self.agent_id,
            kind=EventKind.status_changed,
            payload={
                "agent_id": self.agent_id,
                "state": AgentState.idle.value,
            },
        )
        if self.on_result is not None:
            self.on_result(result)
