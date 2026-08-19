"""Application-facing assembly for a Lead team and its teammate workers."""

from __future__ import annotations

from threading import Thread
from typing import Callable

from src.infrastructure.model_provider import Provider
from src.runtime.agent_runner import AgentRunner
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
        self.runtime: TeamRuntime | None = None
        self.workers: list[TeammateWorker] = []
        self.threads: list[Thread] = []

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
        for worker in self.workers:
            worker.stop()
        for thread in self.threads:
            thread.join(timeout=5)
        self.runtime.mark_stopped()
        self.workers.clear()
        self.threads.clear()
        self.toolset.unregister("propose_team_plan")
        self.runtime = None
