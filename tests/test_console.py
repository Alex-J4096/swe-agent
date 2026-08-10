from io import StringIO

from src.ui.console import ConsoleRenderer, format_tool_preview


def make_renderer() -> tuple[ConsoleRenderer, StringIO]:
    output = StringIO()
    return ConsoleRenderer(output=output, use_color=False), output


def test_tool_preview_is_compact_and_truncated() -> None:
    preview = format_tool_preview('{"command":"rg \\"auth\\" src"}', 12)

    assert preview.endswith("…")
    assert len(preview) == 12


def test_renderer_keeps_tool_lifecycle_in_one_visual_language() -> None:
    renderer, output = make_renderer()

    renderer.show_tool_request("run_bash", '{"command":"pwd"}')
    renderer.show_tool_result("run_bash", {"ok": True})

    rendered = output.getvalue()
    assert "├─ run_bash" in rendered
    assert "└─ ✓ run_bash completed" in rendered


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
