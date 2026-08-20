"""Inspect and resolve approval requests at the root prompt."""

from __future__ import annotations

from src.runtime.approval import ApprovalResponse
from src.utils.slash_commands.base import BaseCommand, CommandContext, CommandResult


class PermissionsCommand(BaseCommand):
    name = "permissions"
    aliases = ("approvals",)
    description = "List, approve, deny, or revoke permission scopes"
    usage = "/permissions [approve|deny|grants|revoke]"

    def execute(self, args: list[str], context: CommandContext) -> CommandResult:
        runtime = context.approval_runtime
        if runtime is None:
            return CommandResult("Approval runtime is unavailable.")
        if not args or args[0].lower() == "list":
            pending = runtime.list_pending()
            if not pending:
                return CommandResult("No pending approval requests.")
            lines = ["Pending approvals"]
            for request in pending:
                source = request.source.agent_id or request.source.kind
                lines.extend(
                    [
                        f"  {request.id}",
                        f"    Source: {source}",
                        f"    Tool:   {request.tool_name}",
                        f"    Target: {request.preview}",
                        f"    Reason: {request.reason}",
                    ]
                )
            lines.append(
                "Resolve with /permissions approve <id> [once|session] "
                "or /permissions deny <id> [feedback]"
            )
            return CommandResult("\n".join(lines))

        action = args[0].lower()
        if action == "approve":
            if len(args) not in {2, 3}:
                return CommandResult(
                    "Usage: /permissions approve <request-id> [once|session]"
                )
            scope = args[2].lower() if len(args) == 3 else "once"
            decisions = {
                "once": "approve_once",
                "session": "approve_session",
            }
            decision = decisions.get(scope)
            if decision is None:
                return CommandResult("Approval scope must be once or session.")
            if not runtime.resolve(args[1], ApprovalResponse(decision)):  # type: ignore[arg-type]
                return CommandResult(f"Pending approval not found: {args[1]}")
            return CommandResult(f"Approved {args[1]} [{scope}].")

        if action == "deny":
            if len(args) < 2:
                return CommandResult(
                    "Usage: /permissions deny <request-id> [feedback]"
                )
            feedback = " ".join(args[2:])
            if not runtime.resolve(args[1], ApprovalResponse("reject", feedback)):
                return CommandResult(f"Pending approval not found: {args[1]}")
            return CommandResult(f"Denied {args[1]}.")

        if action == "grants":
            grants = sorted(context.session.approved_permission_scopes)
            if not grants:
                return CommandResult("No Session permission grants.")
            return CommandResult("Session permission grants\n  " + "\n  ".join(grants))

        if action == "revoke":
            if len(args) != 2:
                return CommandResult("Usage: /permissions revoke <scope|all>")
            target = args[1]
            if target == "all":
                count = (
                    len(context.session.approved_permission_scopes)
                    + len(context.session.approved_command_prefixes)
                )
                context.session.approved_permission_scopes.clear()
                context.session.approved_command_prefixes.clear()
                return CommandResult(f"Revoked {count} permission grants.")
            if target not in context.session.approved_permission_scopes:
                return CommandResult(f"Permission scope not found: {target}")
            context.session.approved_permission_scopes.remove(target)
            return CommandResult(f"Revoked permission scope: {target}")

        return CommandResult(f"Usage: {self.usage}")
