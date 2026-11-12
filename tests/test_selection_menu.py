from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
import pytest

from src.ui.selection_menu import select_menu


def choose(keys, values=None, current=None):
    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=DummyOutput()):
            pipe.send_text(keys)
            return select_menu("Select model", values or ["first", "second", "third"], current)


def test_arrow_keys_select_highlighted_item_without_typing():
    assert choose("\x1b[B\x1b[B\x1b[A\r") == "second"


def test_current_value_is_initially_highlighted():
    assert choose("\r", current="third") == "third"


def test_unavailable_current_value_defaults_to_first():
    assert choose("\r", current="removed") == "first"


@pytest.mark.parametrize("key", ["\x1b", "\x03", "\x04"])
def test_cancel_returns_no_selection(key):
    assert choose(key) is None


def test_large_menu_supports_paging_and_end():
    values = [f"model-{index}" for index in range(100)]
    assert choose("\x1b[6~\x1b[B\r", values) == "model-11"
    assert choose("\x1b[F\r", values) == "model-99"


def test_moving_up_stays_within_menu():
    assert choose("\x1b[A\r") == "first"


def test_empty_menu_does_not_open_terminal():
    assert select_menu("Empty", []) is None


def test_fuzzy_search_matches_nonconsecutive_letters_ignoring_case():
    assert choose("SLFL\r", ["OpenAI", "SiliconFlow", "OpenCode Go"]) == "SiliconFlow"


def test_arrow_navigation_and_search_work_together():
    assert choose("gpt\x1b[B\r", ["claude", "gpt-alpha", "gpt-beta"]) == "gpt-beta"


def test_backspace_updates_search_results():
    assert choose("gptx\x7f\x1b[B\r", ["claude", "gpt-alpha", "gpt-beta"]) == "gpt-beta"


def test_clear_search_restores_list_and_current_selection():
    assert choose("gpt\x15\r", ["claude", "gpt-alpha", "gpt-beta"], current="claude") == "claude"


def test_enter_on_empty_results_does_not_select_hidden_item():
    assert choose("zzzz\r\x15gpt\r", ["claude", "gpt-alpha"]) == "gpt-alpha"


def test_cancel_while_searching():
    assert choose("gpt\x1b", ["claude", "gpt-alpha"]) is None


def test_search_can_match_across_spaces_and_punctuation():
    assert choose("op go\r", ["OpenAI", "OpenCode Go"]) == "OpenCode Go"
    assert choose("ds/v4\r", ["gpt-alpha", "deepseek-ai/DeepSeek-V4-Flash"]) == "deepseek-ai/DeepSeek-V4-Flash"
