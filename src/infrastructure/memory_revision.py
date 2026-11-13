"""Evidence-linked, optimistic revisions of existing project facts."""
from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from src.infrastructure.memory import MemoryStore, Memory


def evidence_kind(tool: str | None, result: Any) -> str | None:
    """Classify observations without confusing a failed command with no execution."""
    if not isinstance(result, dict):
        return None
    if any(result.get(key) for key in ("permission_denied", "error", "timed_out", "cancelled", "interrupted")):
        return None
    if tool == "run_bash":
        if result.get("ok") is True:
            if result.get("exit_code") is not None and (type(result["exit_code"]) is not int or result["exit_code"] != 0):
                return None
            return "command_succeeded"
        code = result.get("exit_code")
        # Our executors report timeout/unavailable as -1 and signals as negative.
        # 126/127 are shell invocation failures, not results of the requested program.
        if (result.get("ok") is False and type(code) is int and code > 0 and code not in {126, 127}
                and isinstance(result.get("stdout"), str) and isinstance(result.get("stderr"), str)):
            return "command_failed"
    elif tool in {"read_file", "edit_file", "write_file"} and result.get("ok") is True:
        return "file_observed" if tool == "read_file" else "file_changed"
    return None


def evidence_dialogue(
    messages: list[dict[str, Any]],
    max_chars: int = 12_000,
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Keep complete recent packets and pair tool outputs with their calls."""
    calls = {}
    packets = []
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                calls[call.get("id")] = call.get("function", {})
        if role != "tool":
            content = message.get("content")
            if content:
                packets.append((f"{role}: {content}", None))
            continue
        call_id = message.get("tool_call_id")
        function = calls.get(call_id)
        try:
            output = json.loads(message.get("content", ""))
            arguments = json.loads(function.get("arguments", "{}")) if function else {}
        except (ValueError, TypeError):
            output, arguments = message.get("content"), {}
        packet = {
            "evidence_id": call_id,
            "tool": function.get("name") if function else None,
            "arguments": arguments,
            "result": output,
        }
        packet["evidence_kind"] = evidence_kind(packet["tool"], output)
        text = "tool: " + json.dumps(packet, ensure_ascii=False)
        packets.append((text, packet))
    selected = []
    size = 0
    for text, packet in reversed(packets):
        if size + len(text) + 1 > max_chars:
            break
        selected.append((text, packet))
        size += len(text) + 1
    selected.reverse()
    evidence = {
        packet["evidence_id"]: packet
        for _, packet in selected
        if packet and packet["evidence_id"] and packet["evidence_kind"] is not None
    }
    return "\n".join(text for text, _ in selected), evidence


def revise_memories(
    store: MemoryStore,
    messages: list[dict[str, Any]],
    should_commit: Callable[[], bool] | None,
    session_id: str | None,
) -> list[Memory]:
    from src.runtime.memory_revision_agent import run_memory_revision

    return run_memory_revision(store, messages, should_commit, session_id)
