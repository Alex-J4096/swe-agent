from src.utils.slash_commands.base import BaseCommand
from src.utils.slash_commands.base import CommandContext, CommandResult


# 清空上下文历史对话
class ClearCommand(BaseCommand):
    name = "clear"
    description = "Clear current session history"

    def execute(self, args: list[str], context: CommandContext) -> CommandResult:
        if args:
            return CommandResult("Usage: /clear")

        message_count = len(context.session.history)
        context.session.history.clear()

        return CommandResult(message=f"Clear session history: {message_count}")
