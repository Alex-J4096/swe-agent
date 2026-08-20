from pathlib import Path

from src.prompts import SUBAGENT
from src.runtime.agent_runner import AgentRunResult, AgentRunner
from src.runtime.session import Session
from src.tools.toolset import ToolSet
from src.tools.base import ToolContext


class SubagentService:
    def __init__(self, runner: AgentRunner, toolset: ToolSet) -> None:
        self.runner = runner
        self.toolset = toolset

    def run(self, task: str, model: str) -> AgentRunResult:
        task = task.strip()
        if not task:
            return AgentRunResult(
                ok=False,
                error="Subagent task cannot be empty.",
            )

        session = Session(
            model=model,
            history=[{"role": "user", "content": task}],
        )

        return self.runner.run(
            session=session,
            toolset=self.toolset,
            system_prompt=self._build_system_prompt(),
            tool_context=ToolContext(
                session=session,
                approval_source_kind="subagent",
                approval_interactive=True,
            ),
        )

    def _build_system_prompt(self) -> str:
        return SUBAGENT.format(
            workspace=Path.cwd(),
            skills=self.toolset.skill_loader.get_skill_description(),
        )
