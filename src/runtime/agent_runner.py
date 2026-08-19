import json
import shlex
import sys
from collections.abc import Callable
from dataclasses import dataclass
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
from src.tools.base import ToolContext
from src.tools.todo_manager.todo_manager import (
    TODO_STATE_KEY,
    get_todo_tasks,
    has_unfinished_tasks,
    render_todo_list,
)
from src.tools.toolset import ToolSet
from src.ui.console import ConsoleRenderer
from src.utils.shell import READONLY_PREFIXES, is_readonly_command, shell_tokens
from src.utils.terminal import ESCAPE, read_confirmation


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
    SAFE_BASH_PREFIXES = READONLY_PREFIXES

    def __init__(
        self,
        provider: Provider,
        workdir: Path | None = None,
        max_tokens: int = 1024,
        max_tool_iterations: int = 10,
        todo_reminder_interval: int = 3,
        max_history_messages: int = 50,
        renderer: ConsoleRenderer | None = None,
    ) -> None:
        if todo_reminder_interval < 1:
            raise ValueError("todo_reminder_interval must be at least 1")
        if max_history_messages < 3:
            raise ValueError("max_history_messages must be at least 3")

        # Keep the provider reference instead of copying the client.  This lets
        # /model replace the provider credentials without rebuilding the loop.
        self.provider = provider
        self.workdir = (workdir or Path.cwd()).resolve()
        self.max_tokens = max_tokens
        self.max_tool_iterations = max_tool_iterations
        self.todo_reminder_interval = todo_reminder_interval
        self.max_history_messages = max_history_messages
        self.renderer = renderer or ConsoleRenderer()
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
    ) -> AgentRunResult:
        self._permission_interrupted = False
        tools = toolset.schemas()
        iterations_since_todo_update = 0

        for _ in range(self.max_tool_iterations):
            if before_model_request is not None:
                before_model_request(session)
            self._inject_background_events(session, toolset)
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

            self.renderer.start_thinking()
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
            for tool_call in assistant_message.tool_calls:
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

                permission_reason, hard_denied = self._check_permission(
                    tool_name,
                    permission_args,
                    session=session,
                )
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
                elif confirm_permission is not None:
                    allowed = confirm_permission(tool_name, arguments_json)
                else:
                    allowed = self._ask_user(
                        tool_name,
                        permission_args,
                        permission_reason,
                        session=session,
                    )

                if allowed:
                    output = toolset.dispatch(
                        tool_name,
                        arguments_json,
                        context=effective_tool_context,
                    )
                else:
                    output = {
                        "ok": False,
                        "error": f"Permission denied: {permission_reason}",
                    }
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

                if self._permission_interrupted:
                    self._compact_after_round(session)
                    return AgentRunResult(
                        ok=True,
                        content=None,
                        stopped_by_permission=True,
                    )

                if stop_after_tool is not None and stop_after_tool(tool_name, output):
                    should_continue = (
                        continue_after_tool(tool_name, output)
                        if continue_after_tool is not None
                        else False
                    )
                    if not should_continue:
                        self._compact_after_round(session)
                        return AgentRunResult(ok=True, content=None)

                if hard_denied or not allowed:
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
            *session.history,
        ]
        self.renderer.start_thinking("Preparing response")
        try:
            try:
                response = request_with_retry(
                    lambda: self.provider.client.chat.completions.create(
                        model=session.model,
                        messages=list(messages),
                        tools=[],
                        tool_choice="none",
                        max_tokens=self.max_tokens,
                    )
                )
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
            *session.history,
        ]

        def request(messages: list[dict[str, Any]], max_tokens: int) -> Any:
            return request_with_retry(
                lambda: self.provider.client.chat.completions.create(
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

    DENY_LIST = [
        "rm -rf /",
        "sudo",
        "shutdown",
        "reboot",
        "mkfs",
        "dd if=",
        "> /dev/sda",
    ]


    def _check_deny_list(self, args: dict[str, Any]) -> str | None:
        command = args.get("command", "")
        if not isinstance(command, str):
            return "Command must be a string"

        normalized = " ".join(command.lower().split())
        for pattern in self.DENY_LIST:
            if pattern in normalized:
                return f"Blocked '{pattern}' is on the deny list"
        return None

    PERMISSION_RULES = [
        {
            "tools": {"read_file", "write_file", "edit_file"},
            "check": "_path_outside_workspace",
            "message": "Path is outside the workspace",
        },
    ]

    def _path_outside_workspace(self, args: dict[str, Any]) -> bool:
        file_path = args.get("file_path", "")
        target = (self.workdir / file_path).resolve()
        return not target.is_relative_to(self.workdir)

    def _check_rules(self, tool_name: str, args: dict[str, Any]) -> str | None:
        for rule in self.PERMISSION_RULES:
            if tool_name not in rule["tools"]:
                continue
            # 在self对象上找到 rule["check"]名称的方法
            checker = getattr(self, rule["check"])
            if checker(args):
                return rule["message"]

        return None

    @staticmethod
    def _permission_target(args: dict[str, Any]) -> str:
        command = args.get("command")
        if isinstance(command, str):
            return command
        return json.dumps(args, ensure_ascii=False)

    @classmethod
    def _command_prefix(cls, command: str) -> tuple[str, ...]:
        try:
            tokens = shlex.split(command)
        except ValueError:
            return ()
        if not tokens:
            return ()
        return tuple(tokens[:2]) if len(tokens) > 1 else (tokens[0],)

    @classmethod
    def _contains_shell_operator(cls, command: str) -> bool:
        return not is_readonly_command(command)

    def _command_stays_in_workspace(
        self,
        args: dict[str, Any],
        tokens: list[str],
    ) -> bool:
        cwd = args.get("cwd", ".")
        if not isinstance(cwd, str):
            return False
        target = (self.workdir / cwd).resolve()
        if not target.is_relative_to(self.workdir):
            return False

        for token in tokens[1:]:
            if token == "/dev/null":
                continue
            path = Path(token)
            if path.is_absolute() or ".." in path.parts:
                return False
        return True

    def _is_auto_allowed_bash(
        self,
        args: dict[str, Any],
        session: Session | None,
    ) -> bool:
        command = args.get("command", "")
        if not isinstance(command, str):
            return False
        tokens = shell_tokens(command)
        if not tokens:
            return False
        if not tokens or not self._command_stays_in_workspace(args, tokens):
            return False

        prefix = self._command_prefix(command)
        if session is not None and prefix in session.approved_command_prefixes:
            return True
        if not is_readonly_command(command):
            return False
        return any(
            tuple(tokens[:len(allowed_prefix)]) == allowed_prefix
            for allowed_prefix in self.SAFE_BASH_PREFIXES
        )

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
        suffix = f" ({reason})" if reason else ""
        scope_suffix = f" [{scope}]" if scope else ""
        decision = "Allowed" if allowed else "Denied"
        self.renderer.permission_decision(
            tool_name,
            target,
            allowed=allowed,
            scope=scope,
            reason=reason,
        )

    def _ask_user(
        self,
        tool_name: str,
        args: dict[str, Any],
        _reason: str,
        session: Session | None = None,
    ) -> bool:
        question = self.renderer.permission_question(tool_name)

        answer, raw_tty = read_confirmation(question)

        allowed = answer in {"y", "yes", "a", "allow", "always"}
        session_scope = answer in {"a", "allow", "always"}
        if session_scope and session is not None:
            command = args.get("command")
            if isinstance(command, str):
                prefix = self._command_prefix(command)
                if prefix:
                    session.approved_command_prefixes.add(prefix)
        if raw_tty:
            sys.stdout.write("\n")
            sys.stdout.flush()
        elif sys.stdin.isatty() and sys.stdout.isatty():
            # input() has moved to the next line; erase its prompt and answer.
            self.renderer.clear_input_line()

        if answer == ESCAPE:
            self._permission_interrupted = True
            self.renderer.show_notice(
                "Permission prompt interrupted. Returning to the user prompt."
            )
            return False

        self._print_permission_decision(
            tool_name,
            args,
            allowed=allowed,
            scope="session" if session_scope else "once",
        )
        return allowed

    def _check_permission(
        self,
        name: str,
        args: dict[str, Any],
        session: Session | None = None,
    ) -> tuple[str | None, bool]:
        # L1 高危命令硬拒绝，不进入用户确认流程。
        if name == "run_bash":
            reason = self._check_deny_list(args)
            if reason:
                return reason, True

            if self._is_auto_allowed_bash(args, session):
                return None, False

            # 未命中严格只读 allowlist，或包含 shell 语法时必须确认。
            return "Shell command execution requires permission", False

        # L2 规则过滤
        return self._check_rules(name, args), False
