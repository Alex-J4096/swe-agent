from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import pytest
from openai import APITimeoutError, AuthenticationError

from src.infrastructure.credential_service import CredentialResolution, CredentialService
from src.infrastructure.database import AppDatabase, ProviderCredential, ProviderProfile
from src.runtime.session import Session
from src.utils.slash_commands.base import CommandContext
from src.utils.slash_commands.commands.keys import KeysCommand


class FakeProvider:
    calls = []

    def __init__(self, provider_name, api_key, base_url=None):
        self.provider_name = provider_name
        self.api_key = api_key
        self.base_url = base_url
        self.credential_source = "runtime" if api_key else "none"
        self.credential_id = None
        self.client = object()

    def list_model_ids(self):
        self.calls.append(self.api_key)
        request = httpx.Request("GET", "https://example.test/v1/models")
        if self.api_key == "secret-rejected":
            raise AuthenticationError("echo secret-rejected", response=httpx.Response(401, request=request), body={"code": 30014})
        if self.api_key == "secret-timeout":
            raise APITimeoutError(request=request)
        return ["model"]

    def update_from(self, other):
        self.__dict__.update(other.__dict__)


@pytest.fixture
def state(monkeypatch):
    monkeypatch.delenv("KEYS_TEST_SF_TOKEN", raising=False)
    monkeypatch.delenv("KEYS_TEST_OTHER_TOKEN", raising=False)
    database = AppDatabase(Path(":memory:"))
    sf = ProviderProfile("sf", "SiliconFlow", "https://example.test/v1", "KEYS_TEST_SF_TOKEN")
    other = ProviderProfile("other", "Other Provider", "https://other.test/v1", "KEYS_TEST_OTHER_TOKEN")
    database.upsert_provider(sf)
    database.upsert_provider(other)
    first = ProviderCredential("a", "sf", "personal", "secret-first", is_default=True)
    database.upsert_credential(first)
    active = FakeProvider(sf.name, first.access_token, sf.base_url)
    context = CommandContext(session=Session(model="model", provider_id="sf"), toolset=object(), provider=active, database=database, on_provider_changed=Mock())
    FakeProvider.calls = []
    with patch("src.utils.slash_commands.commands.keys.Provider", FakeProvider):
        yield SimpleNamespace(db=database, sf=sf, other=other, ctx=context, first=first)
    database.close()


def run(state, args, answers=(), selected="SiliconFlow"):
    with (
        patch("src.utils.slash_commands.commands.keys.prompt", side_effect=list(answers)) as prompt,
        patch("src.utils.slash_commands.commands.keys.select_menu", return_value=selected),
    ):
        result = KeysCommand().execute(args, state.ctx)
    return result.message, prompt


def second(state, *, token="secret-second", provider="sf", active=True):
    value = ProviderCredential("b", provider, "work", token, is_active=active)
    state.db.upsert_credential(value)
    return value


def test_list_shows_metadata_without_tokens(state, monkeypatch):
    second(state)
    monkeypatch.setenv(state.sf.api_key_env, "secret-env")
    text, _ = run(state, [])
    assert "personal" in text and "work" in text and "never" in text
    assert "personal | yes | yes" in text
    assert "Environment" in text and "configured" in text
    assert not any(secret in text for secret in ("secret-first", "secret-second", "secret-env"))
    assert FakeProvider.calls == []


def test_add_named_key_without_switching_default(state):
    text, prompt = run(state, ["add"], ["work", "secret-second", "n"])
    added = next(c for c in state.db.list_credentials("sf") if c.name == "work")
    assert "API key added" in text
    assert not added.is_default and state.db.get_default_credential("sf").id == "a"
    assert state.ctx.provider.api_key == "secret-first"
    assert state.db.get_credential_check(added.id)["status"] == "ok"
    assert prompt.call_args_list[1].kwargs["is_password"]
    state.ctx.on_provider_changed.assert_not_called()


def test_add_as_default_refreshes_live_provider(state):
    run(state, ["add"], ["work", "secret-second", "y"])
    added = state.db.get_default_credential("sf")
    assert added.name == "work"
    assert state.ctx.provider.api_key == "secret-second"
    assert state.ctx.provider.credential_id == added.id
    assert state.ctx.session.model == "model"
    state.ctx.on_provider_changed.assert_called_once_with(state.ctx.provider)


