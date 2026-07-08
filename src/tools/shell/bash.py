import subprocess
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from src.tools.base import BaseTool


class BashArgs(BaseModel):
    command: str = Field(description="Shell command to execute.")
    cwd: str = Field(default=".", description="Working directory relative to project root.")
    timeout_seconds: int = Field(
        default=60,
        ge=1,
        le=60,
        description="Maximum execution time in seconds.",
    )

class BashTool(BaseTool[BashArgs]):
    name = "run_bash"
    description = "Run a safe shell command in the project workspace."
    args_model = BashArgs

    def __init__(self, project_root: str | Path = ".") -> None:
        self.project_root = Path(project_root).resolve()

    def run(self, args: BashArgs) -> dict[str, Any]:
        workdir = (self.project_root / args.cwd).resolve()

        if not str(workdir).startswith(str(self.project_root)):
            return {
                "ok": False,
                "stdout": "",
                "stderr": "cwd escapes project root",
                "exit_code": -1
            }

        try:
            result = subprocess.run(
                args.command,
                shell=True,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=args.timeout_seconds,
            )

            return {
                "ok": result.returncode == 0,
                "stdout": result.stdout[-12000:],
                "stderr": result.stderr[-12000:],
                "exit_code": result.returncode,
            }

        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "stdout": "",
                "stderr": f"Timeout after {args.timeout_seconds}s",
                "exit_code": -1
            }




