from abc import ABC, abstractmethod
from dataclasses import dataclass

from src.infrastructure.model_provider import Provider
from src.runtime.session import Session
from src.tools.toolset import ToolSet


@dataclass
class CommandResult:
    message: str | None = None
    exit_requested: bool = False


# 命令可以访问的运行环境
@dataclass
class CommandContext:
    session: Session
    toolset: ToolSet
    provider: Provider


class BaseCommand(ABC):
    name: str
    description: str
    usage: str
    aliases: tuple[str, ...] = ()

    @abstractmethod
    def execute(self, args: list[str], context: CommandContext) -> CommandResult:
        ...
