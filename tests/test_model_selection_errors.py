from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError, AuthenticationError

from src.infrastructure.database import ProviderCredential, ProviderProfile
from src.infrastructure.model_provider import model_load_error
from src.utils.slash_commands.commands.keys import KeysCommand
from src.runtime.session import Session
from src.utils.slash_commands.base import CommandContext
from src.utils.slash_commands.commands.model import ModelCommand


def context():
    profile = ProviderProfile("siliconflow", "SiliconFlow", "https://api.siliconflow.cn/v1", "SILICONFLOW_API_KEY")
    database = Mock()
    database.list_providers.return_value = [profile]
    database.list_credentials.return_value = []
    database.get_default_credential.return_value = ProviderCredential(
        id="stored", provider_id="siliconflow", name="default", access_token="stored-test-key"
    )
    return CommandContext(
        session=Session(model="old-model", provider_id="siliconflow"),
        toolset=object(), provider=Mock(), database=database,
    )


def authentication_error():
    return AuthenticationError(
        "server echoed stored-test-key",
        response=httpx.Response(401, request=httpx.Request("GET", "https://api.siliconflow.cn/v1/models")),
        body={"code": 30014, "message": "stored-test-key"},
    )


def test_auth_failure_shows_status_and_recovery_without_secrets():
    ctx = context()
    candidate = Mock()
    candidate.list_model_ids.side_effect = authentication_error()
    with (
        patch("src.utils.slash_commands.commands.model.Provider", return_value=candidate),
        patch("src.utils.slash_commands.commands.model.select_menu", return_value="SiliconFlow"),
    ):
        result = ModelCommand().execute([], ctx)
    assert "HTTP 401" in result.message and "30014" in result.message
    assert "/keys reset" in result.message
    assert "stored-test-key" not in result.message
    assert ctx.session.model == "old-model"
    ctx.database.upsert_credential.assert_not_called()
    ctx.provider.update_from.assert_not_called()


def test_reset_key_bypasses_saved_and_environment_credentials(monkeypatch):
    monkeypatch.setenv("SILICONFLOW_API_KEY", "environment-test-key")
    ctx = context()
    candidate = SimpleNamespace(list_model_ids=lambda: ["new-model"])
    with (
        patch("src.utils.slash_commands.commands.keys.Provider", return_value=candidate) as provider,
        patch("src.utils.slash_commands.commands.keys.prompt", side_effect=["replacement-test-key"]) as prompt,
    ):
        result = KeysCommand().execute(["reset"], ctx)
    assert "API key updated for SiliconFlow" in result.message
    assert ctx.session.model == "old-model"
    assert provider.call_args.kwargs["api_key"] == "replacement-test-key"
    assert prompt.call_args_list[0].kwargs["is_password"]
    assert ctx.database.upsert_credential.call_args.args[0].access_token == "replacement-test-key"


def test_failed_replacement_does_not_overwrite_saved_key():
    ctx = context()
    candidate = Mock()
    candidate.list_model_ids.side_effect = authentication_error()
    with (
        patch("src.utils.slash_commands.commands.keys.Provider", return_value=candidate),
        patch("src.utils.slash_commands.commands.keys.prompt", side_effect=["bad-replacement"]),
    ):
        result = KeysCommand().execute(["reset"], ctx)
    assert "HTTP 401" in result.message
    ctx.database.upsert_credential.assert_not_called()


@pytest.mark.parametrize("error,expected", [
    (APITimeoutError(request=httpx.Request("GET", "https://example.com")), "timed out"),
    (APIConnectionError(request=httpx.Request("GET", "https://example.com")), "Connection failed"),
    (APIStatusError("hidden", response=httpx.Response(403, request=httpx.Request("GET", "https://example.com")), body=None), "Access denied"),
    (APIStatusError("hidden", response=httpx.Response(429, request=httpx.Request("GET", "https://example.com")), body=None), "Request limited"),
])
def test_network_and_status_errors_have_distinct_messages(error, expected):
    assert expected in model_load_error("SiliconFlow", error)


