"""Manage named API keys without exposing tokens to command history."""

import sqlite3
from dataclasses import replace
from uuid import uuid4

from openai import OpenAIError
from prompt_toolkit import prompt

from src.infrastructure.credential_service import CredentialResolution, CredentialService
from src.infrastructure.database import ProviderCredential
from src.infrastructure.model_provider import Provider, model_load_error
from src.ui.selection_menu import select_menu
from src.utils.slash_commands.base import BaseCommand, CommandContext, CommandResult


class KeysCommand(BaseCommand):
    name = "keys"
    description = "List, add, select, verify, remove, or inspect API keys"
    usage = "/keys [list|add|use|test|remove|source|reset]"
    HELP = (
        "/keys list [provider]\n/keys add [provider]\n/keys use <key-id>\n"
        "/keys use env [provider]\n/keys test [key-id]\n/keys remove <key-id>\n"
        "/keys source [provider]\n/keys reset [provider]"
    )

    def execute(self, args: list[str], context: CommandContext) -> CommandResult:
        if context.database is None:
            return CommandResult("Provider configuration is not available.")
        action = args[0].lower() if args else "list"
        if action not in {"list", "add", "use", "test", "remove", "source", "reset"}:
            return CommandResult(self.HELP)
        service = CredentialService(context.database)
        try:
            if action in {"use", "test", "remove"} and len(args) > 1 and args[1] != "env":
                if len(args) != 2:
                    return CommandResult(self.HELP)
                credential = context.database.get_credential(args[1])
                if credential is None:
                    return CommandResult("API key ID not found. Use /keys list [provider].")
                profile = context.database.get_provider(credential.provider_id)
            else:
                if action == "remove" or (action == "use" and (len(args) < 2 or args[1] != "env")):
                    return CommandResult(self.HELP)
                provider_args = args[2:] if action == "use" else args[1:]
                if action == "test" and provider_args:
                    return CommandResult(self.HELP)
                profile = self._profile(provider_args, context)
                credential = None
            if profile is None:
                return CommandResult("Choose a configured provider, e.g. /keys list siliconflow.\n" + self.HELP)
            if action == "list":
                return self._list(profile, context, service)
            if action == "source":
                return self._source(profile, context, service)
            if action in {"add", "reset"}:
                return self._save(profile, context, service, reset=action == "reset")
            if action == "remove":
                return self._remove(profile, credential, context, service)
            if credential is not None:
                resolution = CredentialResolution(credential.access_token, "database", credential.id)
            elif action == "use":
                resolution = service.environment(profile)
            else:
                resolution = service.live(profile, context.provider)
                if resolution.source == "not-current-provider":
                    resolution = service.resolve(profile)
            if not resolution.token:
                return CommandResult("No API key available. Use /keys add [provider].")
            if action == "use" and credential is not None and not credential.is_active:
                return CommandResult("This API key is disabled; it cannot be selected.")
            candidate, error = self._verify(profile, resolution, service)
            if error:
                return CommandResult(error)
            if action == "test":
                return CommandResult(f"API key verified for {profile.name} (models endpoint). Source: {resolution.source}; ID: {resolution.credential_id or '-'}.")
            if credential is not None:
                context.database.set_default_credential(credential.id)
            else:
                with context.database.transaction() as connection:
                    connection.execute("UPDATE provider_credentials SET is_default = 0 WHERE provider_id = ?", (profile.id,))
            self._refresh(profile, candidate, context)
            return CommandResult(f"Default API key selected for {profile.name}: {resolution.credential_id or 'environment'}. Selected model is unchanged.")
        except (EOFError, KeyboardInterrupt):
            return CommandResult("API key update cancelled.")
        except sqlite3.IntegrityError:
            return CommandResult("API key could not be saved: its name already exists or the configuration changed. Existing credentials were preserved.")
        except ValueError:
            return CommandResult("API key operation failed: the requested configuration is no longer available. Use /keys list.")

    @staticmethod
    def _profile(args, context):
        providers = context.database.list_providers()
        if args:
            requested = " ".join(args).strip('"').casefold()
            return next((p for p in providers if requested in {p.id.casefold(), p.name.casefold()}), None)
        return next((p for p in providers if p.id == context.session.provider_id), None) or next(
            (p for p in providers if p.name == getattr(context.provider, "provider_name", None)), None
        )

    @staticmethod
    def _refresh(profile, candidate, context):
        if getattr(context.provider, "provider_name", None) != profile.name:
            return
        if hasattr(context.provider, "update_from"):
            context.provider.update_from(candidate)
        else:
            context.provider = candidate
        if context.on_provider_changed is not None:
            context.on_provider_changed(context.provider)

    @staticmethod
    def _verify(profile, resolution, service, *, record=True):
        candidate = Provider(provider_name=profile.name, api_key=resolution.token, base_url=profile.base_url)
        service.stamp(candidate, resolution)
        saved = service.database.get_credential(resolution.credential_id) if resolution.credential_id and record else None
        record_id = saved.id if saved is not None and saved.access_token == resolution.token else None
        try:
            candidate.list_model_ids()
        except OpenAIError as exc:
            if record_id:
                service.record_check(record_id, exc)
            return None, model_load_error(profile.name, exc)
        if record_id:
            service.record_check(record_id)
        return candidate, None

    def _list(self, profile, context, service):
        live = service.live(profile, context.provider)
        lines = [f"API keys — {profile.name}", "ID | Name | Default | In use | Enabled | Last verification (UTC)"]
        credentials = context.database.list_credentials(profile.id)
        for item in credentials:
            check = context.database.get_credential_check(item.id)
            checked = "never"
            if check:
                checked = f"{check['status']} at {check['checked_at']}"
                if check["http_status"]:
                    checked += f" (HTTP {check['http_status']})"
                if check["provider_code"]:
                    checked += f" code {check['provider_code']}"
            in_use = live.source == "database" and live.credential_id == item.id and live.token == item.access_token
            lines.append(f"{item.id} | {item.name} | {'yes' if item.is_default else '-'} | {'yes' if in_use else '-'} | {'yes' if item.is_active else 'no'} | {checked}")
        if not credentials:
            lines.append("No stored API keys.")
        lines.append(f"Environment ({profile.api_key_env or 'not configured'}): {'configured' if service.environment(profile).token else 'not configured'}; tokens are never displayed.")
        lines.append("Use /keys source for actual runtime and future-client sources.\n" + self.HELP)
        return CommandResult("\n".join(lines))

    def _source(self, profile, context, service):
        live = service.live(profile, context.provider)
        configured = service.resolve(profile)
        runtime = f"{live.source}; ID: {live.credential_id or '-'}"
        if live.credential_id:
            saved = context.database.get_credential(live.credential_id)
            if saved is None or saved.access_token != live.token:
                runtime += " (runtime snapshot differs from stored configuration)"
        elif live.source == "environment" and live.token != service.environment(profile).token:
            runtime += " (runtime snapshot differs from current environment)"
        return CommandResult(
            f"Provider: {profile.name}\nCurrent runtime: {runtime}\n"
            f"Next client/startup: {configured.source}; ID: {configured.credential_id or '-'}\n"
            f"Environment variable: {profile.api_key_env or '-'} ({'configured' if service.environment(profile).token else 'not configured'})\n"
            "Priority: saved default key > environment variable > no key. No automatic key rotation."
        )

    def _save(self, profile, context, service, *, reset):
        credentials = context.database.list_credentials(profile.id)
        previous = context.database.get_default_credential(profile.id) if reset else None
        if reset:
            previous = previous or next((c for c in credentials if c.name == "default"), None)
            name = previous.name if previous else "default"
        else:
            name = prompt(f"Name for {profile.name} API key: ").strip()
            if not name:
                return CommandResult("API key update cancelled.")
            if any(c.name == name for c in credentials):
                return CommandResult("An API key with that name already exists. Use a different name or /keys reset.")
        token = prompt(f"Enter API key for {profile.name}: ", is_password=True).strip()
        if not token:
            return CommandResult("API key update cancelled.")
        credential_id = previous.id if previous else f"key_{uuid4().hex}"
        resolution = CredentialResolution(token, "database", credential_id)
        candidate, error = self._verify(profile, resolution, service, record=False)
        if error:
            return CommandResult(error)
        make_default = reset
        if not reset:
            answer = prompt("Make this the default key? [y/N/cancel]: ").strip().lower()
            if answer not in {"", "n", "no", "y", "yes"}:
                return CommandResult("API key update cancelled.")
            make_default = answer in {"y", "yes"}
        credential = replace(previous, access_token=token, is_default=True, is_active=True) if previous else ProviderCredential(
            id=credential_id, provider_id=profile.id, name=name, access_token=token, is_default=make_default
        )
        context.database.upsert_credential(credential)
        service.record_check(credential_id)
        if make_default:
            self._refresh(profile, candidate, context)
        verb = "updated" if reset else "added"
        return CommandResult(f"API key {verb} for {profile.name}: {credential_id}. {'Default key selected.' if make_default else 'Saved without changing the default.'} Selected model is unchanged.")

    def _remove(self, profile, credential, context, service):
        live = service.live(profile, context.provider)
        needs_replacement = credential.is_default or (live.source == "database" and live.credential_id == credential.id)
        replacement = None
        resolution = None
        candidate = None
        if needs_replacement:
            alternatives = [c for c in context.database.list_credentials(profile.id) if c.id != credential.id and c.is_active]
            options = {f"{c.name} ({c.id})": c for c in alternatives}
            env = service.environment(profile)
            env_label = f"Use environment variable {profile.api_key_env}"
            labels = list(options)
            if env.token:
                labels.append(env_label)
            if labels:
                selected = select_menu("Choose replacement before deleting the key", labels)
                if selected is None:
                    return CommandResult("API key removal cancelled.")
                replacement = options.get(selected)
                resolution = CredentialResolution(replacement.access_token, "database", replacement.id) if replacement else env
            else:
                resolution = CredentialResolution(None)
            if resolution.token:
                candidate, error = self._verify(profile, resolution, service)
                if error:
                    return CommandResult(error + " Original key was not removed.")
            else:
                candidate = Provider(provider_name=profile.name, api_key=None, base_url=profile.base_url)
                service.stamp(candidate, resolution)
        notice = " No replacement is configured; API requests will be disabled." if needs_replacement and not resolution.token else ""
        answer = prompt(f"Remove API key {credential.name} ({credential.id})?{notice} [y/N]: ").strip().lower()
        if answer not in {"y", "yes"}:
            return CommandResult("API key removal cancelled.")
        context.database.remove_credential(credential.id, replacement.id if replacement else None, clear_default=needs_replacement)
        if needs_replacement:
            self._refresh(profile, candidate, context)
        env_notice = " Environment variable remains configured." if service.environment(profile).token else ""
        return CommandResult(f"API key removed.{env_notice} Selected model is unchanged.")
