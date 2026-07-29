import json
from collections.abc import Callable
from dataclasses import dataclass

from openai import OpenAIError

from src.infrastructure.model_provider import Provider
from src.runtime.session import Session
from src.tools.toolset import ToolSet


@dataclass(frozen=True)
class AgentRunResult:
    ok: bool
    content: str | None = None
    error: str | None = None


class AgentRunner:
    def __init__(
        self,
        provider: Provider,
        max_tokens: int = 1024,
        max_tool_iterations: int = 10,
    ) -> None:
        self.client = provider.client
        self.max_tokens = max_tokens
        self.max_tool_iterations = max_tool_iterations

    def run(
        self,
        session: Session,
        toolset: ToolSet,
        system_prompt: str,
        # 等价 def callback(name: str, arguments: str) -> None，回调函数
        on_tool_call: Callable[[str, str], None] | None = None,
    ) -> AgentRunResult:
        tools = toolset.schemas()

        for _ in range(self.max_tool_iterations):
            try:
                response = self.client.chat.completions.create(
                    model=session.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        *session.history,
                    ],
                    tools=tools,
                    tool_choice="auto",
                    max_tokens=self.max_tokens,
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
                for tool_call in (assistant_message.tool_calls or [])
            ]
            history_message = {
                "role": "assistant",
                "content": assistant_message.content,
            }
            if tool_calls:
                history_message["tool_calls"] = tool_calls
            session.history.append(history_message)

            if not assistant_message.tool_calls:
                return AgentRunResult(
                    ok=True,
                    content=assistant_message.content,
                )

            for tool_call in assistant_message.tool_calls:
                if on_tool_call is not None:
                    on_tool_call(
                        tool_call.function.name,
                        tool_call.function.arguments,
                    )

                output = toolset.dispatch(
                    tool_call.function.name,
                    tool_call.function.arguments,
                )
                session.history.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(output, ensure_ascii=False),
                    }
                )

        return AgentRunResult(
            ok=False,
            error=(
                f"Stopped after {self.max_tool_iterations} "
                "tool iterations."
            ),
        )
