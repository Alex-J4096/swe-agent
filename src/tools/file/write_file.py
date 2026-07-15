from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from src.tools.base import BaseTool

class WriteFileArgs(BaseModel):
    file_path: str = Field(description="File path relative to project root.")
    content: str = Field(description="Content to write to the file.")
    overwrite: bool = Field(default=False, 
                            description="Whether to overwrite the file if it exists.")

class WriteFileTool(BaseTool[WriteFileArgs]):
    name = "write_file"
    description = "Write a text file to the project workspace."
    args_model = WriteFileArgs

    def __init__(self, project_root: str | Path=".") -> None:
        self.project_root = Path(project_root).resolve()

    def run(self, args: WriteFileArgs) -> dict[str, Any]:
        file_path = (self.project_root / args.file_path).resolve()

        if not str(file_path).startswith(str(self.project_root)):
            return {
                "ok": False,
                "error": "path escape project root",
            }

        if file_path.exists() and not args.overwrite:
            return {
                "ok": False,
                "error": f"file already exists: {args.file_path}",
            }

        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(args.content, encoding="utf-8")
            return {
                "ok": True,
                "path": args.file_path,
                "message": "file written successfully",
            }
        except Exception as e:
            return {
                "ok": False,
                "error": f"failed to write file: {str(e)}",
            }
