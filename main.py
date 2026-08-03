import json
import os
from pathlib import Path
from typing import Any

from prompt_toolkit import prompt

from src.infrastructure.memory import MemoryStore
from src.runtime.subagent_service import SubagentService
from src.runtime.memory_worker import MemoryWorker
from src.infrastructure.model_provider import Provider
from src.prompts import SYSTEM
from src.runtime.agent_runner import AgentRunner
from src.runtime.session import Session
from src.tools.toolset import ToolSet
from src.utils.logger import Logger
from src.utils.welcome import print_welcome
from src.utils.slash_commands.base import CommandContext
from src.utils.slash_commands.factory import create_command_registry
from src.tools.subagent.delegate_task import DelegateTaskTool

MAX_TOKENS = 1024
MAX_TOOL_ITERATIONS = 10
MODEL = "deepseek-ai/DeepSeek-V4-Flash"
TOOL_ARGUMENT_PREVIEW_LENGTH = 120


def prepare_user_input(query: str) -> tuple[str, bool]:
    """Preserve user input and identify commands only at the start of the query."""
    if query.startswith("//"):
        return query[1:], False

    return query, query.startswith("/")


def build_system_prompt(
    toolset: ToolSet,
    workdir: Path,
    memory_store: MemoryStore,
) -> str:
    system_prompt = SYSTEM.format(
        workspace=workdir,
        skills=toolset.skill_loader.get_skill_description(),
    )
    memory_index = memory_store.read_memory_index()
    if not memory_index:
        return system_prompt

    return (
        f"{system_prompt}\n\n"
        "Persistent memory index:\n"
        f"{memory_index}"
    )


def log_tool_call(name: str, arguments: str) -> None:
    Logger.debug("TOOL", f"{name} -> {arguments}")


def display_tool_call(name: str, arguments: str) -> None:
    """Display a compact, single-line summary of a tool call."""
    try:
        parsed_arguments = json.loads(arguments)
        preview = json.dumps(
            parsed_arguments,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (json.JSONDecodeError, TypeError):
        preview = " ".join(str(arguments).split())

    if preview == "{}":
        preview = ""
    elif len(preview) > TOOL_ARGUMENT_PREVIEW_LENGTH:
        preview = f"{preview[:TOOL_ARGUMENT_PREVIEW_LENGTH - 1]}…"

    suffix = f"  {preview}" if preview else ""
    print(f"  → {name}{suffix}")


def display_tool_result(name: str, output: dict[str, Any]) -> None:
    if name != "todo_manager" or not output.get("ok"):
        return

    content = output.get("content")
    if isinstance(content, str) and content:
        print(content)


def main() -> None:
    workdir = Path.cwd().resolve()

    api_key = os.getenv("SILICONFLOW_API_KEY")
    if api_key is None:
        print("Please provide a valid API key")
        return

    provider = Provider(provider_name="SiliconFlow", api_key=api_key)
    session = Session(model=MODEL)
    print_welcome(session, workdir)
    memory_store = MemoryStore(
        workdir,
        client=provider.client,
        model_getter=lambda: session.model,
    )
    memory_worker = MemoryWorker(memory_store)
    toolset = ToolSet()
    runner = AgentRunner(
        provider=provider,
        max_tokens=MAX_TOKENS,
        max_tool_iterations=MAX_TOOL_ITERATIONS,
    )

    subagent_toolset = toolset.view(exclude={"delegate_task"})
    subagent_service = SubagentService(runner=runner, toolset=subagent_toolset)

    toolset.register(
        DelegateTaskTool(service=subagent_service)
    )

    command_context = CommandContext(session=session, toolset=toolset, provider=provider)
    command_registry = create_command_registry()

    while session.running:
        try:
            query = prompt("> ")
        except (EOFError, KeyboardInterrupt):
            break

        if not query.strip():
            continue

        query, is_command = prepare_user_input(query)
        if is_command:
            result = command_registry.dispatch(query, command_context)

            if result.message:
                print(result.message)

            if result.exit_requested:
                session.running = False

            continue

        session.history.append({"role": "user", "content": query})
        system_prompt = build_system_prompt(
            toolset,
            workdir,
            memory_store,
        )
        memory_context = memory_store.load_memories(
            session.history,
        )
        turn_system_prompt = system_prompt
        if memory_context:
            turn_system_prompt = (
                f"{system_prompt}\n\n"
                f"Relevant memories:\n{memory_context}"
            )

        result = runner.run(
            session=session,
            toolset=toolset,
            system_prompt=turn_system_prompt,
            on_tool_call=display_tool_call,
            # on_tool_result 只用于特定方法的打印结果，例如: todo_manager
            on_tool_result=display_tool_result,
        )

        if result.ok:
            if result.content:
                print(result.content)
           # Logger.debug("LLM", result.content)
        else:
            Logger.error(result.error or "Agent run failed.", "LLM")

        # Submit a snapshot and immediately continue to the next prompt.
        memory_worker.submit(session.history)

    memory_worker.shutdown(wait=False)


if __name__ == "__main__":
    main()
