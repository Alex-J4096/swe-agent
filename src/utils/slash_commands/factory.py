from src.utils.slash_commands.command_register import CommandRegistry
from src.utils.slash_commands.commands.clear import ClearCommand
from src.utils.slash_commands.commands.compact import CompactCommand
from src.utils.slash_commands.commands.debug import DebugCommand
from src.utils.slash_commands.commands.help import HelpCommand
from src.utils.slash_commands.commands.model import ModelCommand
from src.utils.slash_commands.commands.memory import MemoryCommand
from src.utils.slash_commands.commands.new import NewSessionCommand
from src.utils.slash_commands.commands.resume import ResumeCommand
from src.utils.slash_commands.commands.sessions import SessionsCommand
from src.utils.slash_commands.commands.status import StatusCommand
from src.utils.slash_commands.commands.team import TeamCommand
from src.utils.slash_commands.commands.permissions import PermissionsCommand


def create_command_registry():
    commands = [
        ClearCommand(),
        CompactCommand(),
        DebugCommand(),
        ModelCommand(),
        MemoryCommand(),
        PermissionsCommand(),
        SessionsCommand(),
        StatusCommand(),
        TeamCommand(),
        ResumeCommand(),
        NewSessionCommand(),
    ]
    commands.insert(0, HelpCommand(commands))
    return CommandRegistry(commands=commands)
