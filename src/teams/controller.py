"""Application-facing assembly for a Lead team and its teammate workers."""

from __future__ import annotations

from threading import Thread
from typing import Callable

from src.infrastructure.model_provider import Provider
from src.runtime.agent_runner import AgentRunner
from src.runtime.approval import ApprovalRuntime, ApprovalRuntimeEvent
from src.teams.teammate_worker import TeammateWorker
from src.teams.team_runtime import TeamRuntime
from src.teams.protocol import PlanStatus
from src.tools.team.propose_plan import ProposeTeamPlanTool
from src.tools.toolset import ToolSet


class TeamController:
    def __init__(
        self,
        provider: Provider,
        workdir,
        toolset: ToolSet,
        runner_factory: Callable[[], AgentRunner],
        model_getter: Callable[[], str],
        approval_runtime: ApprovalRuntime | None = None,
        *,
        worker_count: int = 2,
    ) -> None:
        if worker_count < 1:
            raise ValueError("worker_count must be at least 1")
        self.provider = provider
        self.workdir = workdir
        self.toolset = toolset
        self.runner_factory = runner_factory
        self.model_getter = model_getter
        self.worker_count = worker_count
        self.approval_runtime = approval_runtime
        self._unsubscribe_approval = (
            approval_runtime.subscribe(self._on_approval_event)
            if approval_runtime is not None
            else None
        )
        self.runtime: TeamRuntime | None = None
        self.workers: list[TeammateWorker] = []
        self.threads: list[Thread] = []

    def _on_approval_event(self, event: ApprovalRuntimeEvent) -> None:
        source = event.request.source
        if source.kind != "team" or source.agent_id is None or self.runtime is None:
            return
        state = (
            AgentState.awaiting_approval
            if event.kind == "request_created"
            else AgentState.work
        )
        try:
            self.runtime.set_member_state(
                source.agent_id,
                state,
                task_id=source.task_id,
                session_id=source.session_id,
            )
        except ValueError:
            return

    def start_team(self, team_id: str, worker_count: int | None = None) -> TeamRuntime:
        if self.runtime is not None:
            raise ValueError("A team is already active")
        if not team_id.strip():
            raise ValueError("team_id cannot be empty")
        if worker_count is not None and worker_count < 1:
            raise ValueError("worker_count must be at least 1")
        self.worker_count = worker_count or self.worker_count
        self.runtime = TeamRuntime(self.workdir, team_id)
        existing = self.toolset.registry.get("propose_team_plan")
        if isinstance(existing, ProposeTeamPlanTool):
            existing.runtime = self.runtime
        else:
            self.toolset.register(ProposeTeamPlanTool(self.runtime))
        plan = self.runtime.state().plan
        if plan is not None and plan.status == PlanStatus.approved:
            self._start_workers()
        return self.runtime

    def approve_and_start(self, plan_id: str) -> int:
        if self.runtime is None:
            raise ValueError("No active team")
        tasks = self.runtime.approve_plan(plan_id)
        self._start_workers()
        return len(tasks)

    def _start_workers(self) -> None:
        if self.runtime is None:
            raise ValueError("No active team")
        if self.threads:
            return
        for index in range(self.worker_count):
            agent_id = f"teammate-{index + 1}"
            worker = TeammateWorker(
                runtime=self.runtime,
                agent_id=agent_id,
                model=self.model_getter(),
                runner=self.runner_factory(),
                toolset=self.toolset.view(
                    exclude={"delegate_task", "propose_team_plan"}
                ),
                system_prompt=(
                    "You are a teammate in an Agent Team. Work only on your "
                    "claimed task, use the provided workspace, and report a "
                    "concise concrete result when finished."
                ),
            )
            thread = Thread(
                target=worker.run_forever,
                name=agent_id,
                daemon=True,
            )
            self.workers.append(worker)
            self.threads.append(thread)
            thread.start()

    def shutdown(self) -> None:
        if self.runtime is None:
            return
        self.runtime.request_shutdown()
        if self.approval_runtime is not None:
            for worker in self.workers:
                self.approval_runtime.cancel_source(
                    agent_id=worker.agent_id,
                    reason="Team shutdown cancelled the approval request",
                )
        for worker in self.workers:
            worker.stop()
        for thread in self.threads:
            thread.join(timeout=5)
        self.runtime.mark_stopped()
        self.workers.clear()
        self.threads.clear()
        self.toolset.unregister("propose_team_plan")
        self.runtime = None
