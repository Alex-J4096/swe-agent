import json
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from openai import OpenAI, OpenAIError
from openai.types.chat import (
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionUserMessageParam,
)

from src.infrastructure.retry import request_with_retry

# 保留最近三个工具调用的结果
KEEP_RECENT = 3
PRESERVE_RESULT_TOOLS = {
    "read_file"
}

TOOL_RESULT_MAX_CHARS = 4_000
TOOL_RESULT_ARCHIVE_DIR = Path(".swe-agent/tool-results")

TRANSCRIPT_DIR = Path(".swe-agent/transcripts")


@dataclass(frozen=True)
class CompactHistoryResult:
    ok: bool
    transcript_path: Path | None = None
    error: str | None = None


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    return len(str(messages)) // 4


def _message_has_tool_use(message: dict[str, Any]) -> bool:
    return (
        message.get("role") == "assistant" and bool(message.get("tool_calls"))
    )


def _is_tool_result_message(message: dict[str, Any]) -> bool:
    return (
        message.get("role") == "tool" and bool(message.get("tool_call_id"))
    )


def _message_groups(
    messages: list[dict[str, Any]],
) -> list[tuple[int, int]]:
    groups: list[tuple[int, int]] = []
    index = 0
    while index < len(messages):
        end = index + 1
        if _message_has_tool_use(messages[index]):
            while (
                end < len(messages)
                and _is_tool_result_message(messages[end])
            ):
                end += 1
        groups.append((index, end))
        index = end
    return groups


# L1 snip_compact 消息数量若大于50条，裁剪掉中间的仅保留收尾3条消息，不做摘要
def snip_compact(
    messages: list[dict[str, Any]],
    max_messages: int = 50,
) -> list[dict[str, Any]]:
    if max_messages < 3:
        raise ValueError("max_messages must be at least 3")
    if len(messages) <= max_messages:
        return messages

    groups = _message_groups(messages)
    head_end = 0
    for _, end in groups:
        # 为占位消息和至少一条尾部消息预留空间。
        if end + 2 > max_messages:
            break
        head_end = end
        if head_end >= 3:
            break

    tail_budget = max_messages - head_end - 1
    tail_start = len(messages)
    for start, _ in reversed(groups):
        if start < head_end:
            break
        if len(messages) - start > tail_budget:
            break
        tail_start = start

    if head_end >= tail_start:
        return messages

    snipped = tail_start - head_end
    return (
        messages[:head_end]
        + [{
            "role": "user",
            "content": f"[snipped {snipped} messages]",
        }]
        + messages[tail_start:]
    )

# L2 micro_compact 把除去白名单中的工具(例如read_file)的结果全部替换为占位符
def micro_compact(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    tool_results = [
        message
        for message in messages
        if _is_tool_result_message(message)
    ]

    if len(tool_results) <= KEEP_RECENT:
        return messages

    tool_name_map: dict[str, str] = {}
    for message in messages:
        if not _message_has_tool_use(message):
            continue

        for tool_call in message["tool_calls"]:
            tool_call_id = tool_call.get("id")
            function = tool_call.get("function", {})
            tool_name = function.get("name")
            if tool_call_id and tool_name:
                tool_name_map[tool_call_id] = tool_name

    for result in tool_results[:-KEEP_RECENT]:
        tool_name = tool_name_map.get(result["tool_call_id"], "unknown")
        if tool_name in PRESERVE_RESULT_TOOLS:
            continue

        result["content"] = f"[Earlier tool result compacted. Re-run if needed.]"

    return messages

# L3 tool_result_budget 对于过大的工具调用结果，截断保留，同时保存到本地，需要时可以重新读
def tool_result_budget(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for message in messages:
        if (
            not _is_tool_result_message(message)
            or not isinstance(message.get("content"), str)
        ):
            continue

        content = message["content"]
        if len(content) <= TOOL_RESULT_MAX_CHARS:
            continue

        tool_call_id = message.get("tool_call_id")
        if not isinstance(tool_call_id, str):
            continue

        archive_name = f"{sha256(tool_call_id.encode()).hexdigest()}.json"
        archive_path = TOOL_RESULT_ARCHIVE_DIR / archive_name
        try:
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            archive_path.write_text(content, encoding="utf-8")
        except OSError:
            continue

        prefix = (
            f"[Tool result truncated. Full output saved to "
            f"{archive_path.as_posix()}; use read_file to retrieve it "
            "if needed.]\n\n"
        )
        retained_chars = max(0, TOOL_RESULT_MAX_CHARS - len(prefix))
        message["content"] = f"{prefix}{content[:retained_chars]}"

    return messages


def write_transcript(messages: list[dict[str, Any]]) -> Path:
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"{sha256(str(time.time()).encode()).hexdigest()}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for message in messages:
            f.write(json.dumps(message, ensure_ascii=False, default=str) + "\n")
    return path

# L4 LLM全量摘要，保存目前的对话会用jsonl格式写入.swe-agent/transcripts/，然后用llm摘要当前对话并回填替换当前消息历史
def compact_history(
    messages: list[dict[str, Any]],
    client: OpenAI,
    model: str,
) -> CompactHistoryResult:
    if not messages:
        return CompactHistoryResult(
            ok=False,
            error="Session history is empty.",
        )

    try:
        transcript_path = write_transcript(messages)
    except OSError as exc:
        return CompactHistoryResult(
            ok=False,
            error=f"Failed to save transcript: {exc}",
        )

    transcript = "\n".join(
        json.dumps(message, ensure_ascii=False, default=str)
        for message in messages
    )
    summary_messages: list[ChatCompletionMessageParam] = [
        ChatCompletionSystemMessageParam(
            role="system",
            content=(
                "Summarize this agent conversation for its future self. "
                "Preserve the user's goal, decisions, completed work, "
                "important tool outputs and file paths, unresolved issues, "
                "and the next actions. Do not invent facts."
            ),
        ),
        ChatCompletionUserMessageParam(
            role="user",
            content=transcript,
        ),
    ]
    try:
        response = request_with_retry(
            lambda: client.chat.completions.create(
                model=model,
                messages=summary_messages,
                max_tokens=1_024,
            )
        )
    except OpenAIError as exc:
        return CompactHistoryResult(
            ok=False,
            transcript_path=transcript_path,
            error=f"Summary request failed: {exc}",
        )

    if not response.choices:
        return CompactHistoryResult(
            ok=False,
            transcript_path=transcript_path,
            error="Summary response contained no choices.",
        )

    summary = response.choices[0].message.content
    if not isinstance(summary, str) or not summary.strip():
        return CompactHistoryResult(
            ok=False,
            transcript_path=transcript_path,
            error="Summary response was empty.",
        )

    messages[:] = [
        {
            "role": "user",
            "content": (
                "[Conversation compacted by an LLM. "
                f"Full transcript: {transcript_path.as_posix()} "
                "(use read_file if needed).]\n\n"
                f"{summary.strip()}"
            ),
        }
    ]
    return CompactHistoryResult(
        ok=True,
        transcript_path=transcript_path,
    )
