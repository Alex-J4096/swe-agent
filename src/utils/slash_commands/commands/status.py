from pathlib import Path

from src.utils.slash_commands.base import BaseCommand, CommandContext, CommandResult
from src.utils.welcome import format_workdir


class StatusCommand(BaseCommand):
    name = "status"
    description = "Show current model, directory, thread, and session ID"
    usage = "/status"

    def execute(self, args: list[str], context: CommandContext) -> CommandResult:
        if args:
            return CommandResult(f"Usage: {self.usage}")

        summary = None
        if context.session_store is not None:
            summary = context.session_store.get_summary(
                context.session.session_id,
            )

        directory = context.workdir
        if directory is None and summary is not None and summary.workdir:
            directory = Path(summary.workdir)

        thread_name = summary.title if summary is not None else None
        provider_name = getattr(context.provider, "provider_name", "unknown")
        registry = getattr(context.toolset, "registry", None)
        bash_tool = registry.get("run_bash") if registry is not None else None
        executor = getattr(bash_tool, "executor", None)
        sandbox_backend = getattr(executor, "backend_name", "unknown")
        sandbox_available = getattr(executor, "available", False)
        lines = [
            "Status",
            f"  Provider:    {provider_name}",
            f"  Model:       {context.session.model}",
            f"  Directory:   {format_workdir(directory) if directory else 'unknown'}",
            f"  Thread name: {thread_name or '(unnamed)'}",
            f"  Session UID: {context.session.session_id}",
            f"  Context:     {context.session.context_branch_id} "
            f"({len(context.session.context_checkpoints)} checkpoints, "
            f"generation {context.session.context_generation})",
            f"  Sandbox:     {sandbox_backend} "
            f"({'ready' if sandbox_available else 'unavailable'})",
        ]
        return CommandResult("\n".join(lines))
