import unittest
from unittest.mock import patch

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from main import prepare_user_input
from src.utils.slash_commands.factory import create_command_registry
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
                "/sessions",
                "/status",
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

    def test_completion_is_disabled_after_the_first_token(self):
        registry = create_command_registry()

        completions = list(
            registry.completer().get_completions(
                Document("/model "),
                CompleteEvent(),
            )
        )

        self.assertEqual(completions, [])


if __name__ == "__main__":
    unittest.main()
