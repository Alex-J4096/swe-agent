from src.utils.slash_commands.command_register import CommandRegistry
from src.utils.slash_commands.commands.clear import ClearCommand


def create_command_registry():
    return CommandRegistry(
        commands=[
            ClearCommand()
        ]
    )