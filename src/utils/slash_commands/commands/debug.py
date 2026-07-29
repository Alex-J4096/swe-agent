import json
from typing import Any

from src.utils.slash_commands.base import BaseCommand, CommandContext, CommandResult


class DebugCommand(BaseCommand):
    name = "debug"
    description = "Show formatted session history"

    def execute(self, args: list[str], context: CommandContext) -> CommandResult:
        if args:
            return CommandResult("Usage: /debug")

        history = context.session.history
        if not history:
            return CommandResult("Session history is empty.")

        messages = [
            self._format_message(index, message)
            for index, message in enumerate(history, start=1)
        ]
        noun = "message" if len(history) == 1 else "messages"
        header = f"Session history ({len(history)} {noun})"
        return CommandResult("\n\n".join([header, *messages]))

    def _format_message(self, index: int, message: dict[str, Any]) -> str:
        role = str(message.get("role", "unknown")).upper()
        lines = [f"[{index}] {role}"]

        for key, value in message.items():
            if key == "role":
                continue

            lines.append(f"  {key}:")
            lines.extend(self._format_value(value, indent=4))

        return "\n".join(lines)

    def _format_value(self, value: Any, indent: int) -> list[str]:
        prefix = " " * indent

        if isinstance(value, str):
            text = self._prettify_json_string(value)
        elif value is None:
            text = "null"
        elif isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, indent=2)
        else:
            text = str(value)

        return [f"{prefix}{line}" for line in text.splitlines() or [""]]

    def _prettify_json_string(self, value: str) -> str:
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value

        if not isinstance(parsed, (dict, list)):
            return value

        return json.dumps(parsed, ensure_ascii=False, indent=2)