def test_model_rejects_removed_key_reset_option():
    assert ModelCommand().execute(["--reset-key"], context()).message == "Usage: /model"


def test_model_missing_key_redirects_without_prompting_or_saving(monkeypatch):
    ctx = context()
    ctx.database.get_default_credential.return_value = None
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    with patch("src.utils.slash_commands.commands.model.select_menu", return_value="SiliconFlow") as prompt:
        result = ModelCommand().execute([], ctx)
    assert "/keys reset siliconflow" in result.message
    assert prompt.call_count == 1
    ctx.database.upsert_credential.assert_not_called()


def test_keys_reset_refreshes_live_provider_without_changing_model():
    ctx = context()
    ctx.provider.provider_name = "SiliconFlow"
    ctx.on_provider_changed = Mock()
    candidate = SimpleNamespace(list_model_ids=lambda: [])
    with (
        patch("src.utils.slash_commands.commands.keys.Provider", return_value=candidate),
        patch("src.utils.slash_commands.commands.keys.prompt", return_value="replacement-test-key"),
    ):
        result = KeysCommand().execute(["reset", "siliconflow"], ctx)
    assert "API key updated" in result.message
    ctx.provider.update_from.assert_called_once_with(candidate)
    ctx.on_provider_changed.assert_called_once_with(ctx.provider)
    assert ctx.session.model == "old-model"
    assert ctx.session.provider_id == "siliconflow"


def test_keys_reset_other_provider_does_not_switch_live_client():
    ctx = context()
    ctx.provider.provider_name = "Other"
    ctx.session.provider_id = "other"
    candidate = SimpleNamespace(list_model_ids=lambda: [])
    with (
        patch("src.utils.slash_commands.commands.keys.Provider", return_value=candidate),
        patch("src.utils.slash_commands.commands.keys.prompt", return_value="replacement-test-key"),
    ):
        result = KeysCommand().execute(["reset", "SiliconFlow"], ctx)
    assert "API key updated" in result.message
    ctx.database.upsert_credential.assert_called_once()
    ctx.provider.update_from.assert_not_called()
    assert ctx.session.provider_id == "other"


@pytest.mark.parametrize("answer", ["", KeyboardInterrupt(), EOFError()])
def test_keys_cancel_leaves_existing_configuration_untouched(answer):
    ctx = context()
    with patch("src.utils.slash_commands.commands.keys.prompt", side_effect=[answer]):
        result = KeysCommand().execute(["reset"], ctx)
    assert "cancelled" in result.message
    ctx.database.upsert_credential.assert_not_called()
    ctx.provider.update_from.assert_not_called()


def test_keys_registered_in_slash_dispatch():
    from src.utils.slash_commands.factory import create_command_registry
    result = create_command_registry().dispatch("/keys", context())
    assert "API keys" in result.message
    assert "/keys add" in result.message


def test_provider_menu_cancel_does_not_load_models():
    ctx = context()
    with (
        patch("src.utils.slash_commands.commands.model.select_menu", return_value=None),
        patch("src.utils.slash_commands.commands.model.Provider") as provider,
    ):
        result = ModelCommand().execute([], ctx)
    assert "cancelled" in result.message
    provider.assert_not_called()
    ctx.provider.update_from.assert_not_called()


def test_model_menu_receives_loaded_list_and_cancel_preserves_configuration():
    ctx = context()
    candidate = SimpleNamespace(list_model_ids=lambda: ["new-model", "old-model"])
    with (
        patch("src.utils.slash_commands.commands.model.Provider", return_value=candidate),
        patch("src.utils.slash_commands.commands.model.select_menu", side_effect=["SiliconFlow", None]) as menu,
    ):
        result = ModelCommand().execute([], ctx)
    assert menu.call_args_list[0].args == ("Select provider", ["SiliconFlow"], "SiliconFlow")
    assert menu.call_args_list[1].args == ("Select model", ["new-model", "old-model"], "old-model")
    assert "cancelled" in result.message
    assert ctx.session.model == "old-model"
    ctx.provider.update_from.assert_not_called()
