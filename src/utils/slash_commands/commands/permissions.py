"""Inspect and resolve approval requests at the root prompt."""

from __future__ import annotations

from src.runtime.approval import ApprovalResponse
from src.runtime.permissions import PermissionPolicy
from src.utils.slash_commands.base import BaseCommand, CommandContext, CommandResult


class PermissionsCommand(BaseCommand):
    name = "permissions"
    aliases = ("approvals",)
    description = "List, approve, deny, or revoke permission scopes"
    usage = "/permissions [approve|deny|stop|grants|revoke]"

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
                        f"    Risk: {request.risk}",
                        f"    Directory: {request.cwd or '.'}",
                        f"    Permissions: {', '.join(request.capabilities) or request.capability or 'none'}",
                        f"    Reuse: {'exact operation; current loaded session only' if request.reusable else 'once only'}",
                    ]
                )
            lines.append(
                "Resolve with /permissions approve <id> [once|turn|session], "
                "/permissions deny <id> [feedback], or /permissions stop <id>"
            )
            return CommandResult("\n".join(lines))

        action = args[0].lower()
        if action == "approve":
            if len(args) not in {2, 3}:
                return CommandResult(
                    "Usage: /permissions approve <request-id> [once|turn|session]"
                )
            scope = args[2].lower() if len(args) == 3 else "once"
            decisions = {
                "once": "approve_once",
                "turn": "approve_turn",
                "session": "approve_session",
            }
            decision = decisions.get(scope)
            if decision is None:
                return CommandResult("Approval scope must be once, turn, or session.")
            request = runtime.get_request(args[1])
            if request is not None and not request.reusable and scope != "once":
                return CommandResult("This operation supports once only; no reusable grant was created.")
            if not runtime.resolve(args[1], ApprovalResponse(decision)):  # type: ignore[arg-type]
                return CommandResult(f"Pending approval not found: {args[1]}")
            return CommandResult(f"Approved {args[1]} [{scope}].")

        if action in {"deny", "stop"}:
            if len(args) < 2:
                return CommandResult(
                    f"Usage: /permissions {action} <request-id> [feedback]"
                )
            feedback = " ".join(args[2:])
            if not runtime.resolve(args[1], ApprovalResponse("stop" if action == "stop" else "reject", feedback)):
                return CommandResult(f"Pending approval not found: {args[1]}")
            return CommandResult(f"{'Stopped' if action == 'stop' else 'Denied; agent may adjust its approach:'} {args[1]}.")

        if action == "grants":
            grants = sorted(context.session.approved_permission_scopes)
            if not grants:
                return CommandResult("No Session permission grants.")
            lines = ["Session permission grants (cleared on unload/resume; not permanent rules)"]
            for number, scope in enumerate(grants, 1):
                lines.append(f"  {number}. {PermissionPolicy.describe_scope(scope)}")
            lines.append("Revoke with /permissions revoke <number|scope|all> (numbers reflect this list).")
            return CommandResult("\n".join(lines))

        if action == "revoke":
            if len(args) != 2:
                return CommandResult("Usage: /permissions revoke <number|scope|all>")
            target = args[1]
            if target.isdigit():
                grants = sorted(context.session.approved_permission_scopes)
                index = int(target) - 1
                if not 0 <= index < len(grants):
                    return CommandResult("Grant number not found; run /permissions grants again.")
                target = grants[index]
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
