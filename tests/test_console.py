from io import StringIO
import time

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
    assert "├─ run_bash" in rendered
    assert "└─ ✓ run_bash completed" in rendered


def test_renderer_shows_tool_failure_reason() -> None:
    renderer, output = make_renderer()

    renderer.show_tool_result(
        "run_bash",
        {"ok": False, "error": "Team plan must be approved first"},
    )

    assert "Error: Team plan must be approved first" in output.getvalue()


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
    assert "Plan ID: plan_demo" in rendered
    assert "Goal: Update the team docs" in rendered
    assert "[docs] Update docs" in rendered
    assert "Workspace: worktree" in rendered
    assert "Approve with: /team approve plan_demo" in rendered


def test_renderer_strips_control_sequences_from_model_content() -> None:
    renderer, output = make_renderer()

    renderer.show_assistant("hello\033[31m unsafe\033[0m")

    rendered = output.getvalue()
    assert "unsafe" in rendered
    assert "\033[31m" not in rendered
    assert "╭─ assistant" in rendered
    assert "│ hello unsafe" in rendered
    assert "╰─" in rendered
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
    assert "› inspect files" in rendered
    assert "╭─ assistant" in rendered
    assert "├─ run_bash  {\"command\":\"pwd\"}" in rendered
    assert "└─ ✓ run_bash completed" in rendered
    assert "tool_calls" not in rendered
    assert "function" not in rendered
