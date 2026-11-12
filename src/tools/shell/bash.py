from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from src.tools.base import BaseTool, ToolContext
from src.runtime.sandbox import (
    CommandExecutor,
    SandboxPolicy,
    SandboxedCommandExecutor,
)


class BashArgs(BaseModel):
    command: str = Field(description="Shell command to execute.")
    cwd: str = Field(default=".", description="Working directory relative to project root.")
    network_access: bool = Field(
        default=False,
        description=(
            "Declare that the command needs network access. In sandbox mode this "
            "requests network approval; host mode always requires once-only approval "
            "and does not isolate network access."
        ),
    )
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

    def __init__(
        self,
        project_root: str | Path = ".",
        executor: CommandExecutor | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.executor = executor or SandboxedCommandExecutor()
        if getattr(self.executor, "execution_mode", "sandbox") == "host":
            shell = "PowerShell" if getattr(self.executor, "windows", False) else "POSIX sh"
            self.description = (
                f"Run a command on the host using {shell}, without OS sandbox isolation. "
                "Every call requires explicit once-only approval. On Windows use curl.exe, "
                "and PowerShell syntax (Windows PowerShell 5 may not support &&)."
            )

    def run(
        self,
        args: BashArgs,
        context: ToolContext,
    ) -> dict[str, Any]:
        project_root = (getattr(context, "workspace_root", None) or self.project_root).resolve()
        workdir = (project_root / args.cwd).resolve()

        host = getattr(self.executor, "execution_mode", "sandbox") == "host"
        if host and "host.execute" not in context.granted_capabilities:
            return {"ok": False, "error": "Host execution requires explicit approval", "exit_code": -1}

        if not workdir.is_relative_to(project_root):
            return {
                "ok": False,
                "stdout": "",
                "stderr": "cwd escapes project root",
                "exit_code": -1
            }

        result = self.executor.execute(
            args.command,
            cwd=workdir,
            timeout_seconds=args.timeout_seconds,
            policy=SandboxPolicy(
                workspace=project_root,
                allow_network=(
                    "sandbox.network" in context.granted_capabilities
                ),
            ),
        )
        return {
            "ok": result.exit_code == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
            "sandboxed": result.sandboxed,
            "sandbox_backend": result.backend,
            "execution_mode": "host" if host else "sandbox",
            "network_enabled": host or "sandbox.network" in context.granted_capabilities,
        }
