from typing import Any

from pydantic import ValidationError

from src.tools.base import BaseTool
from src.tools.shell.bash import BashTool


class ToolSet:
    def __init__(self) -> None:
        tool_list: list[BaseTool[Any]] = [
            BashTool(),
        ]

        self.tools = {tool.name: tool for tool in tool_list}


    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema for tool in self.tools.values()]

    def dispatch(self, name: str, arguments_json: str) -> dict[str, Any]:
        tool = self.tools.get(name)
        if tool is None:
            return {
                "ok": False,
                "error": f"Tool not found: {name}",
            }
        try:
            args = tool.args_model.model_validate_json(arguments_json)
            return tool.run(args)

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