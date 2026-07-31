from src.utils.slash_commands.command_register import CommandRegistry
from src.utils.slash_commands.commands.clear import ClearCommand
from src.utils.slash_commands.commands.compact import CompactCommand
from src.utils.slash_commands.commands.debug import DebugCommand
from src.utils.slash_commands.commands.model import ModelCommand


def create_command_registry():
    return CommandRegistry(
        commands=[
            ClearCommand(),
            CompactCommand(),
            DebugCommand(),
            ModelCommand(),
        ]
    )
