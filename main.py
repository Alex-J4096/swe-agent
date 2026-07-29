import os
from pathlib import Path

from prompt_toolkit import prompt

from src.infrastructure.model_provider import Provider
from src.prompts import SYSTEM
from src.runtime.agent_runner import AgentRunner
from src.runtime.session import Session
from src.tools.toolset import ToolSet
from src.utils.logger import Logger
from src.utils.slash_commands.base import CommandContext
from src.utils.slash_commands.factory import create_command_registry

MAX_TOKENS = 1024
MAX_TOOL_ITERATIONS = 10
MODEL = "deepseek-ai/DeepSeek-V4-Flash"


def build_system_prompt(toolset: ToolSet) -> str:
    return SYSTEM.format(
        workspace=Path.cwd(),
        skills=toolset.skill_loader.get_skill_description(),
    )


def log_tool_call(name: str, arguments: str) -> None:
    Logger.debug("TOOL", f"{name} -> {arguments}")


def main() -> None:
    api_key = os.getenv("SILICONFLOW_API_KEY")
    if api_key is None:
        print("Please provide a valid API key")
        return

    provider = Provider(provider_name="SiliconFlow", api_key=api_key)
    toolset = ToolSet()
    runner = AgentRunner(
        provider=provider,
        max_tokens=MAX_TOKENS,
        max_tool_iterations=MAX_TOOL_ITERATIONS,
    )
    system_prompt = build_system_prompt(toolset)
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
        result = runner.run(
            session=session,
            toolset=toolset,
            system_prompt=system_prompt,
            on_tool_call=log_tool_call,
        )

        if result.ok:
            if result.content:
                print(result.content)
            Logger.debug("LLM", result.content)
        else:
            Logger.error(result.error or "Agent run failed.", "LLM")


if __name__ == "__main__":
    main()
