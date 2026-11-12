"""A small, centralized renderer for the line-oriented CLI.

The renderer deliberately owns presentation only. Agent and tool code can keep
returning structured data while this module decides how that data appears in a
TTY or in a plain-text log.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

if TYPE_CHECKING:
    from src.runtime.approval import ApprovalRequestRecord

from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import ANSI


ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

# Terminal width with a sane fallback.
def _term_width() -> int:
    try:
        return os.get_terminal_size().columns
    except (AttributeError, ValueError, OSError):
        return 80


@dataclass(frozen=True)
class Theme:
    """Semantic terminal colors used by the renderer."""

    # Primary palette — inspired by modern CLI agents.
    accent: str = "\033[38;5;75m"       # Soft blue
    accent_bold: str = "\033[1;38;5;75m"
    muted: str = "\033[38;5;243m"       # Neutral gray
    dim: str = "\033[2m"                # Dimmed text
    success: str = "\033[38;5;114m"     # Green
    warning: str = "\033[38;5;221m"     # Yellow/amber
    error: str = "\033[38;5;204m"       # Rose/red
    border: str = "\033[38;5;240m"      # Dark border
    info: str = "\033[38;5;117m"        # Light cyan
    highlight: str = "\033[38;5;183m"   # Soft purple
    white: str = "\033[97m"             # Bright white
    bold: str = "\033[1m"
    reset: str = "\033[0m"

    # Gradient stops for the banner (blue → purple → pink).
    banner_gradient: tuple[str, ...] = (
        "\033[38;5;75m",
        "\033[38;5;111m",
        "\033[38;5;147m",
        "\033[38;5;183m",
        "\033[38;5;218m",
    )


# ── Icons ────────────────────────────────────────────────────────────────────

ICON_TOOL = "▶"
ICON_SUCCESS = "✔"
ICON_FAIL = "✘"
ICON_AGENT = "◆"
ICON_USER = "❯"
ICON_NOTICE = "●"
ICON_WARN = "⚠"
ICON_ERROR = "✘"
ICON_KEY = "🔑"
ICON_THINKING = "◇"


class ThinkingIndicator:
    """Animate a single status line while a model request is in flight."""

    FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧")

    # The dots cycle through a subtle gradient.
    GRADIENT = (
        "\033[38;5;75m",
        "\033[38;5;111m",
        "\033[38;5;147m",
        "\033[38;5;183m",
        "\033[38;5;147m",
        "\033[38;5;111m",
    )

    def __init__(
        self,
        output: TextIO,
        *,
        enabled: bool,
        interval: float = 0.08,
    ) -> None:
        self.output = output
        self.enabled = enabled
        self.interval = interval
        self._stop = threading.Event()
        self._message = "Thinking"
        self._message_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._start_time: float | None = None
        self.stop_confirmation = threading.Event()

    def start(self, message: str = "Thinking") -> None:
        """Start the animation if output is an interactive terminal."""
        if not self.enabled:
            return

        with self._message_lock:
            self._message = message

        if self.is_running:
            return

        self._stop.clear()
        self._start_time = time.monotonic()
        self._thread = threading.Thread(
            target=self._run,
            name="thinking-indicator",
            daemon=True,
        )
        self._thread.start()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        reset = "\033[0m"
        for idx, frame in enumerate(itertools.cycle(self.FRAMES)):
            if self._stop.is_set():
                break

            color = self.GRADIENT[idx % len(self.GRADIENT)]
            with self._message_lock:
                message = self._message
            if self.stop_confirmation.is_set():
                message = "Press Esc again to stop · Ctrl+C stops immediately"

            elapsed = ""
            if self._start_time is not None:
                secs = time.monotonic() - self._start_time
                if secs >= 1.0:
                    elapsed = f" \033[38;5;243m{secs:.0f}s{reset}"

            self.output.write(
                f"\r\033[2K  {color}{frame}{reset}"
                f" \033[38;5;243m{message}{reset}{elapsed}"
            )
            self.output.flush()
            if self._stop.wait(self.interval):
                break

    def stop(self) -> None:
        """Stop the animation and erase its line, including on exceptions."""
        thread = self._thread
        if thread is None:
            return

        self._stop.set()
        thread.join(timeout=max(0.3, self.interval * 4))
        self.output.write("\r\033[2K")
        self.output.flush()
        self._thread = None
        self._start_time = None


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
        self.thinking = ThinkingIndicator(
            self.output,
            enabled=self.output.isatty(),
        )

    def start_thinking(self, message: str = "Thinking") -> None:
        self.thinking.start(message)

    def stop_thinking(self) -> None:
        self.thinking.stop()

    def show_stop_hint(self) -> None:
        self.thinking.stop_confirmation.set()
        if not self.thinking.is_running:
            self.show_notice("Press Esc again to stop this turn; Ctrl+C stops immediately.")

    def clear_stop_hint(self) -> None:
        self.thinking.stop_confirmation.clear()

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

    def _separator(self, char: str = "─", width: int | None = None) -> None:
        """Print a subtle full-width rule."""
        w = min(width or _term_width(), _term_width())
        self._write(self._paint(char * w, self.theme.border))

    # ── Welcome ──────────────────────────────────────────────────────────

    def show_welcome(self, rendered_panel: str) -> None:
        """Show the startup panel with clean semantic styling."""
        lines = [self._safe(line) for line in rendered_panel.splitlines()]
        if not lines:
            return

        for line in lines:
            stripped = line.strip()
            if not stripped:
                self._write()
                continue

            # Boxed panel top/bottom borders (e.g. ╭───╮, ╰───╯, ┏━━━┓, ┗━━━┛)
            if stripped.startswith(("╭", "╰", "┌", "└", "┏", "┗")) and stripped.endswith(("╮", "╯", "┐", "┘", "┓", "┛")):
                self._write(self._paint(line, self.theme.border))
                continue

            # Boxed panel side borders (e.g. │ content │)
            if stripped.startswith("│") and stripped.endswith("│") and len(stripped) >= 2:
                leading = line[:len(line) - len(line.lstrip())]
                content = stripped[1:-1]
                b = self._paint("│", self.theme.border)
                self._write(f"{leading}{b}{content}{b}")
                continue

            # Horizontal dividers (e.g. ─────── or ━━━━━━━)
            if all(ch in "─━-=" for ch in stripped):
                self._write(self._paint(line, self.theme.border))
                continue

            # Title line with icon (e.g. "  ◆ SWE CLI")
            if stripped.startswith("◆"):
                parts = stripped.split(maxsplit=1)
                icon = self._paint(parts[0], self.theme.accent)
                title = self._paint(parts[1], self.theme.accent_bold) if len(parts) > 1 else ""
                leading = line[:len(line) - len(line.lstrip())]
                self._write(f"{leading}{icon} {title}")
                continue

            # Help command hint (e.g. "  Type /help for available commands.")
            if "/help" in stripped:
                leading = line[:len(line) - len(line.lstrip())]
                before, after = stripped.split("/help", 1)
                cmd = self._paint("/help", self.theme.accent)
                before_text = self._paint(before, self.theme.muted)
                after_text = self._paint(after, self.theme.muted)
                self._write(f"{leading}{before_text}{cmd}{after_text}")
                continue

            # Key-value metadata lines (e.g. "  dir       ~/dev/swe-agent")
            match = re.match(r"^(\s+)([a-zA-Z0-9_-]+)(\s{2,})(.*)$", line)
            if match:
                indent, key, spaces, value = match.groups()
                styled_key = self._paint(key, self.theme.muted)
                self._write(f"{indent}{styled_key}{spaces}{value}")
                continue

            self._write(line)

        self._write()

    # ── Tool lifecycle ───────────────────────────────────────────────────

    def show_tool_request(self, name: str, preview: str = "") -> None:
        suffix = f"  {self._paint(self._safe(preview), self.theme.muted)}" if preview else ""
        icon = self._paint(ICON_TOOL, self.theme.accent)
        name_label = self._paint(name, self.theme.accent_bold)
        tree = self._paint("  ├─", self.theme.border)
        self._write(f"{tree} {icon} {name_label}{suffix}")

    def show_tool_result(
        self,
        name: str,
        output: dict[str, Any],
        *,
        show_content: bool = False,
    ) -> None:
        """Show a compact result line and optionally a tool-owned content block."""
        ok = bool(output.get("ok"))
        marker = ICON_SUCCESS if ok else ICON_FAIL
        label = "done" if ok else "failed"
        color = self.theme.success if ok else self.theme.error
        tree = self._paint("  └─", self.theme.border)
        self._write(
            f"{tree} {self._paint(marker, color)} "
            f"{self._paint(name, self.theme.muted)} "
            f"{self._paint(label, color)}"
        )

        if name == "propose_team_plan" and ok:
            self.show_team_plan(output.get("plan"))
            return

        error = output.get("error") or output.get("stderr")
        if not ok and error:
            err_text = self._safe(error)
            self._write(
                f"       {self._paint(ICON_ERROR, self.theme.error)} "
                f"{self._paint(err_text, self.theme.error)}"
            )

        if show_content:
            content = output.get("content")
            if isinstance(content, str) and content:
                for line in self._safe(content).splitlines():
                    self._write(f"       {self._paint(line, self.theme.muted)}")

    # ── Team plan ────────────────────────────────────────────────────────

    def show_team_plan(self, plan: object) -> None:
        """Render the complete proposed plan at the approval boundary."""
        if not isinstance(plan, dict):
            return
        plan_id = self._safe(plan.get("plan_id", "unknown"))
        goal = self._safe(plan.get("goal", ""))
        status = self._safe(plan.get("status", "awaiting_approval"))

        self._write()
        self._write(
            f"  {self._paint('Plan', self.theme.accent_bold)}  "
            f"{self._paint(plan_id, self.theme.highlight)}"
        )
        self._write(
            f"  {self._paint('Status:', self.theme.muted)} {status}"
        )
        self._write(
            f"  {self._paint('Goal:', self.theme.muted)} {goal}"
        )

        tasks = plan.get("tasks", [])
        if not isinstance(tasks, list):
            tasks = []
        self._write(
            f"  {self._paint(f'Tasks ({len(tasks)}):', self.theme.muted)}"
        )
        for index, task in enumerate(tasks, start=1):
            if not isinstance(task, dict):
                continue
            key = self._safe(task.get("key", ""))
            subject = self._safe(task.get("subject", ""))
            description = self._safe(task.get("description", ""))
            depends_on = task.get("depends_on", [])
            if not isinstance(depends_on, list):
                depends_on = []
            owner_hint = self._safe(task.get("owner_hint") or "auto")
            workspace = self._safe(task.get("workspace_mode") or "repo")

            self._write(
                f"    {self._paint(f'{index}.', self.theme.accent)} "
                f"{self._paint(f'[{key}]', self.theme.highlight)} {subject}"
            )
            if description:
                self._write(
                    f"       {self._paint(description, self.theme.muted)}"
                )
            deps_text = (
                ", ".join(self._safe(item) for item in depends_on) or "none"
            )
            self._write(
                f"       {self._paint('depends:', self.theme.muted)} {deps_text}  "
                f"{self._paint('owner:', self.theme.muted)} {owner_hint}  "
                f"{self._paint('workspace:', self.theme.muted)} {workspace}"
            )
        self._write()
        self._write(
            f"  {self._paint('→', self.theme.accent)} "
            f"Approve with: {self._paint(f'/team approve {plan_id}', self.theme.accent_bold)}"
        )

    # ── Messages ─────────────────────────────────────────────────────────

    def show_assistant(self, content: str) -> None:
        """Render a model response with a clean left-border rail."""
        safe_content = self._safe(content).strip()
        if not safe_content:
            return

        icon = self._paint(ICON_AGENT, self.theme.accent)
        label = self._paint("assistant", self.theme.accent_bold)
        self._write(f"  {icon} {label}")
        self._write()
        for line in safe_content.splitlines():
            bar = self._paint("  │", self.theme.border)
            self._write(f"{bar}  {line}")
        self._write()

    def show_user(self, content: str) -> None:
        """Render a restored user message in the same language as the prompt."""
        safe_content = self._safe(content).strip()
        if not safe_content:
            return

        icon = self._paint(ICON_USER, self.theme.highlight)
        for line in safe_content.splitlines():
            self._write(f"  {icon} {line}")
        self._write()

    # ── History replay ───────────────────────────────────────────────────

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

    # ── Notices & logs ───────────────────────────────────────────────────

    def show_notice(self, message: str) -> None:
        icon = self._paint(ICON_NOTICE, self.theme.info)
        self._write(f"  {icon} {self._safe(message)}")

    def show_error(self, message: str, module: str | None = None) -> None:
        prefix = f"{module}: " if module else ""
        icon = self._paint(ICON_ERROR, self.theme.error)
        self._write(
            f"  {icon} {self._paint(self._safe(prefix + message), self.theme.error)}"
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
            "INFO": self.theme.info,
            "WARNING": self.theme.warning,
            "ERROR": self.theme.error,
        }.get(level, self.theme.muted)
        location = f" {module}" if module else ""
        tag = self._paint(f"[{level.lower()}]", color)
        loc = self._paint(location, self.theme.muted)
        self._write(f"  {tag}{loc} {self._safe(message)}")

    # ── Permissions ──────────────────────────────────────────────────────

    def permission_request(self, request: ApprovalRequestRecord) -> None:
        self._write("\n  Approval required")
        capabilities = request.capabilities or ((request.capability,) if request.capability else ())
        for label, value in (
            ("Source", request.source.agent_id or request.source.kind),
            ("Task", request.source.task_id or "foreground"),
            ("Tool", request.tool_name),
            ("Command / arguments", request.preview),
            ("Directory", request.cwd or "."),
            ("Reason", request.reason),
            ("Risk", request.risk),
            ("Required permissions", ", ".join(capabilities)),
            ("Reuse", "Exact operation + directory + permissions; until session is unloaded" if request.reusable else "Once only; no reusable grant"),
        ):
            self._write(f"    {label}: {self._safe(value)}")
        if request.tool_name == "run_bash":
            self._write("    Execution: filesystem sandbox remains enforced.")

    def permission_question(self, tool_name: str, *, reusable: bool = True) -> str:
        question = (
            f"  {ICON_WARN} Allow {tool_name}?\n"
            + "    [y/1] once"
            + (" / [t] turn / [a/2] session (exact operation)" if reusable else "")
            + "\n    [n/3] deny and adjust / [s/Esc] stop task\n"
            + ("  Press a key (no Enter needed): "
               if self.output.isatty() and sys.stdin.isatty()
               else "  Enter choice and press Enter: ")
        )
        return self._paint(question, self.theme.warning)

    def team_plan_continue_question(self) -> str:
        question = (
            f"  {ICON_NOTICE} Continue Agent Loop? "
            "[y] continue / [n] return to prompt "
        )
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
        marker = ICON_SUCCESS if allowed else ICON_FAIL
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
