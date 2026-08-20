"""Risk-based permission policy for tools and sandbox capability upgrades."""

from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from src.runtime.session import Session
from src.utils.shell import shell_tokens


PermissionOutcome = Literal["allow", "require_approval", "deny"]


@dataclass(frozen=True)
class PermissionDecision:
    outcome: PermissionOutcome
    reason: str | None = None
    risk: str = "low"
    capability: str | None = None
    scope_key: str | None = None


class PermissionPolicy:
    """Keep ordinary work in the sandbox and approve only capability upgrades."""

    HARD_DENY_PATTERNS = (
        "rm -rf /",
        "sudo",
        "shutdown",
        "reboot",
        "mkfs",
        "dd if=",
        "> /dev/sda",
    )
    NETWORK_PROGRAMS = frozenset(
        {
            "curl",
            "wget",
            "ssh",
            "scp",
            "sftp",
            "telnet",
            "nc",
            "ncat",
            "ftp",
        }
    )
    PACKAGE_PROGRAMS = frozenset(
        {"pip", "pip3", "npm", "pnpm", "yarn", "bun", "uv", "cargo", "go", "brew"}
    )
    PACKAGE_NETWORK_ACTIONS = frozenset(
        {"install", "add", "update", "upgrade", "sync", "download", "fetch"}
    )
    GIT_NETWORK_ACTIONS = frozenset(
        {"clone", "fetch", "pull", "push", "ls-remote", "submodule"}
    )
    DESTRUCTIVE_PROGRAMS = frozenset({"rm", "rmdir", "shred", "truncate"})
    DESTRUCTIVE_GIT_ACTIONS = frozenset(
        {"reset", "clean", "checkout", "restore", "rebase"}
    )
    BUILTIN_TOOLS = frozenset(
        {
            "run_bash",
            "read_file",
            "write_file",
            "edit_file",
            "todo_manager",
            "task_manager",
            "background_task",
            "load_skill",
            "rewind_context",
            "delegate_task",
            "propose_team_plan",
        }
    )

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir.resolve()

    @staticmethod
    def _segments(command: str) -> list[list[str]]:
        tokens = shell_tokens(command) or []
        segments: list[list[str]] = []
        current: list[str] = []
        for token in tokens:
            if token in {"|", "&&", "||", ";", "&", "|&"}:
                if current:
                    segments.append(current)
                    current = []
                continue
            if token in {">", ">>", "<", "<<", "<<<", "&>"}:
                continue
            current.append(token)
        if current:
            segments.append(current)
        return segments

    @staticmethod
    def _program(segment: list[str]) -> tuple[str, list[str]]:
        tokens = list(segment)
        while tokens and "=" in tokens[0] and not tokens[0].startswith(("/", "./")):
            name, _, _value = tokens[0].partition("=")
            if not name.replace("_", "").isalnum():
                break
            tokens.pop(0)
        if not tokens:
            return "", []
        return Path(tokens[0]).name.lower(), [token.lower() for token in tokens[1:]]

    def _requires_network(self, command: str) -> bool:
        for segment in self._segments(command):
            program, args = self._program(segment)
            if program in self.NETWORK_PROGRAMS:
                return True
            if program == "git" and args and args[0] in self.GIT_NETWORK_ACTIONS:
                return True
            if program in self.PACKAGE_PROGRAMS and any(
                action in args[:3] for action in self.PACKAGE_NETWORK_ACTIONS
            ):
                return True
        return False

    def _is_destructive(self, command: str) -> bool:
        for segment in self._segments(command):
            program, args = self._program(segment)
            if program in self.DESTRUCTIVE_PROGRAMS:
                return True
            if program == "git" and args and args[0] in self.DESTRUCTIVE_GIT_ACTIONS:
                return True
        return False

    @staticmethod
    def _legacy_approved(session: Session | None, command: str) -> bool:
        if session is None:
            return False
        try:
            tokens = shlex.split(command)
        except ValueError:
            return False
        prefix = tuple(tokens[:2]) if len(tokens) > 1 else tuple(tokens)
        return bool(prefix) and prefix in session.approved_command_prefixes

    @staticmethod
    def _scope_key(capability: str, command: str, cwd: str) -> str:
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = [command]
        # Preserve the executable/action shape without granting every invocation
        # of broad launchers such as `uv run` or `python -m`.
        if len(tokens) >= 3 and tokens[:2] in (["uv", "run"], ["python", "-m"], ["python3", "-m"]):
            family = tokens[:3]
        elif len(tokens) >= 3 and tokens[0] in {"npm", "pnpm", "yarn", "bun"}:
            family = tokens[:3]
        else:
            family = tokens[:2]
        payload = json.dumps(
            {"capability": capability, "family": family, "cwd": cwd},
            ensure_ascii=False,
            sort_keys=True,
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        label = quote(" ".join(family), safe="-._")
        location = quote(cwd, safe="-._/")
        return f"{capability}:{digest}:{label}@{location}"

    def evaluate(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        session: Session | None = None,
    ) -> PermissionDecision:
        if tool_name == "run_bash":
            command = args.get("command")
            if not isinstance(command, str):
                return PermissionDecision("deny", "Command must be a string", "critical")
            normalized = " ".join(command.lower().split())
            for pattern in self.HARD_DENY_PATTERNS:
                if pattern in normalized:
                    return PermissionDecision(
                        "deny",
                        f"Blocked '{pattern}' is on the deny list",
                        "critical",
                    )

            cwd = args.get("cwd", ".")
            if not isinstance(cwd, str):
                return PermissionDecision("deny", "cwd must be a string", "high")
            target = (self.workdir / cwd).resolve()
            if not target.is_relative_to(self.workdir):
                return PermissionDecision("deny", "cwd escapes workspace", "critical")

            if args.get("network_access") is True or self._requires_network(command):
                capability = "sandbox.network"
                scope_key = self._scope_key(capability, command, cwd)
                if session is not None and (
                    scope_key in session.approved_permission_scopes
                    or self._legacy_approved(session, command)
                ):
                    return PermissionDecision(
                        "allow",
                        "Session-approved sandbox network access",
                        "medium",
                        capability,
                        scope_key,
                    )
                return PermissionDecision(
                    "require_approval",
                    "Command requires sandbox network access",
                    "medium",
                    capability,
                    scope_key,
                )

            if self._is_destructive(command):
                capability = "workspace.destructive"
                scope_key = self._scope_key(capability, command, cwd)
                if session is not None and (
                    scope_key in session.approved_permission_scopes
                    or self._legacy_approved(session, command)
                ):
                    return PermissionDecision(
                        "allow",
                        "Session-approved destructive workspace command",
                        "high",
                        capability,
                        scope_key,
                    )
                return PermissionDecision(
                    "require_approval",
                    "Command may destructively modify the workspace",
                    "high",
                    capability,
                    scope_key,
                )

            return PermissionDecision("allow", "Confined to the shell sandbox")

        if tool_name in {"read_file", "write_file", "edit_file"}:
            file_path = args.get("file_path")
            if not isinstance(file_path, str):
                return PermissionDecision("deny", "file_path must be a string", "high")
            target = (self.workdir / file_path).resolve()
            if not target.is_relative_to(self.workdir):
                return PermissionDecision("deny", "Path is outside the workspace", "critical")
            return PermissionDecision("allow")

        if tool_name not in self.BUILTIN_TOOLS:
            scope_key = f"tool:{tool_name}"
            if session is not None and scope_key in session.approved_permission_scopes:
                return PermissionDecision(
                    "allow",
                    "Session-approved extension tool",
                    "medium",
                    f"tool.{tool_name}",
                    scope_key,
                )
            return PermissionDecision(
                "require_approval",
                "Extension tool has no built-in permission classification",
                "medium",
                f"tool.{tool_name}",
                scope_key,
            )

        return PermissionDecision("allow")
