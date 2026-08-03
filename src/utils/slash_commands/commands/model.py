from openai import OpenAIError
from prompt_toolkit import prompt
from prompt_toolkit.completion import FuzzyWordCompleter

from src.utils.slash_commands.base import BaseCommand, CommandContext, CommandResult


# 切换模型
class ModelCommand(BaseCommand):
    name = "model"
    description = "Switch models"
    usage = "/model"

    def execute(self, args: list[str], context: CommandContext) -> CommandResult:
        if args:
            return CommandResult("Usage: /model")

        try:
            model_ids = context.provider.list_model_ids()
        except OpenAIError:
            return CommandResult("Failed to load models from provider.")

        if not model_ids:
            return CommandResult("No models available from provider.")

        selected = self._select_model(model_ids, context.session.model)

        if selected is None:
            return CommandResult("Model switch cancelled.")

        context.session.model = selected
        return CommandResult(f"Model switched to: {selected}")

    def _select_model(self, model_ids: list[str], current_model: str) -> str | None:
        completer = FuzzyWordCompleter(model_ids, WORD=True)

        try:
            selected = prompt(
                f"Select model [{current_model}]: ",
                completer=completer,
                complete_while_typing=True,
            ).strip()
        except (EOFError, KeyboardInterrupt):
            return None

        if not selected:
            return None

        if selected not in model_ids:
            return None

        return selected
