from collections.abc import Sequence

from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import ANSI

from src.utils.slash_commands.base import BaseCommand, CommandContext, CommandResult


# ANSI helpers — these are kept minimal so the output degrades gracefully
# in non-color environments.
_ACCENT = "\033[38;5;75m"
_MUTED = "\033[38;5;243m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


class HelpCommand(BaseCommand):
    name = "help"
    description = "Show all slash commands and their usage"
    usage = "/help"

    def __init__(self, commands: Sequence[BaseCommand]):
        self._commands = (self, *commands)

    def execute(self, args: list[str], context: CommandContext) -> CommandResult:
        if args:
            print_formatted_text(ANSI(f"  {_MUTED}Usage: {self.usage}{_RESET}"))
            return CommandResult()

        lines: list[str] = [
            "",
            f"  {_ACCENT}{_BOLD}Available Commands{_RESET}",
            f"  {_MUTED}{'─' * 46}{_RESET}",
        ]
        for command in self._commands:
            cmd = f"{_ACCENT}{command.usage}{_RESET}"
            desc = f"{_MUTED}{command.description}{_RESET}"
            # Pad usage to align descriptions.  The raw usage is at most ~20
            # chars, so padding with ANSI escape lengths accounted for works
            # well enough for the formatted text renderer.
            padding = 14 - len(command.usage)
            lines.append(f"  {cmd}{' ' * max(padding, 2)}{desc}")
        lines.append(f"  {_MUTED}{'─' * 46}{_RESET}")
        lines.append(
            f"  {_MUTED}Prefix a message with // to send a literal slash.{_RESET}"
        )
        lines.append("")

        print_formatted_text(ANSI("\n".join(lines)))
        return CommandResult()
