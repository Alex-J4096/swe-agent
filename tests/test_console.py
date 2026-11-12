from io import StringIO
import time
from threading import Event

from src.ui.console import ConsoleRenderer, format_tool_preview


def make_renderer() -> tuple[ConsoleRenderer, StringIO]:
    output = StringIO()
    return ConsoleRenderer(output=output, use_color=False), output


def test_tool_preview_is_compact_and_truncated() -> None:
    preview = format_tool_preview('{"command":"rg \\"auth\\" src"}', 12)

    assert preview.endswith("…")
    assert len(preview) == 12


def test_thinking_indicator_is_disabled_for_non_tty_output() -> None:
    renderer, output = make_renderer()

    renderer.start_thinking()
    time.sleep(0.02)
    renderer.stop_thinking()

    assert output.getvalue() == ""


def test_stop_confirmation_hint_is_rendered_and_can_be_reset():
    shown = Event()

    class FakeTTY(StringIO):
        def isatty(self):
            return True

        def write(self, text):
            result = super().write(text)
            if "Press Esc again" in text:
                shown.set()
            return result

    output = FakeTTY()
    renderer = ConsoleRenderer(output=output, use_color=False)
    renderer.start_thinking()
    try:
        renderer.show_stop_hint()
        assert shown.wait(1)
        assert renderer.thinking.is_running  # First Esc only asks for confirmation.
        renderer.clear_stop_hint()
        assert not renderer.thinking.stop_confirmation.is_set()
    finally:
        renderer.stop_thinking()


def test_thinking_indicator_clears_its_line_when_stopped() -> None:
    class FakeTTY(StringIO):
        def isatty(self) -> bool:
            return True

    output = FakeTTY()
    renderer = ConsoleRenderer(output=output, use_color=False)
    renderer.thinking.interval = 0.01

    renderer.start_thinking()
    time.sleep(0.03)
    renderer.stop_thinking()

    rendered = output.getvalue()
    assert "Thinking" in rendered
    assert rendered.endswith("\r\033[2K")


def test_renderer_keeps_tool_lifecycle_in_one_visual_language() -> None:
    renderer, output = make_renderer()

    renderer.show_tool_request("run_bash", '{"command":"pwd"}')
    renderer.show_tool_result("run_bash", {"ok": True})

    rendered = output.getvalue()
    # New style uses ▶ icon and "done" instead of "completed".
    assert "├─" in rendered
    assert "run_bash" in rendered
    assert "└─" in rendered
    assert "done" in rendered


def test_renderer_shows_tool_failure_reason() -> None:
    renderer, output = make_renderer()

    renderer.show_tool_result(
        "run_bash",
        {"ok": False, "error": "Team plan must be approved first"},
    )

    rendered = output.getvalue()
    assert "Team plan must be approved first" in rendered
    assert "failed" in rendered


def test_renderer_prints_complete_team_plan_for_approval() -> None:
    renderer, output = make_renderer()

    renderer.show_tool_result(
        "propose_team_plan",
        {
            "ok": True,
            "plan": {
                "plan_id": "plan_demo",
                "status": "awaiting_approval",
                "goal": "Update the team docs",
                "tasks": [{
                    "key": "docs",
                    "subject": "Update docs",
                    "description": "Explain recovery",
                    "depends_on": [],
                    "owner_hint": "writer",
                    "workspace_mode": "worktree",
                }],
            },
        },
    )

    rendered = output.getvalue()
    assert "plan_demo" in rendered
    assert "Update the team docs" in rendered
    assert "[docs] Update docs" in rendered
    assert "worktree" in rendered
    assert "/team approve plan_demo" in rendered


def test_renderer_strips_control_sequences_from_model_content() -> None:
    renderer, output = make_renderer()

    renderer.show_assistant("hello\033[31m unsafe\033[0m")

    rendered = output.getvalue()
    assert "unsafe" in rendered
    assert "\033[31m" not in rendered
    # New style uses ◆ icon and "assistant" label.
    assert "assistant" in rendered
    assert "hello unsafe" in rendered
    assert "╮" not in rendered


def test_assistant_style_does_not_depend_on_content_width() -> None:
    renderer, output = make_renderer()
    content = "a" * 200

    renderer.show_assistant(content)

    rendered = output.getvalue()
    assert content in rendered
    assert "─" * 34 not in rendered
    assert "─" * 44 not in rendered


def test_renderer_formats_restored_history_as_conversation() -> None:
    renderer, output = make_renderer()

    renderer.show_history([
        {"role": "user", "content": "inspect files"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-1",
                "function": {"name": "run_bash", "arguments": '{"command":"pwd"}'},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": '{"ok":true,"content":"/workspace"}',
        },
        {"role": "assistant", "content": "Done."},
    ])

    rendered = output.getvalue()
    # New style uses ❯ for user messages instead of ›.
    assert "inspect files" in rendered
    assert "assistant" in rendered
    assert "run_bash" in rendered
    assert "done" in rendered
    assert "tool_calls" not in rendered
    assert "function" not in rendered


def test_show_welcome_does_not_recolor_trailing_characters() -> None:
    lines_written: list[str] = []
    renderer = ConsoleRenderer(use_color=True)
    renderer._write = lambda text="": lines_written.append(text)

    panel = (
        "\n"
        "  ◆ SWE CLI\n"
        "  ────────────────────────────────────────────────\n"
        "  dir       ~/dev/swe-agent\n"
        "  session   42749eaf\n"
        "  model     deepseek-ai/DeepSeek-V4-Flash\n"
        "  ────────────────────────────────────────────────\n"
        "  Type /help for available commands.\n"
    )
    renderer.show_welcome(panel)

    # In key-value lines, the value part must not end with an ANSI reset directly
    # after the last character, indicating the last char was recolored.
    dir_line = next(line for line in lines_written if "swe-agent" in line)
    assert dir_line.endswith("~/dev/swe-agent")

    model_line = next(line for line in lines_written if "DeepSeek-V4-Flash" in line)
    assert model_line.endswith("deepseek-ai/DeepSeek-V4-Flash")


def test_show_welcome_emits_no_ansi_when_color_disabled() -> None:
    renderer, output = make_renderer()

    panel = (
        "\n"
        "  ◆ SWE CLI\n"
        "  ────────────────────────────────────────────────\n"
        "  dir       ~/dev/swe-agent\n"
        "  model     deepseek-ai/DeepSeek-V4-Flash\n"
        "  ────────────────────────────────────────────────\n"
        "  Type /help for available commands.\n"
    )
    renderer.show_welcome(panel)

    rendered = output.getvalue()
    assert "\x1b" not in rendered
    assert "Type /help for available commands." in rendered
