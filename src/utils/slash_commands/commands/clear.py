from src.utils.slash_commands.base import BaseCommand
from src.utils.slash_commands.base import CommandContext, CommandResult


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

        return CommandResult(
            message=(
                "Cleared session: "
                f"{message_count} messages, "
                f"{tool_state_count} tool states."
            )
        )
