import json
import os
import sys
from pathlib import Path
from typing import Any

from prompt_toolkit import prompt

from src.infrastructure.memory import MemoryStore
from src.infrastructure.memory_selection_cache import MemorySelectionCache
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
from src.runtime.memory_selection_worker import MemorySelectionWorker
from src.infrastructure.model_provider import Provider
from src.prompts import SYSTEM
from src.runtime.agent_runner import AgentRunner
from src.runtime.approval import ApprovalRuntime, ApprovalRuntimeEvent
from src.runtime.context_rewind import ContextRewindResult
from src.runtime.session import Session
from src.teams.controller import TeamController
from src.tools.base import ToolContext
from src.tools.toolset import ToolSet
from src.utils.logger import Logger
from src.utils.welcome import print_welcome
from src.ui.console import ConsoleRenderer, format_tool_preview
from src.utils.terminal import ESCAPE, read_confirmation
from src.utils.slash_commands.base import CommandContext
from src.utils.slash_commands.factory import create_command_registry
from src.tools.subagent.delegate_task import DelegateTaskTool

MAX_TOKENS = 1024
MAX_TOOL_ITERATIONS = 10
MODEL = "deepseek-ai/DeepSeek-V4-Flash"
PROVIDER_ID = "siliconflow"
TOOL_ARGUMENT_PREVIEW_LENGTH = 120

BUILTIN_PROVIDERS = (
    ProviderProfile(
        id="siliconflow",
        name="SiliconFlow",
        base_url="https://api.siliconflow.cn/v1",
        api_key_env="SILICONFLOW_API_KEY",
    ),
    ProviderProfile(
        id="openai",
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
    ),
    ProviderProfile(
        id="opencode-go",
        name="OpenCode Go",
        base_url="https://opencode.ai/zen/go/v1",
        api_key_env="OPENCODE_GO_API_KEY",
    ),
)

BUILTIN_DEFAULT_MODELS = {
    "siliconflow": MODEL,
    "openai": "gpt-5.6-luna",
    "opencode-go": "kimi-k3",
}


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


def merge_memory_filenames(
    primary: list[str],
    secondary: list[str],
    limit: int = 5,
) -> list[str]:
    """Prefer current local matches, then fill from the previous selection."""
    merged: list[str] = []
    for filename in [*primary, *secondary]:
        if filename not in merged:
            merged.append(filename)
        if len(merged) >= limit:
            break
    return merged


def confirm_team_plan_continuation(
    renderer: ConsoleRenderer,
    name: str,
    output: dict[str, Any],
) -> bool:
    """Let the user choose whether to keep running after a plan proposal."""
    if not (
        name == "propose_team_plan"
        and output.get("ok") is True
        and output.get("status") == "awaiting_approval"
    ):
        return True
    answer, raw_tty = read_confirmation(renderer.team_plan_continue_question())
    if raw_tty:
        sys.stdout.write("\n")
        sys.stdout.flush()
    elif sys.stdin.isatty() and sys.stdout.isatty():
        renderer.clear_input_line()
    if answer == ESCAPE:
        answer = "n"
    should_continue = answer in {"y", "yes", "c", "continue"}
    renderer.show_notice(
        "Continuing Agent Loop."
        if should_continue
        else "Agent Loop paused. Use /team approve <plan-id> to start teammates."
    )
    return should_continue


def load_or_prompt_credential(
    database: AppDatabase,
    profile: ProviderProfile,
) -> ProviderCredential | None:
    """Load a stored/env key, or ask for one without echoing it."""
    credential = database.get_default_credential(profile.id)
    if credential is not None:
        return credential

    environment_token = os.getenv(profile.api_key_env or "")
    if environment_token:
        database.upsert_credential(
            ProviderCredential(
                id=f"{profile.id}-default",
                provider_id=profile.id,
                name="default",
                access_token=environment_token,
                is_default=True,
            )
        )
        return database.get_default_credential(profile.id)

    try:
        token = prompt(
            f"Enter API key for {profile.name}: ",
            is_password=True,
        ).strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not token:
        return None

    database.upsert_credential(
        ProviderCredential(
            id=f"{profile.id}-default",
            provider_id=profile.id,
            name="default",
            access_token=token,
            is_default=True,
        )
    )
    return database.get_default_credential(profile.id)


