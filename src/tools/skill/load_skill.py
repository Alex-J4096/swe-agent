from pydantic import BaseModel, Field

from src.infrastructure.skill_loader import SkillLoader
from src.tools.base import BaseTool


class LoadSkillArgs(BaseModel):
    name: str = Field(description="Skill name")

class LoadSkill(BaseTool[LoadSkillArgs]):
    name = "load_skill"
    description = "Load specialized knowledge by name."
    args_model = LoadSkillArgs

    def __init__(self, loader: SkillLoader) -> None:
        self.loader = loader

    def run(self, args: LoadSkillArgs) -> dict:
        if args.name not in self.loader.skills:
            return {
                "ok": False,
                "error": f"Unknown skill: {args.name}",
                "available": list(self.loader.skills),
            }

        return {
            "ok": True,
            "skill": args.name,
            "content": self.loader.get_content(args.name),
        }