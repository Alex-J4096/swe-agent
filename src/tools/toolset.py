from typing import Any

from pydantic import ValidationError

from src.tools.base import BaseTool
from src.tools.file.edit_file import EditFileTool
from src.tools.file.read_file import ReadFileTool
from src.tools.file.write_file import WriteFileTool
from src.tools.shell.bash import BashTool
from src.tools.todo_manager import TodoManager
from src.utils.logger import Logger


class ToolSet:
    def __init__(self) -> None:
        tool_list: list[BaseTool[Any]] = [
            BashTool(),
            ReadFileTool(),
            WriteFileTool(),
            EditFileTool(),
            TodoManager()
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
            # print(f"\033[46m [DEBUG]Ran\033[46m {tool.name}: {args}")
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
