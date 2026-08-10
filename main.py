import json
import os
from pathlib import Path
from typing import Any

from prompt_toolkit import prompt

from src.infrastructure.memory import MemoryStore
from src.infrastructure.database import (
    AppDatabase,
    ModelProfile,
    ProviderCredential,
    ProviderProfile,
    default_database_path,
)
from src.infrastructure.session_store import SessionStore
from src.runtime.subagent_service import SubagentService
from src.runtime.memory_worker import MemoryWorker
from src.infrastructure.model_provider import Provider
from src.prompts import SYSTEM
from src.runtime.agent_runner import AgentRunner
from src.runtime.session import Session
from src.tools.toolset import ToolSet
from src.utils.logger import Logger
from src.utils.welcome import print_welcome
from src.ui.console import ConsoleRenderer, format_tool_preview
from src.utils.slash_commands.base import CommandContext
from src.utils.slash_commands.factory import create_command_registry
from src.tools.subagent.delegate_task import DelegateTaskTool

MAX_TOKENS = 1024
MAX_TOOL_ITERATIONS = 10
MODEL = "deepseek-ai/DeepSeek-V4-Flash"
PROVIDER_ID = "siliconflow"
TOOL_ARGUMENT_PREVIEW_LENGTH = 120


def save_session_if_needed(
    session_store: SessionStore,
    session: Session,
    workdir: Path,
) -> bool:
    """Persist only materialized sessions, avoiding empty startup records."""
    if not session.history and not session_store.exists(session.session_id):
        return False

    session_store.save(session, workdir=workdir)
    return True


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


def display_tool_requested(
    name: str,
    arguments: str,
    renderer: ConsoleRenderer | None = None,
) -> None:
    """Display a compact, single-line summary of a tool call."""
    (renderer or ConsoleRenderer()).show_tool_request(
        name,
        format_tool_preview(arguments, TOOL_ARGUMENT_PREVIEW_LENGTH),
    )


def display_tool_result(
    name: str,
    output: dict[str, Any],
    renderer: ConsoleRenderer | None = None,
) -> None:
    (renderer or ConsoleRenderer()).show_tool_result(
        name,
        output,
        show_content=name in {"todo_manager", "task_manager"},
    )


def main() -> None:
    workdir = Path.cwd().resolve()
    renderer = ConsoleRenderer()

    database = AppDatabase(default_database_path())
    database.upsert_provider(
        ProviderProfile(
            id=PROVIDER_ID,
            name="SiliconFlow",
            base_url="https://api.siliconflow.cn/v1",
            api_key_env="SILICONFLOW_API_KEY",
        )
    )
    database.upsert_model(
        ModelProfile(
            id="siliconflow-default",
            provider_id=PROVIDER_ID,
            model_id=MODEL,
            config={},
            is_default=True,
        )
    )
    provider_profile = database.get_provider(PROVIDER_ID)
    model_profile = database.get_default_model(PROVIDER_ID)
    if provider_profile is None or model_profile is None:
        Logger.error("Failed to load provider configuration.", "CONFIG")
        database.close()
        return

    credential = database.get_default_credential(PROVIDER_ID)
    if credential is None:
        environment_token = os.getenv(provider_profile.api_key_env or "")
        if environment_token:
            database.upsert_credential(
                ProviderCredential(
                    id=f"{PROVIDER_ID}-default",
                    provider_id=PROVIDER_ID,
                    name="default",
                    access_token=environment_token,
                    is_default=True,
                )
            )
            credential = database.get_default_credential(PROVIDER_ID)

    if credential is None:
        renderer.show_error(
            f"Please provide a valid API key in {provider_profile.api_key_env}",
            "CONFIG",
        )
        database.close()
        return

    provider = Provider(
        provider_name=provider_profile.name,
        api_key=credential.access_token,
        base_url=provider_profile.base_url,
    )
    session_store = SessionStore(database)
    session = Session(
        model=model_profile.model_id,
        provider_id=provider_profile.id,
    )
    print_welcome(session, workdir, renderer=renderer)
    memory_store = MemoryStore(
        workdir,
        client=provider.client,
        model_getter=lambda: session.model,
    )
    memory_worker = MemoryWorker(memory_store)
    toolset = ToolSet()
    runner = AgentRunner(
        provider=provider,
        workdir=workdir,
        max_tokens=MAX_TOKENS,
        max_tool_iterations=MAX_TOOL_ITERATIONS,
        renderer=renderer,
    )

    subagent_toolset = toolset.view(exclude={"delegate_task"})
    subagent_service = SubagentService(runner=runner, toolset=subagent_toolset)

    toolset.register(
        DelegateTaskTool(service=subagent_service)
    )

    command_context = CommandContext(
        session=session,
        toolset=toolset,
        provider=provider,
        session_store=session_store,
        workdir=workdir,
        renderer=renderer,
    )
    command_registry = create_command_registry()
    command_completer = command_registry.completer()

    while session.running:
        try:
            query = prompt(
                "› ",
                completer=command_completer,
                complete_while_typing=True,
            )
        except (EOFError, KeyboardInterrupt):
            break

        if not query.strip():
            continue

        query, is_command = prepare_user_input(query)
        if is_command:
            result = command_registry.dispatch(query, command_context)

            if result.message:
                renderer.show_notice(result.message)

            if result.exit_requested:
                session.running = False

            continue

        session.history.append({"role": "user", "content": query})
        save_session_if_needed(session_store, session, workdir)
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
            on_tool_requested=lambda name, arguments: display_tool_requested(
                name, arguments, renderer
            ),
            # on_tool_result 只用于特定方法的打印结果，例如: todo_manager
            on_tool_result=lambda name, output: display_tool_result(
                name, output, renderer
            ),
        )
        save_session_if_needed(session_store, session, workdir)

        if result.ok:
            if result.content:
                renderer.show_assistant(result.content)
           # Logger.debug("LLM", result.content)
        else:
            renderer.show_error(result.error or "Agent run failed.", "LLM")

        # Submit a snapshot and immediately continue to the next prompt.
        memory_worker.submit(session.history)

    memory_worker.shutdown(wait=False)
    save_session_if_needed(session_store, session, workdir)
    database.close()


if __name__ == "__main__":
    main()
