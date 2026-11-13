from src.infrastructure.experiment_log import logged_completion, record_event
import json
from copy import deepcopy
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any
from pathlib import Path
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
from src.runtime.approval import (
    ApprovalRequestRecord,
    ApprovalResponse,
    ApprovalRuntime,
    ApprovalSource,
)
from src.runtime.permissions import PermissionDecision, PermissionPolicy
from src.runtime.context_rewind import (
    ContextCheckpointManager,
    ContextRewindResult,
)
from src.tools.base import ToolContext
from src.tools.todo_manager.todo_manager import (
    TODO_STATE_KEY,
    get_todo_tasks,
    has_unfinished_tasks,
    render_todo_list,
)
from src.tools.toolset import ToolSet
from src.ui.console import ConsoleRenderer
from src.utils.terminal import ESCAPE, read_confirmation
from src.utils.interrupt import check_interrupted, run_interruptible


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
    stopped_by_permission: bool = False
    waiting_for_background: bool = False


class AgentRunner:
    MAX_TRUNCATION_CONTINUATIONS = 3
    TRUNCATION_MAX_TOKENS_MULTIPLIER = 8
    MAX_PERMISSION_REJECTIONS = 3

    def __init__(
        self,
        provider: Provider,
        workdir: Path | None = None,
        max_tokens: int = 1024,
        max_tool_iterations: int = 10,
        todo_reminder_interval: int = 3,
        max_history_messages: int = 50,
        renderer: ConsoleRenderer | None = None,
        approval_runtime: ApprovalRuntime | None = None,
        permission_policy: PermissionPolicy | None = None,
        context_checkpoint_manager: ContextCheckpointManager | None = None,
        execution_mode: str = "sandbox",
        prior_messages: list[dict[str, Any]] | None = None,
    ) -> None:
        if todo_reminder_interval < 1:
            raise ValueError("todo_reminder_interval must be at least 1")
        if max_history_messages < 3:
            raise ValueError("max_history_messages must be at least 3")

        # Keep the provider reference instead of copying the client.  This lets
        # /model replace the provider credentials without rebuilding the loop.
        self.provider = provider
        self.workdir = (workdir or Path.cwd()).resolve()
        # Keep imported conversations outside the current turn’s compaction window.
        self.prior_messages = deepcopy(prior_messages or [])
        self.max_tokens = max_tokens
        self.max_tool_iterations = max_tool_iterations
        self.todo_reminder_interval = todo_reminder_interval
        self.max_history_messages = max_history_messages
        self.renderer = renderer or ConsoleRenderer()
        self.approval_runtime = approval_runtime or ApprovalRuntime()
        self.permission_policy = permission_policy or PermissionPolicy(self.workdir, execution_mode)
        self.context_checkpoint_manager = (
            context_checkpoint_manager or ContextCheckpointManager(self.workdir)
        )
        self._permission_interrupted = False

    def run(
        self,
        session: Session,
        toolset: ToolSet,
        system_prompt: str,
        # 等价 def callback(name: str, arguments: str) -> None，工具调用请求回调
        on_tool_requested: Callable[[str, str], None] | None = None,
        confirm_permission: Callable[[str, str], bool] | None = None,
        on_tool_result: Callable[[str, dict[str, Any]], None] | None = None,
        before_model_request: Callable[[Session], None] | None = None,
        tool_context: ToolContext | None = None,
        stop_after_tool: Callable[[str, dict[str, Any]], bool] | None = None,
        continue_after_tool: Callable[[str, dict[str, Any]], bool] | None = None,
        on_context_rewound: (
            Callable[[Session, ContextRewindResult], None] | None
        ) = None,
    ) -> AgentRunResult:
        self._permission_interrupted = False
        session.pending_context_rewind = None
        tools = toolset.schemas()
        rewind_enabled = any(
            isinstance(schema, dict)
            and isinstance(schema.get("function"), dict)
            and schema["function"].get("name") == "rewind_context"
            for schema in tools
        )
        iterations_since_todo_update = 0
        turn_approved_scopes: set[str] = set()
        rejected_operations: set[str] = set()
        rejection_count = 0

        for iteration in range(self.max_tool_iterations):
            check_interrupted()
            if before_model_request is not None:
                before_model_request(session)
            self._inject_background_events(session, toolset)
            self._apply_message_limit(session)
            if rewind_enabled:
                try:
                    self.context_checkpoint_manager.create_checkpoint(
                        session,
                        reason=f"model_step_{iteration + 1}",
                    )
                except (OSError, TypeError, ValueError):
                    # A new checkpoint write failure must not stop the primary
                    # loop; older validated checkpoints may remain available.
                    pass
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
            if rewind_enabled:
                rewind_prompt = self.context_checkpoint_manager.prompt(session)
                if rewind_prompt:
                    effective_system_prompt = (
                        f"{effective_system_prompt}\n\n{rewind_prompt}"
                    )

            self.renderer.start_thinking("Thinking · Esc/Ctrl+C to stop")
            try:
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
            finally:
                self.renderer.stop_thinking()

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
                    waiting_for_background=self._has_pending_background(
                        session,
                        toolset,
                    ),
                )

            todo_updated = False
            effective_tool_context = tool_context or ToolContext(session=session)
            mixed_rewind_batch = len(tool_calls) > 1 and any(
                tool_call.function.name == "rewind_context"
                for tool_call in assistant_message.tool_calls
            )
            for call_index, tool_call in enumerate(assistant_message.tool_calls):
                check_interrupted()
                response: ApprovalResponse | None = None
                tool_name = tool_call.function.name
                arguments_json = tool_call.function.arguments
                if on_tool_requested is not None:
                    on_tool_requested(
                        tool_name,
                        arguments_json,
                    )

                try:
                    permission_args = json.loads(arguments_json)
                except json.JSONDecodeError:
                    permission_args = {}

                if not isinstance(permission_args, dict):
                    permission_args = {}

                if tool_name == "rewind_context" and mixed_rewind_batch:
                    output = {
                        "ok": False,
                        "error": (
                            "rewind_context must be the sole tool call in its "
                            "model step so every external effect is represented "
                            "in carry_forward."
                        ),
                    }
                    if on_tool_result is not None:
                        on_tool_result(tool_name, output)
                    session.history.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps(output, ensure_ascii=False),
                        }
                    )
                    continue

                policy = self.permission_policy
                registry = getattr(toolset, "registry", None)
                shell_tool = registry.get("run_bash") if registry is not None else None
                executor = getattr(shell_tool, "executor", None)
                mode = getattr(executor, "execution_mode", policy.execution_mode)
                workspace = effective_tool_context.workspace_root or self.workdir
                if mode != policy.execution_mode or workspace.resolve() != policy.workdir:
                    policy = PermissionPolicy(workspace, mode)
                permission = policy.evaluate(
                    tool_name,
                    permission_args,
                    session=session,
                )
                operation_key = permission.scope_key or json.dumps(
                    [tool_name, permission_args], ensure_ascii=False, sort_keys=True
                )
                if operation_key in rejected_operations:
                    permission = replace(
                        permission,
                        outcome="deny",
                        reason=(
                            "This operation was already rejected. Choose a "
                            "permitted alternative; do not bypass the rejection."
                        ),
                    )
                if (
                    permission.scope_key
                    and permission.reusable
                    and permission.scope_key in turn_approved_scopes
                    and permission.outcome == "require_approval"
                ):
                    permission = replace(permission, outcome="allow")
                permission_reason = permission.reason
                hard_denied = permission.outcome == "deny"
                if hard_denied:
                    self._print_permission_decision(
                        tool_name,
                        permission_args,
                        allowed=False,
                        reason=permission_reason,
                    )
                    allowed = False
                elif permission_reason is None:
                    allowed = True
                elif permission.outcome == "allow":
                    allowed = True
                else:
                    response = self._request_approval(
                        tool_call_id=tool_call.id,
                        tool_name=tool_name,
                        args=permission_args,
                        arguments_json=arguments_json,
                        permission=permission,
                        session=session,
                        context=effective_tool_context,
                        confirm_permission=confirm_permission,
                    )
                    allowed = response.approved
                    if response.decision == "stop":
                        self._permission_interrupted = True
                    if (
                        response.decision == "approve_turn"
                        and permission.scope_key
                        and permission.reusable
                    ):
                        turn_approved_scopes.add(permission.scope_key)
                    if (
                        response.decision == "approve_session"
                        and permission.scope_key
                        and permission.reusable
                    ):
                        session.approved_permission_scopes.add(permission.scope_key)

                if allowed:
                    check_interrupted()
                    capabilities = effective_tool_context.granted_capabilities
                    capabilities = capabilities.union(permission.required_capabilities)
                    dispatch_context = replace(
                        effective_tool_context,
                        granted_capabilities=capabilities,
                    )
                    self.renderer.start_thinking(
                        f"Executing {tool_name} · Esc/Ctrl+C to stop"
                    )
                    try:
                        output = toolset.dispatch(
                            tool_name,
                            arguments_json,
                            context=dispatch_context,
                        )
                    finally:
                        self.renderer.stop_thinking()
                else:
                    feedback = ""
                    if not hard_denied and response is not None:
                        feedback = response.feedback.strip()
                    output = {
                        "ok": False,
                        "permission_denied": True,
                        "retry_allowed": False,
                        "guidance": (
                            "Continue with a permitted alternative. Do not repeat "
                            "this operation or bypass the restriction using "
                            "another tool or command spelling."
                        ),
                        "error": (
                            f"Permission denied: {permission_reason}"
                            + (f". User feedback: {feedback}" if feedback else "")
                        ),
                    }
                    rejected_operations.add(operation_key)
                    rejection_count += 1
                if tool_name == TODO_STATE_KEY and output.get("ok"):
                    todo_updated = True

                record_event("tool_result", session_id=session.session_id,
                             tool=tool_name, arguments=arguments_json, output=output)
                if on_tool_result is not None:
                    on_tool_result(tool_name, output)

                session.history.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(output, ensure_ascii=False),
                    }
                )

                if not allowed:
                    # Resolve every outstanding call before the next model
                    # request or resume. Replan before executing the rest of a
                    # batch whose assumptions may depend on the rejected call.
                    for skipped in assistant_message.tool_calls[call_index + 1:]:
                        skipped_output = {
                            "ok": False,
                            "error": "Skipped after permission rejection; replan using permitted alternatives.",
                        }
                        session.history.append({
                            "role": "tool",
                            "tool_call_id": skipped.id,
                            "content": json.dumps(skipped_output),
                        })
                        if on_tool_result is not None:
                            on_tool_result(skipped.function.name, skipped_output)

                if self._permission_interrupted:
                    session.pending_context_rewind = None
                    self._compact_after_round(session)
                    return AgentRunResult(
                        ok=True,
                        content=None,
                        stopped_by_permission=True,
                    )
                if not allowed and rejection_count < self.MAX_PERMISSION_REJECTIONS:
                    break

                if stop_after_tool is not None and stop_after_tool(tool_name, output):
                    should_continue = (
                        continue_after_tool(tool_name, output)
                        if continue_after_tool is not None
                        else False
                    )
                    if not should_continue:
                        session.pending_context_rewind = None
                        self._compact_after_round(session)
                        return AgentRunResult(ok=True, content=None)

                if not allowed and rejection_count >= self.MAX_PERMISSION_REJECTIONS:
                    session.pending_context_rewind = None
                    summary = self._request_permission_summary(
                        session=session,
                        system_prompt=effective_system_prompt,
                    )
                    session.history.append(
                        {
                            "role": "assistant",
                            "content": summary,
                        }
                    )
                    self._compact_after_round(session)
                    return AgentRunResult(
                        ok=True,
                        content=summary,
                        stopped_by_permission=True,
                    )
            if rewind_enabled and session.pending_context_rewind is not None:
                try:
                    rewind_result = self.context_checkpoint_manager.apply_pending(
                        session
                    )
                except (OSError, TypeError, ValueError) as exc:
                    session.pending_context_rewind = None
                    session.history.append(
                        {
                            "role": "user",
                            "content": (
                                "[Context rewind failed; the current history was "
                                f"preserved. Error: {exc}]"
                            ),
                        }
                    )
                else:
                    if rewind_result is not None:
                        if on_context_rewound is not None:
                            try:
                                on_context_rewound(session, rewind_result)
                            except Exception:
                                # Cache invalidation and UI callbacks are advisory;
                                # the already-applied rewind remains authoritative.
                                pass
                        self._compact_after_round(session)
                        iterations_since_todo_update = 0
                        continue

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

    @staticmethod
    def _background_manager(toolset: ToolSet) -> Any | None:
        return getattr(toolset, "background_task_manager", None)

    def _inject_background_events(
        self,
        session: Session,
        toolset: ToolSet,
    ) -> None:
        manager = self._background_manager(toolset)
        if manager is None:
            return

        delivered = set(
            session.tool_state.get("background_task_event_ids", [])
        )
        try:
            events = manager.pending_events(session.session_id)
        except (OSError, ValueError, TypeError):
            return

        for task in events:
            event_id = task.event_id or task.id
            if event_id in delivered:
                continue
            event = {
                "task_id": task.id,
                "subject": task.subject,
                "status": task.status,
                "result": task.result,
                "error": task.error,
            }
            session.history.append(
                {
                    "role": "user",
                    "content": (
                        "[Background task event]\n"
                        "A background task assigned to this session reached "
                        "a terminal state. Treat this as an external event, "
                        "not as a new user request.\n"
                        f"{json.dumps(event, ensure_ascii=False)}"
                    ),
                }
            )
            delivered.add(event_id)
            try:
                manager.acknowledge_event(task.id)
            except (OSError, ValueError, TypeError):
                # The in-memory delivery marker prevents duplicate injection
                # during this run; the persisted acknowledgement can retry on
                # the next session invocation.
                pass

        session.tool_state["background_task_event_ids"] = sorted(delivered)

    @classmethod
    def _has_pending_background(
        cls,
        session: Session,
        toolset: ToolSet,
    ) -> bool:
        manager = cls._background_manager(toolset)
        if manager is None:
            return False
        try:
            return manager.has_pending(session.session_id)
        except (OSError, ValueError, TypeError):
            return False

    def _request_permission_summary(
        self,
        session: Session,
        system_prompt: str,
    ) -> str:
        fallback = "操作已被拒绝，未继续执行其他工具。"
        messages = [
            {
                "role": "system",
                "content": system_prompt,
            },
            *self.prior_messages,
            *session.history,
        ]
        self.renderer.start_thinking("Preparing response · Esc/Ctrl+C to stop")
        client = self.provider.client
        model = session.model
        messages = deepcopy(messages)
        try:
            try:
                response = run_interruptible(lambda: request_with_retry(
                    lambda: logged_completion(client, component="agent",
                        model=model,
                        messages=list(messages),
                        tools=[],
                        tool_choice="none",
                        max_tokens=self.max_tokens,
                    )
                ))
            except OpenAIError:
                return fallback
        finally:
            self.renderer.stop_thinking()

        choices = getattr(response, "choices", [])
        if not choices:
            return fallback
        content = getattr(choices[0].message, "content", None)
        if not isinstance(content, str) or not content.strip():
            return fallback
        return content

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
            *self.prior_messages,
            *session.history,
        ]

        def request(messages: list[dict[str, Any]], max_tokens: int) -> Any:
            client = self.provider.client
            model = session.model
            request_messages = deepcopy(messages)
            request_tools = deepcopy(tools)
            return run_interruptible(lambda: request_with_retry(
                lambda: logged_completion(client, component="agent",
                    model=model,
                    messages=request_messages,
                    tools=request_tools,
                    tool_choice="auto",
                    max_tokens=max_tokens,
                )
            ))

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

    @staticmethod
    def _permission_target(args: dict[str, Any]) -> str:
        command = args.get("command")
        if isinstance(command, str):
            return command
        return json.dumps(args, ensure_ascii=False)

    def _print_permission_decision(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        allowed: bool,
        reason: str | None = None,
        scope: str | None = None,
    ) -> None:
        target = self._permission_target(args)
        self.renderer.permission_decision(
            tool_name,
            target,
            allowed=allowed,
            scope=scope,
            reason=reason,
        )

    def _prompt_for_approval(
        self,
        request: ApprovalRequestRecord,
    ) -> ApprovalResponse:
        self.renderer.permission_request(request)
        question = self.renderer.permission_question(
            request.tool_name, reusable=request.reusable
        )
        valid_answers = {ESCAPE, "y", "yes", "1", "n", "no", "3", "s", "stop"}
        if request.reusable:
            valid_answers.update({"t", "turn", "a", "session", "2"})
        while True:
            answer, raw_tty = read_confirmation(question)
            if raw_tty:
                sys.stdout.write("\n")
                sys.stdout.flush()
            if answer in valid_answers:
                break
            self.renderer.show_notice(
                "Choose y/n/s" + (
                    " or t/a for this exact operation." if request.reusable
                    else "; this operation supports once only."
                )
            )
        if not raw_tty and sys.stdin.isatty() and sys.stdout.isatty():
            self.renderer.clear_input_line()

        if answer == ESCAPE:
            self._permission_interrupted = True
            self.renderer.show_notice(
                "Permission prompt interrupted. Returning to the user prompt."
            )
            return ApprovalResponse("stop", "User interrupted the approval prompt")

        decisions = {
            "y": "approve_once",
            "yes": "approve_once",
            "1": "approve_once",
            "t": "approve_turn",
            "turn": "approve_turn",
            "a": "approve_session",
            "session": "approve_session",
            "2": "approve_session",
            "s": "stop",
            "stop": "stop",
        }
        decision = decisions.get(answer, "reject")
        response = ApprovalResponse(decision)  # type: ignore[arg-type]
        scope = {
            "approve_once": "once",
            "approve_turn": "turn",
            "approve_session": "session",
            "reject": "once",
            "stop": "task",
        }[response.decision]
        self._print_permission_decision(
            request.tool_name,
            self._approval_arguments(request.arguments_json),
            allowed=response.approved,
            scope=scope,
            reason=None if response.approved else request.reason,
        )
        return response

    @staticmethod
    def _approval_arguments(arguments_json: str) -> dict[str, Any]:
        try:
            value = json.loads(arguments_json)
        except (TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _request_approval(
        self,
        tool_call_id: str,
        tool_name: str,
        args: dict[str, Any],
        *,
        arguments_json: str,
        permission: PermissionDecision,
        session: Session,
        context: ToolContext,
        confirm_permission: Callable[[str, str], bool] | None,
    ) -> ApprovalResponse:
        source = ApprovalSource(
            kind=context.approval_source_kind,
            session_id=session.session_id,
            agent_id=context.agent_id,
            task_id=context.task_id,
            interactive=context.approval_interactive,
        )
        request = self.approval_runtime.create_request(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments_json=arguments_json,
            reason=permission.reason or "Approval required",
            preview=self._permission_target(args),
            scope_key=permission.scope_key,
            capability=permission.capability,
            source=source,
            capabilities=tuple(sorted(permission.required_capabilities)),
            cwd=(
                str(((context.workspace_root or self.workdir) / args.get("cwd", ".")).resolve())
                if tool_name == "run_bash" else str(context.workspace_root or self.workdir)
            ),
            reusable=permission.reusable,
            risk=permission.risk,
        )
        if context.approval_interactive:
            try:
                if confirm_permission is not None:
                    response = ApprovalResponse(
                        "approve_once"
                        if confirm_permission(tool_name, arguments_json)
                        else "reject"
                    )
                else:
                    response = self._prompt_for_approval(request)
            except Exception as exc:
                response = ApprovalResponse(
                    "reject",
                    f"Approval handler failed: {type(exc).__name__}",
                )
            self.approval_runtime.resolve(request.id, response)
        return self.approval_runtime.wait_for_response(request.id)

    def _check_permission(
        self,
        name: str,
        args: dict[str, Any],
        session: Session | None = None,
    ) -> tuple[str | None, bool]:
        decision = self.permission_policy.evaluate(name, args, session=session)
        if decision.outcome == "allow":
            return None, False
        return decision.reason, decision.outcome == "deny"
