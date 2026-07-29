import json
import os
# from prompt_toolkit import print_formatted_text as print
from openai import OpenAIError
from prompt_toolkit import prompt

from src.infrastructure.model_provider import Provider
from src.runtime.session import Session
from src.tools.toolset import ToolSet
from src.utils.logger import Logger
from src.utils.slash_commands.base import CommandContext
from src.utils.slash_commands.factory import create_command_registry

MAX_TOKENS = 1024
MAX_TOOL_ITERATIONS = 10
MODEL = "deepseek-ai/DeepSeek-V4-Flash"

toolset = ToolSet()
TOOLS = toolset.schemas()

SYSTEM_PROMPT = f"""You are a coding assistant at {os.getcwd()}. 
Available skills: {toolset.skill_loader.get_skill_description()}.
When a task matches an available skill, call load_skill first.`
Use tools to solve tasks. Use the todo tool to plan multi-step tasks. Mark in_progress before starting, completed when done.
Prefer tools over prose.
Unless requested by the user, or the task does not require coding, you will not provide any explanations or comments, only the code."""


api_key=os.getenv("SILICONFLOW_API_KEY")
if api_key is None:
    print("Please provide a valid API key")
    exit(1)

provider = Provider(provider_name="SiliconFlow", api_key=api_key)
client = provider.client

def agent_loop(session: Session) -> str | None:
    for _ in range(MAX_TOOL_ITERATIONS):
        try:
            response = client.chat.completions.create(
                model=session.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    *session.history
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
        session.history.append(history_message)

        if not assistant_message.tool_calls:
            print(assistant_message.content)
            return assistant_message.content

        for tool_call in assistant_message.tool_calls:
            tool_name = tool_call.function.name
            tool_args = tool_call.function.arguments

            Logger.debug("TOOL", f"{tool_name} -> {tool_args}")

            output = toolset.dispatch(tool_name, tool_args)

            session.history.append({
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
    session = Session(model=MODEL)
    command_context = CommandContext(session=session, toolset=toolset, provider=provider)
    command_registry = create_command_registry()

    while session.running:
        try:
            query = prompt("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not query:
            continue

        if query.startswith("//"):
            query = query[1:]
        elif query.startswith("/"):
            result = command_registry.dispatch(query, command_context)

            if result.message:
                print(result.message)

            if result.exit_requested:
                session.running = False

            continue

        session.history.append({"role": "user", "content": query})
        response = agent_loop(session)
        Logger.debug("LLM", response)
