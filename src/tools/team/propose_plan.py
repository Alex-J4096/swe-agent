"""Lead capability for proposing a typed team plan before approval."""

from pydantic import BaseModel, Field

from src.teams.protocol import PlanTask, TeamPlan
from src.teams.team_runtime import TeamRuntime
from src.tools.base import BaseTool, ToolContext


class ProposeTeamPlanArgs(BaseModel):
    goal: str = Field(min_length=1, description="The team's overall goal.")
    tasks: list[PlanTask] = Field(
        min_length=1,
        description=(
            "Delegable tasks. Each key must be unique; depends_on refers to "
            "another task key."
        ),
    )


class ProposeTeamPlanTool(BaseTool[ProposeTeamPlanArgs]):
    name = "propose_team_plan"
    description = (
        "Propose a structured team plan for user approval. This does not "
        "modify the workspace or start teammates."
    )
    args_model = ProposeTeamPlanArgs

    def __init__(self, runtime: TeamRuntime) -> None:
        self.runtime = runtime

    def run(
        self,
        args: ProposeTeamPlanArgs,
        context: ToolContext,
    ) -> dict:
        plan = self.runtime.propose_plan(
            TeamPlan(goal=args.goal, tasks=args.tasks)
        )
        return {
            "ok": True,
            "status": "awaiting_approval",
            "plan": plan.model_dump(mode="json"),
            "message": "Plan proposed. Wait for explicit user approval.",
        }
