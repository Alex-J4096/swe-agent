from src.utils.slash_commands.base import BaseCommand, CommandContext, CommandResult


class SessionsCommand(BaseCommand):
    name = "sessions"
    description = "List persisted sessions"
    usage = "/sessions [limit]"

    def execute(self, args: list[str], context: CommandContext) -> CommandResult:
        if len(args) > 1:
            return CommandResult(f"Usage: {self.usage}")
        if context.session_store is None:
            return CommandResult("Session persistence is not configured.")

        try:
            limit = int(args[0]) if args else 20
        except ValueError:
            return CommandResult(f"Usage: {self.usage}")
        if limit < 1 or limit > 100:
            return CommandResult("Session limit must be between 1 and 100.")

        summaries = context.session_store.list(limit)
        if not summaries:
            return CommandResult("No persisted sessions.")

        lines = ["Persisted sessions:"]
        for summary in summaries:
            marker = "*" if summary.session_id == context.session.session_id else " "
            title = f" — {summary.title}" if summary.title else ""
            lines.append(
                f"{marker} {summary.session_id} | {summary.model} | "
                f"{summary.message_count} messages{title} | {summary.updated_at}"
            )
        return CommandResult("\n".join(lines))
