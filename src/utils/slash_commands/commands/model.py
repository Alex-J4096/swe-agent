from openai import OpenAIError
from prompt_toolkit import prompt
from prompt_toolkit.completion import FuzzyWordCompleter

from src.infrastructure.database import ProviderCredential, ProviderProfile
from src.infrastructure.model_provider import Provider
from src.utils.slash_commands.base import BaseCommand, CommandContext, CommandResult


# 切换模型
class ModelCommand(BaseCommand):
    name = "model"
    description = "Switch models"
    usage = "/model"

    def execute(self, args: list[str], context: CommandContext) -> CommandResult:
        if args:
            return CommandResult("Usage: /model")

        if context.database is None:
            return CommandResult("Provider configuration is not available.")

        providers = context.database.list_providers()
        if not providers:
            return CommandResult("No providers available.")

        current_provider = self._current_provider(providers, context)
        selected_provider_name = self._select_provider(
            providers,
            current_provider.name if current_provider else None,
        )
        if selected_provider_name is None:
            return CommandResult("Model switch cancelled.")

        selected_provider = next(
            provider
            for provider in providers
            if provider.name == selected_provider_name
        )
        api_key = self._get_api_key(selected_provider, context)
        if api_key is None:
            return CommandResult("Model switch cancelled.")

        candidate = Provider(
            provider_name=selected_provider.name,
            api_key=api_key,
            base_url=selected_provider.base_url,
        )
        try:
            model_ids = candidate.list_model_ids()
        except OpenAIError:
            return CommandResult(
                f"Failed to load models from {selected_provider.name}."
            )

        if not model_ids:
            return CommandResult(
                f"No models available from {selected_provider.name}."
            )

        current_model = (
            context.session.model
            if context.session.provider_id == selected_provider.id
            else model_ids[0]
        )
        selected = self._select_model(model_ids, current_model)

        if selected is None:
            return CommandResult("Model switch cancelled.")

        if hasattr(context.provider, "update_from"):
            context.provider.update_from(candidate)
        else:
            context.provider = candidate
        if context.on_provider_changed is not None:
            context.on_provider_changed(context.provider)

        context.database.upsert_credential(
            ProviderCredential(
                id=f"{selected_provider.id}-default",
                provider_id=selected_provider.id,
                name="default",
                access_token=api_key,
                is_default=True,
            )
        )
        context.session.provider_id = selected_provider.id
        context.session.model = selected
        if context.session_store is not None:
            context.session_store.save(
                context.session,
                workdir=context.workdir,
            )
        return CommandResult(f"Model switched to: {selected}")

    @staticmethod
    def _current_provider(
        providers: list[ProviderProfile],
        context: CommandContext,
    ) -> ProviderProfile | None:
        if context.session.provider_id is not None:
            for provider in providers:
                if provider.id == context.session.provider_id:
                    return provider
        for provider in providers:
            if provider.name == getattr(context.provider, "provider_name", None):
                return provider
        return None

    def _select_provider(
        self,
        providers: list[ProviderProfile],
        current_provider: str | None,
    ) -> str | None:
        provider_names = [provider.name for provider in providers]
        completer = FuzzyWordCompleter(provider_names, WORD=True)
        prompt_hint = current_provider or provider_names[0]
        try:
            selected = prompt(
                f"Select provider [{prompt_hint}]: ",
                completer=completer,
                complete_while_typing=True,
            ).strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not selected or selected not in provider_names:
            return None
        return selected

    @staticmethod
    def _get_api_key(
        provider: ProviderProfile,
        context: CommandContext,
    ) -> str | None:
        credential = context.database.get_default_credential(provider.id)
        if credential is not None:
            return credential.access_token

        import os

        environment_token = os.getenv(provider.api_key_env or "")
        if environment_token:
            return environment_token

        try:
            api_key = prompt(
                f"Enter API key for {provider.name}: ",
                is_password=True,
            ).strip()
        except (EOFError, KeyboardInterrupt):
            return None
        return api_key or None

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
