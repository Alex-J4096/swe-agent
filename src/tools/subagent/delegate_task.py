from pydantic import BaseModel, Field

from src.runtime.subagent_service import SubagentService
from src.tools.base import BaseTool, ToolContext


class DelegateTaskArgs(BaseModel):
    task: str = Field(description=("A complete, self-contained task for a subagent."
                      "The subagent cannot see the parent conversation."
                                   ))

class DelegateTaskTool(BaseTool[DelegateTaskArgs]):
    name = "delegate_task"
    description = (
        "Delegate a self-contained task to an isolated subagent. "
        "The subagent shares the workspace and tools but cannot see "
        "the parent conversation."
    )
    args_model = DelegateTaskArgs

    def __init__(self, service: SubagentService):
        self.service = service

    def run(self, args: DelegateTaskArgs, context: ToolContext) -> dict:
        result = self.service.run(
            task=args.task,
            model=context.session.model,
        )

        if not result.ok:
            return {
                "ok": False,
                "error": result.error,
            }
        return {
            "ok": True,
            "content": result.content,
        }
