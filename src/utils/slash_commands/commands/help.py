from collections.abc import Sequence

from prompt_toolkit import print_formatted_text

from src.utils.slash_commands.base import BaseCommand, CommandContext, CommandResult


class HelpCommand(BaseCommand):
    name = "help"
    description = "Show all slash commands and their usage"
    usage = "/help"

    def __init__(self, commands: Sequence[BaseCommand]):
        self._commands = (self, *commands)

    def execute(self, args: list[str], context: CommandContext) -> CommandResult:
        if args:
            print_formatted_text(f"Usage: {self.usage}")
            return CommandResult()

        lines = ["Available slash commands:"]
        for command in self._commands:
            lines.append(f"  {command.usage:<12} {command.description}")

        print_formatted_text("\n".join(lines))
        return CommandResult()
