import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from main import prepare_user_input
from src.infrastructure.database import AppDatabase, ProviderProfile
from src.runtime.session import Session
from src.utils.slash_commands.base import CommandContext
from src.utils.slash_commands.commands.model import ModelCommand
from src.utils.slash_commands.factory import create_command_registry


class PrepareUserInputTests(unittest.TestCase):
    def test_command_at_start_is_dispatched(self):
        query, is_command = prepare_user_input("/clear")

        self.assertEqual(query, "/clear")
        self.assertTrue(is_command)

    def test_command_after_whitespace_is_preserved_for_llm(self):
        query, is_command = prepare_user_input(" /clear")

        self.assertEqual(query, " /clear")
        self.assertFalse(is_command)

    def test_command_after_other_text_is_preserved_for_llm(self):
        query, is_command = prepare_user_input("please run /clear")

        self.assertEqual(query, "please run /clear")
        self.assertFalse(is_command)

    def test_double_slash_escapes_a_command(self):
        query, is_command = prepare_user_input("//clear")

        self.assertEqual(query, "/clear")
        self.assertFalse(is_command)


class HelpCommandTests(unittest.TestCase):
    @patch("src.utils.slash_commands.commands.help.print_formatted_text")
    def test_help_uses_prompt_toolkit_and_lists_every_command(self, display):
        registry = create_command_registry()

        result = registry.dispatch("/help", object())

        self.assertIsNone(result.message)
        display.assert_called_once()
        help_text = display.call_args.args[0]
        for usage in (
            "/help",
            "/clear",
            "/compact",
            "/debug",
            "/model",
            "/memory",
            "/status",
        ):
            self.assertIn(usage, help_text)

    @patch("src.utils.slash_commands.commands.help.print_formatted_text")
    def test_help_usage_error_also_uses_prompt_toolkit(self, display):
        registry = create_command_registry()

        result = registry.dispatch("/help extra", object())

        self.assertIsNone(result.message)
        display.assert_called_once_with("Usage: /help")


class SlashCommandCompletionTests(unittest.TestCase):
    def test_slash_opens_all_available_commands(self):
        registry = create_command_registry()

        completions = list(
            registry.completer().get_completions(
                Document("/"),
                CompleteEvent(),
            )
        )

        names = [completion.text for completion in completions]
        self.assertEqual(
            names,
            [
                "/help",
                "/clear",
                "/compact",
                "/debug",
                "/model",
                "/memory",
                "/sessions",
                "/status",
                "/team",
                "/resume",
                "/new",
            ],
        )

    def test_partial_slash_command_is_filtered(self):
        registry = create_command_registry()

        completions = list(
            registry.completer().get_completions(
                Document("/mo"),
                CompleteEvent(),
            )
        )

        self.assertEqual([completion.text for completion in completions], ["/model"])

    def test_memory_command_toggles_session_setting(self):
        session = Session(model="test-model")
        context = CommandContext(
            session=session,
            toolset=object(),
            provider=object(),
        )
        registry = create_command_registry()

        self.assertTrue(session.memory_enabled)
        self.assertEqual(
            registry.dispatch("/memory off", context).message,
            "Memory disabled for this session.",
        )
        self.assertFalse(session.memory_enabled)
        self.assertEqual(
            registry.dispatch("/memory status", context).message,
            "Memory is off for this session.",
        )
        self.assertEqual(
            registry.dispatch("/memory on", context).message,
            "Memory enabled for this session.",
        )
        self.assertTrue(session.memory_enabled)

    def test_completion_is_disabled_after_the_first_token(self):
        registry = create_command_registry()

        completions = list(
            registry.completer().get_completions(
                Document("/model "),
                CompleteEvent(),
            )
        )

        self.assertEqual(completions, [])


class ModelCommandTests(unittest.TestCase):
    def make_database(self, tmp_path: Path) -> AppDatabase:
        database = AppDatabase(tmp_path / "state.db")
        database.upsert_provider(
            ProviderProfile(
                id="siliconflow",
                name="SiliconFlow",
                base_url="https://api.siliconflow.cn/v1",
                api_key_env="SILICONFLOW_API_KEY",
            )
        )
        database.upsert_provider(
            ProviderProfile(
                id="openai",
                name="OpenAI",
                base_url="https://api.openai.com/v1",
                api_key_env="OPENAI_API_KEY",
            )
        )
        return database

    def test_selects_provider_then_prompts_for_key_then_model(self):
        with TemporaryDirectory() as directory:
            self._test_selects_provider_then_prompts_for_key_then_model(
                Path(directory)
            )

    def _test_selects_provider_then_prompts_for_key_then_model(
        self,
        tmp_path: Path,
    ):
        database = self.make_database(tmp_path)

        class MutableProvider:
            provider_name = "SiliconFlow"

            def __init__(self):
                self.updated = None

            def update_from(self, provider):
                self.updated = provider

        current_provider = MutableProvider()
        candidate = SimpleNamespace(
            list_model_ids=lambda: ["gpt-test"],
        )
        session = Session(model="old-model", provider_id="siliconflow")
        context = CommandContext(
            session=session,
            toolset=object(),
            provider=current_provider,
            database=database,
        )

        with (
            patch(
                "src.utils.slash_commands.commands.model.Provider",
                return_value=candidate,
            ),
            patch(
                "src.utils.slash_commands.commands.model.prompt",
                side_effect=["OpenAI", "openai-secret", "gpt-test"],
            ),
        ):
            result = ModelCommand().execute([], context)

        self.assertEqual(result.message, "Model switched to: gpt-test")
        self.assertIs(current_provider.updated, candidate)
        self.assertEqual(session.provider_id, "openai")
        self.assertEqual(session.model, "gpt-test")
        credential = database.get_default_credential("openai")
        self.assertIsNotNone(credential)
        self.assertEqual(credential.access_token, "openai-secret")
        self.assertEqual(database.get_default_credential("siliconflow"), None)
        database.close()

    def test_reuses_existing_key_without_prompting_for_it(self):
        with TemporaryDirectory() as directory:
            self._test_reuses_existing_key_without_prompting_for_it(
                Path(directory)
            )

    def _test_reuses_existing_key_without_prompting_for_it(
        self,
        tmp_path: Path,
    ):
        database = self.make_database(tmp_path)
        from src.infrastructure.database import ProviderCredential

        database.upsert_credential(
            ProviderCredential(
                id="openai-default",
                provider_id="openai",
                name="default",
                access_token="stored-secret",
                is_default=True,
            )
        )

        candidate = SimpleNamespace(
            list_model_ids=lambda: ["gpt-test"],
        )
        session = Session(model="old-model", provider_id="siliconflow")
        context = CommandContext(
            session=session,
            toolset=object(),
            provider=object(),
            database=database,
        )
        with (
            patch(
                "src.utils.slash_commands.commands.model.Provider",
                return_value=candidate,
            ),
            patch(
                "src.utils.slash_commands.commands.model.prompt",
                side_effect=["OpenAI", "gpt-test"],
            ) as prompt_mock,
        ):
            result = ModelCommand().execute([], context)

        self.assertEqual(result.message, "Model switched to: gpt-test")
        self.assertEqual(prompt_mock.call_count, 2)
        self.assertEqual(
            database.get_default_credential("openai").access_token,
            "stored-secret",
        )
        database.close()


if __name__ == "__main__":
    unittest.main()
