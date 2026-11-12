"""Resolve configured credentials and track the authority of a live client."""

import os
from dataclasses import dataclass, field

from openai import OpenAIError

from src.infrastructure.database import AppDatabase, ProviderProfile


@dataclass(frozen=True)
class CredentialResolution:
    token: str | None = field(repr=False)
    source: str = "none"
    credential_id: str | None = None


class CredentialService:
    def __init__(self, database: AppDatabase):
        self.database = database

    def resolve(self, profile: ProviderProfile) -> CredentialResolution:
        credential = self.database.get_default_credential(profile.id)
        if credential is not None:
            return CredentialResolution(credential.access_token, "database", credential.id)
        return self.environment(profile)

    @staticmethod
    def environment(profile: ProviderProfile) -> CredentialResolution:
        token = os.getenv(profile.api_key_env or "") or None
        return CredentialResolution(token, "environment" if token else "none")

    @staticmethod
    def stamp(provider, resolution: CredentialResolution) -> None:
        provider.credential_source = resolution.source
        provider.credential_id = resolution.credential_id

    def live(self, profile: ProviderProfile, provider) -> CredentialResolution:
        if getattr(provider, "provider_name", None) != profile.name:
            return CredentialResolution(None, "not-current-provider")
        token = getattr(provider, "api_key", None)
        if not isinstance(token, str) or not token:
            return CredentialResolution(None)
        source = getattr(provider, "credential_source", None)
        credential_id = getattr(provider, "credential_id", None)
        # Retain actual provenance even if an environment variable or saved key
        # changes after the client was created.
        if source in {"database", "environment"}:
            return CredentialResolution(token, source, credential_id)
        for credential in self.database.list_credentials(profile.id):
            if credential.access_token == token:
                return CredentialResolution(token, "database", credential.id)
        if self.environment(profile).token == token:
            return CredentialResolution(token, "environment")
        return CredentialResolution(token, "runtime")

    def record_check(self, credential_id: str, error: OpenAIError | None = None) -> None:
        status = getattr(error, "status_code", None)
        code = getattr(error, "code", None)
        result = "ok" if error is None else {
            401: "authentication-failed", 403: "access-denied", 429: "limited",
        }.get(status, "unverified")
        self.database.record_credential_check(
            credential_id, result,
            status if isinstance(status, int) else None,
            str(code) if str(code).isdigit() else None,
        )
