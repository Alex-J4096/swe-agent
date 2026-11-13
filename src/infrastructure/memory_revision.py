"""Evidence-linked, optimistic revisions of existing project facts."""
from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from src.infrastructure.memory import MemoryStore, Memory


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
        if packet and packet["evidence_id"] and packet["tool"] in {"read_file", "run_bash", "edit_file", "write_file"}
        and isinstance(packet["result"], dict) and packet["result"].get("ok") is True
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
