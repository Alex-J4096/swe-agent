from typing import List

from openai import APIConnectionError, APITimeoutError, OpenAI, OpenAIError
from dotenv import load_dotenv

load_dotenv()

PROVIDER_BASE_URLS = {
    "OpenAI": "https://api.openai.com/v1",
    "SiliconFlow": "https://api.siliconflow.cn/v1",
    "OpenCode Go": "https://opencode.ai/zen/go/v1",
}


class MissingAPIKeyError(OpenAIError):
    """Raised when a request is attempted before a provider is configured."""


class _MissingAPIKeyClient:
    def __getattr__(self, name: str):
        raise MissingAPIKeyError(
            "API key is not configured. Use /keys reset <provider> to configure a key, then /model."
        )

class Provider:
    def __init__(
        self,
        provider_name: str,
        api_key: str | None,
        base_url: str | None = None,
    ):
        self.provider_name = provider_name
        self.api_key = api_key
        self.base_url = base_url or PROVIDER_BASE_URLS.get(provider_name)
        self.credential_source = "runtime" if api_key else "none"
        self.credential_id: str | None = None
        self.client = self.initialize_client()

    def update_from(self, provider: "Provider") -> None:
        """Replace this provider in place so existing runtime components follow it."""
        self.provider_name = provider.provider_name
        self.api_key = provider.api_key
        self.base_url = provider.base_url
        self.client = provider.client
        self.credential_source = getattr(provider, "credential_source", "runtime")
        self.credential_id = getattr(provider, "credential_id", None)

    def initialize_client(self):
        if not self.api_key:
            return _MissingAPIKeyClient()
        if self.base_url is None:
            return OpenAI(api_key=self.api_key)
        return OpenAI(base_url=self.base_url, api_key=self.api_key)

    def list_model_ids(self) -> List[str]:
        response = self.client.with_options(timeout=15, max_retries=0).models.list()

        return sorted({
            model.id
            for model in response.data
            if model.id
        })


def model_load_error(provider_name: str, exc: OpenAIError) -> str:
    # Exception messages/response bodies may echo credentials. Only expose
    # structured status and numeric provider codes, never raw server text.
    status = getattr(exc, "status_code", None)
    code = getattr(exc, "code", None)
    details = []
    if isinstance(status, int):
        details.append(f"HTTP {status}")
    if str(code).isdigit():
        details.append(f"provider code {code}")
    suffix = f" ({', '.join(details)})" if details else ""
    if status == 401:
        reason = (
            "Authentication failed: the provider rejected the API key. "
            f"Run /keys reset {provider_name} to enter a replacement key."
        )
    elif status == 403:
        reason = "Access denied: check the key's permissions and provider account access."
    elif status == 429:
        reason = "Request limited: check the provider's rate limits and account quota."
    elif isinstance(exc, APITimeoutError):
        reason = "Request timed out: check network connectivity and proxy settings."
    elif isinstance(exc, APIConnectionError):
        reason = "Connection failed before an API response: check DNS, network connectivity, proxy settings, and certificates."
    else:
        reason = "Provider request failed: check the provider endpoint and service status."
    return f"Failed to load models from {provider_name}{suffix}. {reason}"