@pytest.mark.parametrize("answers", [[""], ["work", ""], ["work", "secret-second", "cancel"]])
def test_add_cancel_is_non_mutating(state, answers):
    run(state, ["add"], answers)
    assert len(state.db.list_credentials("sf")) == 1
    assert state.db.get_default_credential("sf").id == "a"


def test_duplicate_name_does_not_ask_for_or_replace_token(state):
    text, prompt = run(state, ["add"], ["personal"])
    assert "already exists" in text and prompt.call_count == 1
    assert FakeProvider.calls == []
    assert state.db.get_credential("a").access_token == "secret-first"


def test_invalid_add_does_not_persist(state):
    text, _ = run(state, ["add"], ["bad", "secret-rejected"])
    assert "401" in text and "secret-rejected" not in text
    assert len(state.db.list_credentials("sf")) == 1


def test_use_validates_before_switching_and_survives_new_service(state):
    second(state)
    text, _ = run(state, ["use", "b"])
    assert "selected" in text
    assert state.db.get_default_credential("sf").id == "b"
    assert state.ctx.provider.credential_id == "b"
    assert CredentialService(state.db).resolve(state.sf).token == "secret-second"
    assert state.db.get_credential_check("b")["status"] == "ok"


def test_failed_use_preserves_current_default_and_records_error(state):
    second(state, token="secret-rejected")
    text, _ = run(state, ["use", "b"])
    assert "401" in text and "30014" in text
    assert state.db.get_default_credential("sf").id == "a"
    assert state.ctx.provider.api_key == "secret-first"
    assert state.db.get_credential_check("b")["status"] == "authentication-failed"


def test_use_disabled_key_does_not_make_request(state):
    second(state, active=False)
    text, _ = run(state, ["use", "b"])
    assert "disabled" in text and FakeProvider.calls == []


def test_using_other_provider_key_does_not_switch_provider(state):
    second(state, provider="other")
    run(state, ["use", "b"])
    assert state.db.get_default_credential("other").id == "b"
    assert state.ctx.provider.provider_name == "SiliconFlow"
    assert state.ctx.provider.api_key == "secret-first"
    assert state.ctx.session.provider_id == "sf"


def test_test_command_does_not_change_default_or_live_client(state):
    second(state)
    text, _ = run(state, ["test", "b"])
    assert "verified" in text and "models endpoint" in text
    assert state.db.get_default_credential("sf").id == "a"
    assert state.ctx.provider.api_key == "secret-first"
    state.ctx.on_provider_changed.assert_not_called()


def test_test_defaults_to_actual_live_key(state):
    second(state)
    state.db.set_default_credential("b")
    run(state, ["test"])
    assert FakeProvider.calls == ["secret-first"]


def test_timeout_is_unverified_not_invalid(state):
    second(state, token="secret-timeout")
    text, _ = run(state, ["test", "b"])
    assert "timed out" in text
    assert state.db.get_credential_check("b")["status"] == "unverified"


def test_source_distinguishes_runtime_from_configured_default(state, monkeypatch):
    second(state)
    state.db.set_default_credential("b")
    monkeypatch.setenv(state.sf.api_key_env, "secret-env")
    text, _ = run(state, ["source"])
    assert "Current runtime: database; ID: a" in text
    assert "Next client/startup: database; ID: b" in text
    assert "secret-" not in text


def test_explicit_environment_selection_clears_default_without_copying_token(state, monkeypatch):
    monkeypatch.setenv(state.sf.api_key_env, "secret-env")
    run(state, ["use", "env"])
    assert state.db.get_default_credential("sf") is None
    assert state.ctx.provider.credential_source == "environment"
    assert state.ctx.provider.api_key == "secret-env"
    assert len(state.db.list_credentials("sf")) == 1
    assert CredentialService(state.db).resolve(state.sf).source == "environment"
    text, _ = run(state, ["source"])
    assert "Current runtime: environment" in text


def test_missing_environment_does_not_clear_saved_default(state):
    text, _ = run(state, ["use", "env"])
    assert "No API key" in text
    assert state.db.get_default_credential("sf").id == "a"


def test_remove_current_key_switches_to_verified_replacement(state):
    second(state)
    run(state, ["remove", "a"], ["y"], selected="work (b)")
    assert state.db.get_credential("a") is None
    assert state.db.get_default_credential("sf").id == "b"
    assert state.ctx.provider.api_key == "secret-second"


