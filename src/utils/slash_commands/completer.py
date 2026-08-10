"""Command completion for the interactive prompt."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document

from src.utils.slash_commands.base import BaseCommand


class SlashCommandCompleter(Completer):
    """Suggest slash commands only while the first input token is a command."""

    def __init__(self, commands: Iterable[BaseCommand]):
        self._commands = tuple(commands)

    def get_completions(
        self,
        document: Document,
        complete_event,
    ) -> Iterator[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/") or any(char.isspace() for char in text):
            return

        query = text[1:].lower()
        for command in self._commands:
            if command.name.lower().startswith(query):
                yield Completion(
                    text=f"/{command.name}",
                    start_position=-len(text),
                    display=f"/{command.name}",
                    display_meta=command.description,
                )

            for alias in command.aliases:
                if alias.lower().startswith(query):
                    yield Completion(
                        text=f"/{alias}",
                        start_position=-len(text),
                        display=f"/{alias}",
                        display_meta=f"Alias for /{command.name}",
                    )
