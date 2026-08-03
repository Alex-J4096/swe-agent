from src.utils.slash_commands.command_register import CommandRegistry
from src.utils.slash_commands.commands.clear import ClearCommand
from src.utils.slash_commands.commands.compact import CompactCommand
from src.utils.slash_commands.commands.debug import DebugCommand
from src.utils.slash_commands.commands.help import HelpCommand
from src.utils.slash_commands.commands.model import ModelCommand


def create_command_registry():
    commands = [
        ClearCommand(),
        CompactCommand(),
        DebugCommand(),
        ModelCommand(),
    ]
    commands.insert(0, HelpCommand(commands))
    return CommandRegistry(commands=commands)
