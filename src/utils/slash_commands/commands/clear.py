from src.utils.slash_commands.base import BaseCommand
from src.utils.slash_commands.base import CommandContext, CommandResult
from src.runtime.context_rewind import ContextCheckpointManager


# 清空当前会话
class ClearCommand(BaseCommand):
    name = "clear"
    description = "Clear current session"
    usage = "/clear"

    def execute(self, args: list[str], context: CommandContext) -> CommandResult:
        if args:
            return CommandResult("Usage: /clear")

        message_count = len(context.session.history)
        tool_state_count = len(context.session.tool_state)
        context.session.history.clear()
        context.session.tool_state.clear()
        ContextCheckpointManager.clear(context.session)
        if (
            context.session_store is not None
            and context.session_store.exists(context.session.session_id)
        ):
            context.session_store.save(
                context.session,
                workdir=context.workdir,
            )

        return CommandResult(
            message=(
                "Cleared session: "
                f"{message_count} messages, "
                f"{tool_state_count} tool states."
            )
        )
