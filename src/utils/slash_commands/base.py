from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from src.infrastructure.model_provider import Provider
from src.runtime.session import Session
from src.tools.toolset import ToolSet

if TYPE_CHECKING:
    from src.infrastructure.session_store import SessionStore
    from src.ui.console import ConsoleRenderer


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
    session_store: "SessionStore | None" = None
    workdir: Path | None = None
    renderer: "ConsoleRenderer | None" = None


class BaseCommand(ABC):
    name: str
    description: str
    usage: str
    aliases: tuple[str, ...] = ()

    @abstractmethod
    def execute(self, args: list[str], context: CommandContext) -> CommandResult:
        ...
