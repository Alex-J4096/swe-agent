from openai import OpenAIError

from src.infrastructure.database import ProviderProfile
from src.infrastructure.credential_service import CredentialService
from src.infrastructure.model_provider import Provider, model_load_error
from src.ui.selection_menu import select_menu
from src.utils.slash_commands.base import BaseCommand, CommandContext, CommandResult


# 切换模型
class ModelCommand(BaseCommand):
    name = "model"
    description = "Switch models"
    usage = "/model"

    def execute(self, args: list[str], context: CommandContext) -> CommandResult:
        if args:
            return CommandResult(f"Usage: {self.usage}")

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
        resolution = CredentialService(context.database).resolve(selected_provider)
        api_key = resolution.token
        if api_key is None:
            return CommandResult(
                f"No API key configured for {selected_provider.name}. "
                f"Run /keys reset {selected_provider.id}, then /model."
            )

        candidate = Provider(
            provider_name=selected_provider.name,
            api_key=api_key,
            base_url=selected_provider.base_url,
        )
        CredentialService.stamp(candidate, resolution)
        try:
            model_ids = candidate.list_model_ids()
        except OpenAIError as exc:
            return CommandResult(
                model_load_error(selected_provider.name, exc)
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
        return select_menu("Select provider", provider_names, current_provider)

    def _select_model(self, model_ids: list[str], current_model: str) -> str | None:
        return select_menu("Select model", model_ids, current_model)
