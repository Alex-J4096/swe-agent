import json
import os
from typing import Any
# from prompt_toolkit import print_formatted_text as print
from openai import OpenAIError
from prompt_toolkit import prompt
from src.infrastructure.model_provider import Provider
from src.tools.toolset import ToolSet
from src.utils.logger import Logger

MAX_TOKENS = 1024
MAX_TOOL_ITERATIONS = 10
MODEL = "deepseek-ai/DeepSeek-V4-Flash"
SYSTEM_PROMPT = f"""You are a coding assistant at {os.getcwd()}. 
Use tools to solve tasks. Use the todo tool to plan multi-step tasks. Mark in_progress before starting, completed when done.
Prefer tools over prose.
Unless requested by the user, or the task does not require coding, you will not provide any explanations or comments, only the code."""

toolset = ToolSet()
TOOLS = toolset.schemas()

api_key=os.getenv("SILICONFLOW_API_KEY")
if api_key is None:
    print("Please provide a valid API key")
    exit(1)

provider = Provider(provider_name="SiliconFlow", api_key=api_key)
client = provider.client

def agent_loop(messages: list[dict[str, Any]]) -> str | None:
    for _ in range(MAX_TOOL_ITERATIONS):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    *messages
                ],
                tools=TOOLS,
                tool_choice="auto",
                max_tokens=MAX_TOKENS,
            )
        except OpenAIError as exc:
            Logger.error(f"Model request failed: {exc}", "LLM")
            return None

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
        messages.append(history_message)

        if not assistant_message.tool_calls:
            print(assistant_message.content)
            return assistant_message.content

        for tool_call in assistant_message.tool_calls:
            tool_name = tool_call.function.name
            tool_args = tool_call.function.arguments

            Logger.debug("TOOL", f"{tool_name} -> {tool_args}")

            output = toolset.dispatch(tool_name, tool_args)

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": json.dumps(output, ensure_ascii=False),
            })

    Logger.warning(
        f"Stopped after {MAX_TOOL_ITERATIONS} tool iterations.",
        "LLM",
    )
    return None


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = prompt("> ")
        except (EOFError, KeyboardInterrupt):
            break

        history.append({"role": "user", "content": query})
        response = agent_loop(history)
        Logger.debug("LLM", response)
