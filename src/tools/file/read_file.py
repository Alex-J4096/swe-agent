from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from src.tools.base import BaseTool

class ReadFileArgs(BaseModel):
    file_path: str = Field(description="File path relative to project root.")

class ReadFileTool(BaseTool[ReadFileArgs]):
    name = "read_file"
    description = "Read a text file from the project workspace."
    args_model = ReadFileArgs

    def __init__(self, project_root: str | Path=".") -> None:
        self.project_root = Path(project_root).resolve()

    def run(self, args: ReadFileArgs) -> dict[str, Any]:
        file_path = (self.project_root / args.file_path).resolve()

        if not str(file_path).startswith(str(self.project_root)):
            return {
                "ok": False,
                "error": "path escape project root",
            }

        if not file_path.is_file():
            return {
                "ok": False,
                "error": f"file not found: {args.file_path}",
            }
        try:
            content = file_path.read_text(encoding="utf-8")
            return {
                "ok": True,
                "path": args.file_path,
                "content": content,
            }
        except UnicodeDecodeError:
            return {
                "ok": False,
                "error": f"file is not a valid UTF-8 file: {args.file_path}",
            }