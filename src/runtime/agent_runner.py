import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from openai import OpenAIError

from src.infrastructure.compact import (
    micro_compact,
    snip_compact,
    tool_result_budget,
)
from src.infrastructure.model_provider import Provider
from src.infrastructure.retry import request_with_retry
from src.prompts import TODO_REMINDER
from src.runtime.session import Session
from src.tools.base import ToolContext
from src.tools.todo_manager.todo_manager import (
    TODO_STATE_KEY,
    get_todo_tasks,
    has_unfinished_tasks,
    render_todo_list,
)
from src.tools.toolset import ToolSet


CONTINUATION_PROMPT = (
    "Your previous response was truncated because it reached the token limit. "
    "Continue from exactly where it stopped. Do not repeat any text that was "
    "already written."
)


@dataclass(frozen=True)
class AgentRunResult:
    ok: bool
    content: str | None = None
    error: str | None = None


class AgentRunner:
    MAX_TRUNCATION_CONTINUATIONS = 3
    TRUNCATION_MAX_TOKENS_MULTIPLIER = 8

    def __init__(
        self,
        provider: Provider,
        max_tokens: int = 1024,
        max_tool_iterations: int = 10,
        todo_reminder_interval: int = 3,
        max_history_messages: int = 50,
    ) -> None:
        if todo_reminder_interval < 1:
            raise ValueError("todo_reminder_interval must be at least 1")
        if max_history_messages < 3:
            raise ValueError("max_history_messages must be at least 3")

        self.client = provider.client
        self.max_tokens = max_tokens
        self.max_tool_iterations = max_tool_iterations
        self.todo_reminder_interval = todo_reminder_interval
        self.max_history_messages = max_history_messages

    def run(
        self,
        session: Session,
        toolset: ToolSet,
        system_prompt: str,
        # 等价 def callback(name: str, arguments: str) -> None，回调函数
        on_tool_call: Callable[[str, str], None] | None = None,
        on_tool_result: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> AgentRunResult:
        tools = toolset.schemas()
        iterations_since_todo_update = 0

        for _ in range(self.max_tool_iterations):
            self._apply_message_limit(session)
            effective_system_prompt = system_prompt
            todo_tasks = get_todo_tasks(session)
            if (
                has_unfinished_tasks(todo_tasks)
                and iterations_since_todo_update
                >= self.todo_reminder_interval
            ):
                reminder = TODO_REMINDER.format(
                    interval=self.todo_reminder_interval,
                    todo_list=render_todo_list(todo_tasks)
                )
                effective_system_prompt = (
                    f"{system_prompt}\n\n{reminder}"
                )
                iterations_since_todo_update = 0

            try:
                response, recovered_content = (
                    self._request_with_truncation_recovery(
                        session=session,
                        system_prompt=effective_system_prompt,
                        tools=tools,
                    )
                )
            except OpenAIError as exc:
                return AgentRunResult(
                    ok=False,
                    error=f"Model request failed: {exc}",
                )

            assistant_message = response.choices[0].message
            tool_calls = [
                tool_call.model_dump()
                if hasattr(tool_call, "model_dump")
                else tool_call
                for tool_call in (
                    []
                    if recovered_content is not None
                    else (assistant_message.tool_calls or [])
                )
            ]
            history_message = {
                "role": "assistant",
                "content": (
                    recovered_content
                    if recovered_content is not None
                    else assistant_message.content
                ),
            }
            if tool_calls:
                history_message["tool_calls"] = tool_calls
            session.history.append(history_message)

            if not tool_calls:
                self._compact_after_round(session)
                return AgentRunResult(
                    ok=True,
                    content=history_message["content"],
                )

            todo_updated = False
            for tool_call in assistant_message.tool_calls:
                tool_name = tool_call.function.name
                if on_tool_call is not None:
                    on_tool_call(
                        tool_name,
                        tool_call.function.arguments,
                    )

                output = toolset.dispatch(
                    tool_name,
                    tool_call.function.arguments,
                    context=ToolContext(session=session),
                )
                if tool_name == TODO_STATE_KEY and output.get("ok"):
                    todo_updated = True

                if on_tool_result is not None:
                    on_tool_result(tool_name, output)

                session.history.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(output, ensure_ascii=False),
                    }
                )

            self._compact_after_round(session)

            if todo_updated:
                iterations_since_todo_update = 0
            elif has_unfinished_tasks(get_todo_tasks(session)):
                iterations_since_todo_update += 1
            else:
                iterations_since_todo_update = 0

        return AgentRunResult(
            ok=False,
            error=(
                f"Stopped after {self.max_tool_iterations} "
                "tool iterations."
            ),
        )

    def _request_with_truncation_recovery(
        self,
        session: Session,
        system_prompt: str,
        tools: list[dict[str, Any]],
    ) -> tuple[Any, str | None]:
        base_messages = [
            {
                "role": "system",
                "content": system_prompt,
            },
            *session.history,
        ]

        def request(messages: list[dict[str, Any]], max_tokens: int) -> Any:
            return request_with_retry(
                lambda: self.client.chat.completions.create(
                    model=session.model,
                    messages=list(messages),
                    tools=tools,
                    tool_choice="auto",
                    max_tokens=max_tokens,
                )
            )

        response = request(base_messages, self.max_tokens)
        if not self._response_was_truncated(response):
            return response, None

        expanded_max_tokens = (
            self.max_tokens * self.TRUNCATION_MAX_TOKENS_MULTIPLIER
        )
        response = request(base_messages, expanded_max_tokens)
        if not self._response_was_truncated(response):
            return response, None

        first_partial = response.choices[0].message.content
        first_partial_text = (
            first_partial if isinstance(first_partial, str) else ""
        )
        parts = [first_partial_text]
        continuation_messages = [
            *base_messages,
            {
                "role": "assistant",
                "content": first_partial_text,
            },
        ]

        for _ in range(self.MAX_TRUNCATION_CONTINUATIONS):
            continuation_messages.append(
                {
                    "role": "user",
                    "content": CONTINUATION_PROMPT,
                }
            )
            response = request(continuation_messages, expanded_max_tokens)
            continuation = response.choices[0].message.content
            continuation_text = (
                continuation if isinstance(continuation, str) else ""
            )
            parts.append(continuation_text)

            if not self._response_was_truncated(response):
                return response, "".join(parts)

            continuation_messages.append(
                {
                    "role": "assistant",
                    "content": continuation_text,
                }
            )

        return response, "".join(parts)

    @staticmethod
    def _response_was_truncated(response: Any) -> bool:
        if getattr(response, "stop_reason", None) == "max_tokens":
            return True

        choices = getattr(response, "choices", [])
        if not choices:
            return False

        choice = choices[0]
        if getattr(choice, "stop_reason", None) == "max_tokens":
            return True
        if getattr(choice, "finish_reason", None) in {
            "length",
            "max_tokens",
        }:
            return True

        message = getattr(choice, "message", None)
        return getattr(message, "stop_reason", None) == "max_tokens"

    def _compact_after_round(self, session: Session) -> None:
        tool_result_budget(session.history)
        micro_compact(session.history)
        self._apply_message_limit(session)

    def _apply_message_limit(self, session: Session) -> None:
        if len(session.history) <= self.max_history_messages:
            return
        session.history[:] = snip_compact(
            session.history,
            max_messages=self.max_history_messages,
        )
