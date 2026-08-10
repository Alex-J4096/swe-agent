"""A small, centralized renderer for the line-oriented CLI.

The renderer deliberately owns presentation only. Agent and tool code can keep
returning structured data while this module decides how that data appears in a
TTY or in a plain-text log.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import ANSI


ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


@dataclass(frozen=True)
class Theme:
    """Semantic terminal colors used by the renderer."""

    accent: str = "\033[96m"
    muted: str = "\033[90m"
    success: str = "\033[92m"
    warning: str = "\033[93m"
    error: str = "\033[91m"
    border: str = "\033[94m"
    reset: str = "\033[0m"


class ConsoleRenderer:
    """Render user-facing terminal events with consistent hierarchy and tone."""

    def __init__(
        self,
        *,
        output: TextIO | None = None,
        use_color: bool | None = None,
        theme: Theme | None = None,
    ) -> None:
        self.output = output or sys.stdout
        self.use_color = (
            self.output.isatty() if use_color is None else use_color
        )
        self.theme = theme or Theme()

    def _safe(self, value: object) -> str:
        """Keep model/tool text from injecting terminal control sequences."""
        return ANSI_ESCAPE.sub("", str(value))

    def _paint(self, value: object, color: str) -> str:
        text = self._safe(value)
        if not self.use_color:
            return text
        return f"{color}{text}{self.theme.reset}"

    def _write(self, text: str = "") -> None:
        if self.use_color:
            print_formatted_text(ANSI(text), file=self.output)
        else:
            print(text, file=self.output)

    def show_welcome(self, rendered_panel: str) -> None:
        """Show the startup panel, coloring only its structural border."""
        lines = [self._safe(line) for line in rendered_panel.splitlines()]
        if not lines:
            return

        for index, line in enumerate(lines):
            if index in (0, len(lines) - 1):
                self._write(self._paint(line, self.theme.border))
                continue

            left, content, right = line[0], line[1:-1], line[-1]
            self._write(
                f"{self._paint(left, self.theme.border)}{content}"
                f"{self._paint(right, self.theme.border)}"
            )
        self._write()

    def show_tool_request(self, name: str, preview: str = "") -> None:
        suffix = f"  {self._safe(preview)}" if preview else ""
        self._write(
            f"  {self._paint('├─', self.theme.muted)} "
            f"{self._paint(name, self.theme.accent)}{suffix}"
        )

    def show_tool_result(
        self,
        name: str,
        output: dict[str, Any],
        *,
        show_content: bool = False,
    ) -> None:
        """Show a compact result line and optionally a tool-owned content block."""
        ok = bool(output.get("ok"))
        marker = "✓" if ok else "✗"
        label = "completed" if ok else "failed"
        color = self.theme.success if ok else self.theme.error
        self._write(
            f"  {self._paint('└─', self.theme.muted)} "
            f"{self._paint(marker, color)} {self._safe(name)} "
            f"{self._paint(label, color)}"
        )

        if show_content:
            content = output.get("content")
            if isinstance(content, str) and content:
                for line in self._safe(content).splitlines():
                    self._write(f"    {line}")

    def show_assistant(self, content: str) -> None:
        """Render a model response with a width-independent open rail."""
        safe_content = self._safe(content).strip()
        if not safe_content:
            return

        self._write(self._paint("╭─ assistant", self.theme.border))
        for line in safe_content.splitlines():
            self._write(f"{self._paint('│', self.theme.border)} {line}")
        self._write(self._paint("╰─", self.theme.border))
        self._write()

    def show_user(self, content: str) -> None:
        """Render a restored user message in the same language as the prompt."""
        safe_content = self._safe(content).strip()
        if not safe_content:
            return

        for line in safe_content.splitlines():
            self._write(f"{self._paint('›', self.theme.accent)} {line}")
        self._write()

    def show_history(
        self,
        history: list[dict[str, Any]],
        *,
        limit: int = 10,
    ) -> int:
        """Render recent history without exposing raw Chat Completions payloads."""
        if limit < 1:
            raise ValueError("limit must be at least 1")

        recent_history = history[-limit:]
        tool_names: dict[str, str] = {}
        for message in history:
            if message.get("role") != "assistant":
                continue
            for tool_call in message.get("tool_calls", []):
                if not isinstance(tool_call, dict):
                    continue
                call_id = tool_call.get("id")
                function = tool_call.get("function", {})
                if isinstance(function, dict):
                    tool_name = function.get("name")
                else:
                    tool_name = None
                if isinstance(call_id, str) and isinstance(tool_name, str):
                    tool_names[call_id] = tool_name

        for message in recent_history:
            role = message.get("role")
            if role == "user":
                self.show_user(str(message.get("content") or ""))
                continue

            if role == "assistant":
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    self.show_assistant(content)
                for tool_call in message.get("tool_calls", []):
                    if not isinstance(tool_call, dict):
                        continue
                    function = tool_call.get("function", {})
                    if not isinstance(function, dict):
                        continue
                    name = str(function.get("name") or "tool")
                    arguments = str(function.get("arguments") or "")
                    self.show_tool_request(name, format_tool_preview(arguments))
                continue

            if role == "tool":
                call_id = message.get("tool_call_id")
                name = tool_names.get(str(call_id), "tool")
                content = message.get("content")
                output: dict[str, Any]
                if isinstance(content, str):
                    try:
                        parsed = json.loads(content)
                    except (json.JSONDecodeError, TypeError):
                        parsed = None
                    output = parsed if isinstance(parsed, dict) else {"ok": True}
                else:
                    output = {"ok": True}
                self.show_tool_result(
                    name,
                    output,
                    show_content=name in {"todo_manager", "task_manager"},
                )
                continue

            content = message.get("content")
            if content:
                self.show_notice(str(content))

        return len(recent_history)

    def show_notice(self, message: str) -> None:
        self._write(f"{self._paint('•', self.theme.accent)} {self._safe(message)}")

    def show_error(self, message: str, module: str | None = None) -> None:
        prefix = f"{module}: " if module else ""
        self._write(
            f"{self._paint('✗', self.theme.error)} "
            f"{self._safe(prefix + message)}"
        )

    def show_log(
        self,
        level: str,
        message: str,
        module: str | None = None,
    ) -> None:
        level = level.upper()
        color = {
            "DEBUG": self.theme.muted,
            "INFO": self.theme.accent,
            "WARNING": self.theme.warning,
            "ERROR": self.theme.error,
        }.get(level, self.theme.muted)
        location = f" {module}" if module else ""
        self._write(
            f"{self._paint(f'[{level.lower()}]', color)}"
            f"{self._paint(location, self.theme.muted)} "
            f"{self._safe(message)}"
        )

    def permission_question(self, tool_name: str) -> str:
        question = f"  ? Allow {tool_name}? [y] once / [a] session / [n] deny "
        return self._paint(question, self.theme.warning)

    def permission_decision(
        self,
        tool_name: str,
        target: str,
        *,
        allowed: bool,
        scope: str | None = None,
        reason: str | None = None,
    ) -> None:
        marker = "✓" if allowed else "✗"
        decision = "Allowed" if allowed else "Denied"
        color = self.theme.success if allowed else self.theme.error
        suffix = f" ({reason})" if reason else ""
        scope_suffix = f" [{scope}]" if scope else ""
        self._write(
            f"  {self._paint(marker, color)} "
            f"{self._paint(decision, color)} {self._safe(tool_name)}: "
            f"{self._safe(target)}{scope_suffix}{suffix}"
        )

    def clear_input_line(self) -> None:
        """Remove the input prompt after a permission answer in an interactive TTY."""
        if self.use_color:
            print("\033[1A\r\033[2K", end="", file=self.output)


def format_tool_preview(arguments: str, max_length: int = 120) -> str:
    """Normalize a tool argument payload for a single-line display."""
    import json

    try:
        parsed = json.loads(arguments)
        preview = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    except (json.JSONDecodeError, TypeError):
        preview = " ".join(str(arguments).split())

    if preview == "{}":
        return ""
    if len(preview) > max_length:
        return f"{preview[:max_length - 1]}…"
    return preview