def main() -> None:
    workdir = Path.cwd().resolve()
    renderer = ConsoleRenderer()

    database = AppDatabase(default_database_path())
    for profile in BUILTIN_PROVIDERS:
        database.upsert_provider(profile)
        database.upsert_model(
            ModelProfile(
                id=f"{profile.id}-default",
                provider_id=profile.id,
                model_id=BUILTIN_DEFAULT_MODELS[profile.id],
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

    credential = load_or_prompt_credential(database, provider_profile)

    if credential is None:
        renderer.show_notice(
            f"No API key configured for {provider_profile.name}. "
            "Use /model to configure a provider before sending a request."
        )

    provider = Provider(
        provider_name=provider_profile.name,
        api_key=credential.access_token if credential is not None else None,
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
    memory_selection_cache = MemorySelectionCache()
    memory_selection_worker = MemorySelectionWorker(
        memory_store,
        memory_selection_cache,
    )

    def handle_context_rewind(
        current_session: Session,
        result: ContextRewindResult,
    ) -> None:
        memory_selection_cache.clear(current_session.session_id)
        memory_worker.invalidate()
        memory_selection_worker.invalidate()
        try:
            transcript = result.transcript_path.relative_to(workdir).as_posix()
        except ValueError:
            transcript = result.transcript_path.as_posix()
        renderer.show_notice(
            f"Context rewound to {result.checkpoint_id}; discarded "
            f"{result.discarded_messages} messages. Abandoned branch: {transcript}"
        )

    toolset = ToolSet()
    approval_runtime = ApprovalRuntime()

    def show_approval_event(event: ApprovalRuntimeEvent) -> None:
        request = event.request
        if event.kind == "request_created" and not request.source.interactive:
            renderer.show_notice(
                f"Approval required ({request.id}) from "
                f"{request.source.agent_id or request.source.kind}: {request.preview}. "
                "Use /permissions to inspect or resolve it."
            )

    approval_runtime.subscribe(show_approval_event)
    runner = AgentRunner(
        provider=provider,
        workdir=workdir,
        max_tokens=MAX_TOKENS,
        max_tool_iterations=MAX_TOOL_ITERATIONS,
        renderer=renderer,
        approval_runtime=approval_runtime,
    )

    team_controller = TeamController(
        provider=provider,
        workdir=workdir,
        toolset=toolset,
        runner_factory=lambda: AgentRunner(
            provider=provider,
            workdir=workdir,
            max_tokens=MAX_TOKENS,
            max_tool_iterations=MAX_TOOL_ITERATIONS,
            renderer=renderer,
            approval_runtime=approval_runtime,
        ),
        model_getter=lambda: session.model,
        worker_count=2,
        approval_runtime=approval_runtime,
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
        memory_selection_cache=memory_selection_cache,
        team_controller=team_controller,
        database=database,
        approval_runtime=approval_runtime,
        on_provider_changed=lambda active_provider: setattr(
            memory_store,
            "client",
            active_provider.client,
        ),
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

        # Start before session/prompt/memory preparation so slow local work is
        # also visible immediately after the user submits the prompt.
        renderer.start_thinking("Preparing")
        session.history.append({"role": "user", "content": query})
        save_session_if_needed(session_store, session, workdir)
        system_prompt = build_system_prompt(
            toolset,
            workdir,
            memory_store,
        )
        memory_context = ""
        if session.memory_enabled:
            memory_revision = memory_store.memory_revision()
            local_files = memory_store.select_relevant_memories(
                session.history,
                use_llm=False,
            )
            cached_files = memory_selection_cache.get_compatible(
                session_id=session.session_id,
                messages=session.history,
                memory_revision=memory_revision,
            )
            selected_files = merge_memory_filenames(
                local_files,
                cached_files,
            )
            memory_context = memory_store.load_memories_by_filenames(
                selected_files,
            )
        turn_system_prompt = system_prompt
        if memory_context:
            turn_system_prompt = (
                f"{system_prompt}\n\n"
                f"Relevant memories:\n{memory_context}"
            )

        team_runtime = command_context.team_runtime
        if team_runtime is not None:
            turn_system_prompt += (
                "\n\nTeam mode: you are the Lead. Use propose_team_plan to "
                "propose a structured plan before calling any other tool. "
                "Do not inspect the workspace first. Base the initial plan "
                "on the user's request and make reasonable assumptions. "
                "After proposing, wait for explicit user choice or "
                "`/team approve <plan-id>`. Do not modify the workspace directly."
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
            before_model_request=(
                (
                    lambda current_session: team_runtime.deliver_pending(
                        current_session,
                        "lead",
                    )
                )
                if team_runtime is not None
                else None
            ),
            tool_context=ToolContext(
                session=session,
                team_runtime=team_runtime,
                agent_id="lead" if team_runtime is not None else None,
            ),
            stop_after_tool=(
                (
                    lambda name, output: (
                        name == "propose_team_plan"
                        and output.get("ok") is True
                        and output.get("status") == "awaiting_approval"
                    )
                )
                if team_runtime is not None
                else None
            ),
            continue_after_tool=(
                (
                    lambda name, output: confirm_team_plan_continuation(
                        renderer,
                        name,
                        output,
                    )
                )
                if team_runtime is not None
                else None
            ),
            on_context_rewound=handle_context_rewind,
        )
        save_session_if_needed(session_store, session, workdir)

        if result.ok:
            if result.content:
                renderer.show_assistant(result.content)
           # Logger.debug("LLM", result.content)
        else:
            renderer.show_error(result.error or "Agent run failed.", "LLM")

        # Submit a snapshot and immediately continue to the next prompt.
        if session.memory_enabled:
            memory_worker.submit(session.history)
            memory_selection_worker.submit(
                session.session_id,
                session.history,
            )

    team_controller.shutdown()
    approval_runtime.cancel_source(reason="Application shutdown")
    memory_worker.shutdown(wait=False)
    memory_selection_worker.shutdown(wait=False)
    save_session_if_needed(session_store, session, workdir)
    database.close()


if __name__ == "__main__":
    main()