def test_remove_nondefault_does_not_change_current(state):
    second(state)
    text, _ = run(state, ["remove", "b"], ["y"])
    assert "removed" in text and state.db.get_credential("b") is None
    assert state.db.get_default_credential("sf").id == "a"
    assert FakeProvider.calls == []


def test_cancel_remove_preserves_key(state):
    second(state)
    text, _ = run(state, ["remove", "a"], selected=None)
    assert "cancelled" in text and state.db.get_credential("a") is not None
    text, _ = run(state, ["remove", "b"], ["n"])
    assert "cancelled" in text and state.db.get_credential("b") is not None


def test_failed_replacement_prevents_removal(state):
    second(state, token="secret-rejected")
    text, _ = run(state, ["remove", "a"], selected="work (b)")
    assert "not removed" in text
    assert state.db.get_default_credential("sf").id == "a"


def test_remove_current_explicitly_switches_to_environment(state, monkeypatch):
    monkeypatch.setenv(state.sf.api_key_env, "secret-env")
    text, _ = run(state, ["remove", "a"], ["y"], selected=f"Use environment variable {state.sf.api_key_env}")
    assert "Environment variable remains" in text
    assert state.ctx.provider.api_key == "secret-env"
    assert state.ctx.provider.credential_source == "environment"


def test_remove_last_key_disables_live_requests(state):
    text, prompt = run(state, ["remove", "a"], ["y"])
    assert "disabled" in prompt.call_args.args[0]
    assert state.db.get_credential("a") is None
    assert state.ctx.provider.api_key is None
    assert CredentialService(state.db).resolve(state.sf).token is None


def test_reset_replaces_selected_default_not_another_named_key(state):
    second(state)
    state.db.set_default_credential("b")
    run(state, ["reset"], ["secret-replacement"], selected="SiliconFlow")
    assert state.db.get_credential("b").access_token == "secret-replacement"
    assert state.db.get_credential("a").access_token == "secret-first"
    assert len(state.db.list_credentials("sf")) == 2


def test_no_default_does_not_silently_select_a_named_key(state):
    with state.db.transaction() as connection:
        connection.execute("UPDATE provider_credentials SET is_default = 0")
    assert state.db.get_default_credential("sf") is None
    assert CredentialService(state.db).resolve(state.sf).source == "none"


def test_replacement_from_different_provider_rolls_back(state):
    second(state, provider="other")
    with pytest.raises(ValueError):
        state.db.remove_credential("a", "b", clear_default=True)
    assert state.db.get_default_credential("sf").id == "a"
    assert state.db.get_credential("a") is not None


def test_stale_runtime_verification_does_not_overwrite_new_token_status(state):
    service = CredentialService(state.db)
    service.stamp(state.ctx.provider, CredentialResolution("secret-first", "database", "a"))
    state.db.upsert_credential(replace(state.first, access_token="secret-replacement"))
    run(state, ["test"])
    assert state.db.get_credential_check("a") is None


def test_resolution_repr_does_not_expose_tokens():
    assert "secret" not in repr(CredentialResolution("secret", "environment"))


def test_v5_migration_preserves_keys_and_adds_verification_storage(tmp_path):
    path = tmp_path / "credentials.db"
    database = AppDatabase(path)
    profile = ProviderProfile("sf", "SiliconFlow", None, "KEYS_TEST_SF_TOKEN")
    database.upsert_provider(profile)
    credential = ProviderCredential("a", "sf", "personal", "secret-first", is_default=True)
    database.upsert_credential(credential)
    with database.transaction() as connection:
        connection.execute("DROP TABLE credential_checks")
        connection.execute("PRAGMA user_version = 5")
    database.close()
    upgraded = AppDatabase(path)
    try:
        assert upgraded.get_default_credential("sf") == credential
        assert upgraded.connection.execute("PRAGMA user_version").fetchone()[0] == 6
        upgraded.record_credential_check("a", "ok")
        assert upgraded.get_credential_check("a")["status"] == "ok"
    finally:
        upgraded.close()


