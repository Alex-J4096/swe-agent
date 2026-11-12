"""Risk-based permission policy for tools and sandbox capability upgrades."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, unquote

from src.runtime.session import Session
from src.utils.shell import is_readonly_command, shell_tokens


PermissionOutcome = Literal["allow", "require_approval", "deny"]


@dataclass(frozen=True)
class PermissionDecision:
    outcome: PermissionOutcome
    reason: str | None = None
    risk: str = "low"
    capability: str | None = None
    scope_key: str | None = None
    capabilities: frozenset[str] = frozenset()
    reusable: bool = True

    @property
    def required_capabilities(self) -> frozenset[str]:
        return self.capabilities or frozenset({self.capability} if self.capability else ())


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
    def _reusable(command: str) -> bool:
        # Shell source is executable code. Opaque syntax and interpreter entry
        # points always need a fresh decision, rather than a reusable grant.
        if any(marker in command for marker in ("$", "`", "\n", "\r", ">", "<", "{", "}")):
            return False
        segments = PermissionPolicy._segments(command)
        opaque = {
            "bash", "sh", "zsh", "fish", "python", "python3", "node",
            "ruby", "perl", "powershell", "pwsh", "cmd", "eval", "exec",
            "source", ".", "env", "command", "nohup", "xargs",
        }
        if not segments:
            return False
        for segment in segments:
            program, args = PermissionPolicy._program(segment)
            program = program.removesuffix(".exe")
            if program in opaque or program.startswith("python"):
                return False
            if Path(program).suffix in {".py", ".sh", ".ps1", ".js", ".bat", ".cmd"}:
                return False
            if program in {"uv", "npm", "pnpm", "yarn", "bun", "npx"} and (
                program == "npx" or any(action in args[:3] for action in {"run", "exec", "x"})
            ):
                return False
            if program == "find" and any(flag in args for flag in {"-exec", "-execdir", "-delete", "-ok", "-okdir"}):
                return False
        return True

    @staticmethod
    def _scope_key(capability: str, command: str, cwd: str) -> str:
        # Preserve full shell source, including quoting and every command segment.
        # Only the directory is canonicalized; source whitespace can be meaningful.
        payload = json.dumps(
            {"version": 2, "capability": capability, "command": command, "cwd": cwd},
            ensure_ascii=False,
            sort_keys=True,
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        label = quote(command, safe="-._")
        location = quote(cwd, safe="-._/")
        return f"v2:{quote(capability, safe='-._+')}:{digest}:{label}@{location}"

    @staticmethod
    def describe_scope(scope: str) -> str:
        if not scope.startswith("v2:"):
            return "Legacy grant (no longer used; revoke it)"
        try:
            _, capability, _digest, target = scope.split(":", 3)
            command, location = target.rsplit("@", 1)
            return f"{unquote(capability)} | {unquote(command)} | cwd={unquote(location)}"
        except ValueError:
            return "Unrecognized grant (no longer used)"

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

            capabilities: set[str] = set()
            reasons: list[str] = []
            if args.get("network_access") is True or self._requires_network(command):
                capabilities.add("sandbox.network")
                reasons.append("Command requires sandbox network access")
            if self._is_destructive(command):
                capabilities.add("workspace.destructive")
                reasons.append("Command may destructively modify the workspace")
            reusable = self._reusable(command)
            if not reusable and not is_readonly_command(command):
                capabilities.add("shell.opaque")
                reasons.append("Shell source cannot be safely classified; approval is once only")
            if not capabilities:
                return PermissionDecision("allow", "Confined to the shell sandbox")
            authority = "+".join(sorted(capabilities))
            scope_key = self._scope_key(authority, command, str(target))
            approved = (
                reusable and session is not None
                and scope_key in session.approved_permission_scopes
            )
            return PermissionDecision(
                "allow" if approved else "require_approval",
                "; ".join(reasons),
                "high" if capabilities - {"sandbox.network"} else "medium",
                next(iter(capabilities)) if len(capabilities) == 1 else None,
                scope_key,
                frozenset(capabilities),
                reusable,
            )

        if tool_name in {"read_file", "write_file", "edit_file"}:
            file_path = args.get("file_path")
            if not isinstance(file_path, str):
                return PermissionDecision("deny", "file_path must be a string", "high")
            target = (self.workdir / file_path).resolve()
            if not target.is_relative_to(self.workdir):
                return PermissionDecision("deny", "Path is outside the workspace", "critical")
            return PermissionDecision("allow")

        if tool_name not in self.BUILTIN_TOOLS:
            capability = f"tool.{tool_name}"
            source = json.dumps(args, ensure_ascii=False, sort_keys=True)
            scope_key = self._scope_key(capability, source, str(self.workdir))
            approved = session is not None and scope_key in session.approved_permission_scopes
            return PermissionDecision(
                "allow" if approved else "require_approval",
                "Extension tool has no built-in permission classification",
                "medium", capability, scope_key,
            )

        return PermissionDecision("allow")
