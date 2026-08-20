from src.utils.slash_commands.base import BaseCommand, CommandContext, CommandResult
from src.utils.slash_commands.commands.debug import DebugCommand


RECENT_MESSAGE_LIMIT = 10


def _copy_session(target, source) -> None:
    target.model = source.model
    target.provider_id = source.provider_id
    target.session_id = source.session_id
    target.history = source.history
    target.tool_state = source.tool_state
    target.approved_command_prefixes = source.approved_command_prefixes
    target.approved_permission_scopes = source.approved_permission_scopes
    target.memory_enabled = source.memory_enabled
    target.context_checkpoints = source.context_checkpoints
    target.context_branch_id = source.context_branch_id
    target.context_generation = source.context_generation
    target.pending_context_rewind = None
    target.running = True


class ResumeCommand(BaseCommand):
    name = "resume"
    description = "Resume a persisted session"
    usage = "/resume <session-id>"

    def execute(self, args: list[str], context: CommandContext) -> CommandResult:
        if len(args) != 1:
            return CommandResult(f"Usage: {self.usage}")
        if context.session_store is None:
            return CommandResult("Session persistence is not configured.")

        loaded = context.session_store.load(args[0])
        if loaded is None:
            return CommandResult(f"Session not found: {args[0]}")

        _copy_session(context.session, loaded)
        if context.renderer is not None:
            context.renderer.show_notice(
                f"Resumed session {loaded.session_id} "
                f"({len(loaded.history)} messages)."
            )
            if not loaded.history:
                context.renderer.show_notice("Recent session history is empty.")
                return CommandResult()

            recent_count = min(len(loaded.history), RECENT_MESSAGE_LIMIT)
            context.renderer.show_notice(
                f"Recent session history (last {recent_count} messages):"
            )
            context.renderer.show_history(
                loaded.history,
                limit=RECENT_MESSAGE_LIMIT,
            )
            return CommandResult()

        if not loaded.history:
            return CommandResult(
                f"Resumed session {loaded.session_id} (0 messages).\n\n"
                "Recent session history is empty."
            )

        recent_history = loaded.history[-RECENT_MESSAGE_LIMIT:]
        start_index = len(loaded.history) - len(recent_history) + 1
        formatted_history = DebugCommand().format_history(
            recent_history,
            start_index=start_index,
        )
        return CommandResult(
            f"Resumed session {loaded.session_id} "
            f"({len(loaded.history)} messages).\n\n"
            f"Recent session history (last {len(recent_history)} messages):\n"
            f"{formatted_history}"
        )
