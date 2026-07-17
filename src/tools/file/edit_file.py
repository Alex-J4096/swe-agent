from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from src.tools.base import BaseTool


class EditFileArgs(BaseModel):
    file_path: str = Field(description="File path relative to project root.")
    old_string: str = Field(description="Exact text to replace.")
    new_string: str = Field(description="Replacement text.")
    expected_replacements: int = Field(
        default=1,
        ge=1,
        description="Expected number of occurrences to replace.",
    )


class EditFileTool(BaseTool[EditFileArgs]):
    name = "edit_file"
    description = "Replace exact text in an existing text file in the project workspace."
    args_model = EditFileArgs

    def __init__(self, project_root: str | Path = ".") -> None:
        self.project_root = Path(project_root).resolve()

    def run(self, args: EditFileArgs) -> dict[str, Any]:
        file_path = (self.project_root / args.file_path).resolve()

        try:
            file_path.relative_to(self.project_root)
        except ValueError:
            return {
                "ok": False,
                "error": "path escape project root.",
            }

        if not file_path.is_file():
            return {
                "ok": False,
                "error": f"file not found: {args.file_path}",
            }

        if not args.old_string:
            return {
                "ok": False,
                "error": "old_string must not be empty.",
            }

        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return {
                "ok": False,
                "error": f"file is not a valid UTF-8 file: {args.file_path}",
            }
        except Exception as e:
            return {
                "ok": False,
                "error": f"failed to read file: {str(e)}",
            }

        replacements = content.count(args.old_string)
        if replacements != args.expected_replacements:
            return {
                "ok": False,
                "error": (
                    "replacement count mismatch: "
                    f"expected {args.expected_replacements}, found {replacements}."
                ),
            }

        try:
            file_path.write_text(
                content.replace(args.old_string, args.new_string),
                encoding="utf-8",
            )
            return {
                "ok": True,
                "path": args.file_path,
                "replacements": replacements,
                "message": "file edited successfully",
            }
        except Exception as e:
            return {
                "ok": False,
                "error": f"failed to edit file: {str(e)}",
            }