def test_default_and_verification_survive_database_reopen(tmp_path):
    path = tmp_path / "credentials.db"
    database = AppDatabase(path)
    database.upsert_provider(ProviderProfile("sf", "SiliconFlow", None, None))
    database.upsert_credential(ProviderCredential("a", "sf", "one", "secret-one", is_default=True))
    database.upsert_credential(ProviderCredential("b", "sf", "two", "secret-two"))
    database.set_default_credential("b")
    database.record_credential_check("b", "ok")
    database.close()
    restored = AppDatabase(path)
    try:
        assert restored.get_default_credential("sf").id == "b"
        assert restored.get_credential_check("b")["status"] == "ok"
    finally:
        restored.close()


def test_changing_token_clears_previous_verification_and_deleting_cascades(state):
    state.db.record_credential_check("a", "ok")
    state.db.upsert_credential(replace(state.first, access_token="secret-replacement"))
    assert state.db.get_credential_check("a") is None
    state.db.record_credential_check("a", "ok")
    state.db.remove_credential("a")
    assert state.db.get_credential_check("a") is None


def test_source_reports_changed_environment_without_exposing_either_token(state, monkeypatch):
    monkeypatch.setenv(state.sf.api_key_env, "secret-env-old")
    run(state, ["use", "env"])
    monkeypatch.setenv(state.sf.api_key_env, "secret-env-new")
    text, _ = run(state, ["source"])
    assert "differs from current environment" in text
    assert "secret-env-old" not in text and "secret-env-new" not in text


def test_reset_selects_provider_before_requesting_key(state):
    with (
        patch("src.utils.slash_commands.commands.keys.select_menu", return_value="Other Provider") as menu,
        patch("src.utils.slash_commands.commands.keys.prompt", return_value="secret-other") as prompt,
    ):
        def select_provider(*args):
            prompt.assert_not_called()
            return "Other Provider"

        menu.side_effect = select_provider
        result = KeysCommand().execute(["reset"], state.ctx)
    menu.assert_called_once_with("Select provider", ["Other Provider", "SiliconFlow"], "SiliconFlow")
    prompt.assert_called_once_with("Enter API key for Other Provider: ", is_password=True)
    assert "updated for Other Provider" in result.message
    assert state.db.get_default_credential("other").access_token == "secret-other"
    assert state.db.get_credential("a").access_token == "secret-first"
    assert state.ctx.session.provider_id == "sf"
    state.ctx.on_provider_changed.assert_not_called()


def test_cancel_reset_provider_selection_does_not_prompt_or_mutate(state):
    text, prompt = run(state, ["reset"], selected=None)
    assert "cancelled" in text
    prompt.assert_not_called()
    assert FakeProvider.calls == []
    assert state.db.get_credential("a").access_token == "secret-first"
    assert state.db.list_credentials("other") == []


def test_reset_with_explicit_provider_skips_selection(state):
    with patch("src.utils.slash_commands.commands.keys.select_menu") as menu:
        with patch("src.utils.slash_commands.commands.keys.prompt", return_value="secret-other"):
            result = KeysCommand().execute(["reset", "other"], state.ctx)
    menu.assert_not_called()
    assert "updated for Other Provider" in result.message


@pytest.mark.parametrize("args", [[], ["list"], ["add"], ["source"], ["reset"], ["use", "env"], ["test"]])
def test_provider_menu_cancel_stops_all_provider_operations(state, args):
    text, prompt = run(state, args, selected=None)
    assert "cancelled" in text
    prompt.assert_not_called()
    assert FakeProvider.calls == []
    assert state.db.get_credential("a").access_token == "secret-first"


@pytest.mark.parametrize("args,expected", [(["list"], "API keys — Other Provider"), (["source"], "Provider: Other Provider")])
def test_provider_menu_reads_selected_provider(state, args, expected):
    text, _ = run(state, args, selected="Other Provider")
    assert expected in text


def test_add_provider_menu_saves_to_selected_provider(state):
    text, _ = run(state, ["add"], ["work", "secret-other", "n"], selected="Other Provider")
    assert "added for Other Provider" in text
    assert state.db.list_credentials("other")[0].access_token == "secret-other"
    assert state.ctx.session.provider_id == "sf"


def test_environment_provider_menu_uses_selected_provider(state, monkeypatch):
    monkeypatch.setenv(state.other.api_key_env, "secret-env")
    text, _ = run(state, ["use", "env"], selected="Other Provider")
    assert "selected for Other Provider" in text
    assert FakeProvider.calls == ["secret-env"]
    assert state.ctx.provider.api_key == "secret-first"
