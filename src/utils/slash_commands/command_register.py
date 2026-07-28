import shlex

from src.utils.slash_commands.base import (
    BaseCommand,
    CommandContext,
    CommandResult,
)


class CommandRegistry:
    def __init__(self, commands: list[BaseCommand]):
        self.commands: dict[str, BaseCommand] = {}

        for cmd in commands:
            self.commands[cmd.name] = cmd
            for alias in cmd.aliases:
                self.commands[alias] = cmd

    def dispatch(self, line: str, context: CommandContext) -> CommandResult:
        try:
            parts = shlex.split(line[1:], posix=False)
        except ValueError as e:
            return CommandResult(f"Syntax error: {e}")

        if not parts:
            return CommandResult(f"Need command, /help for help.")

        command = self.commands.get(parts[0].lower())
        if command is None:
            return CommandResult(f"Unknown command: {line}, /help for help.")

        return command.execute(parts[1:], context)
