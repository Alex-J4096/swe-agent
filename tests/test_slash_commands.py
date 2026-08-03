import unittest
from unittest.mock import patch

from main import prepare_user_input
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
        for usage in ("/help", "/clear", "/compact", "/debug", "/model"):
            self.assertIn(usage, help_text)

    @patch("src.utils.slash_commands.commands.help.print_formatted_text")
    def test_help_usage_error_also_uses_prompt_toolkit(self, display):
        registry = create_command_registry()

        result = registry.dispatch("/help extra", object())

        self.assertIsNone(result.message)
        display.assert_called_once_with("Usage: /help")


if __name__ == "__main__":
    unittest.main()
