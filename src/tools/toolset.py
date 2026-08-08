from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from src.infrastructure.skill_loader import SkillLoader
from src.tools.base import BaseTool, ToolContext
from src.tools.file.edit_file import EditFileTool
from src.tools.file.read_file import ReadFileTool
from src.tools.file.write_file import WriteFileTool
from src.tools.shell.bash import BashTool
from src.tools.skill.load_skill import LoadSkill
from src.tools.task.task import TaskManager
from src.tools.todo_manager.todo_manager import TodoManager


class ToolRegistry:
    def __init__(self, tools: Iterable[BaseTool[Any]] = ()) -> None:
        self._tools: dict[str, BaseTool[Any]] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: BaseTool[Any]) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool {tool.name} already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool[Any] | None:
        return self._tools.get(name)

    def items(self):
        return self._tools.items()


class ToolSet:
    def __init__(
        self,
        registry: ToolRegistry | None = None,
        skill_loader: SkillLoader | None = None,
        excluded_tools: Iterable[str] = (),
    ) -> None:
        self.skill_loader = skill_loader or SkillLoader(
            Path.cwd() / ".agents" / "skills"
        )
        self.registry = registry or ToolRegistry(
            [
                BashTool(),
                ReadFileTool(),
                WriteFileTool(),
                EditFileTool(),
                TodoManager(),
                TaskManager(),
                LoadSkill(self.skill_loader),
            ]
        )
        self.excluded_tools = frozenset(excluded_tools)

    @property
    def tools(self) -> dict[str, BaseTool[Any]]:
        return {
            name: tool
            for name, tool in self.registry.items()
            if name not in self.excluded_tools
        }

    # 动态注册工具
    def register(self, tool: BaseTool[Any]) -> None:
        self.registry.register(tool)

    def view(self, *, exclude: Iterable[str] = ()) -> "ToolSet":
        return ToolSet(
            registry=self.registry,
            skill_loader=self.skill_loader,
            excluded_tools=self.excluded_tools.union(exclude),
        )

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema for tool in self.tools.values()]

    def dispatch(
        self,
        name: str,
        arguments_json: str,
        context: ToolContext,
    ) -> dict[str, Any]:
        if name in self.excluded_tools:
            tool = None
        else:
            tool = self.registry.get(name)
        if tool is None:
            return {
                "ok": False,
                "error": f"Tool not found: {name}",
            }
        try:
            args = tool.args_model.model_validate_json(arguments_json)
            # print(f"\033[46m [DEBUG]Ran\033[46m {tool.name}: {args}")
            return tool.run(args, context)

        except ValidationError as e:
            return {
                "ok": False,
                "error": "Invalid tool arguments",
                "details": e.errors(),
            }

        except Exception as e:
            return {
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
            }
