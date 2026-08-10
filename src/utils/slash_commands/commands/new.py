from src.utils.slash_commands.commands.resume import _copy_session
from src.utils.slash_commands.base import BaseCommand, CommandContext, CommandResult


class NewSessionCommand(BaseCommand):
    name = "new"
    description = "Create a new persisted session"
    usage = "/new [title]"

    def execute(self, args: list[str], context: CommandContext) -> CommandResult:
        if len(args) > 1:
            return CommandResult(f"Usage: {self.usage}")
        if context.session_store is None:
            return CommandResult("Session persistence is not configured.")

        title = args[0] if args else None
        session = context.session_store.create(
            model=context.session.model,
            provider_id=context.session.provider_id,
            title=title,
            workdir=context.workdir,
        )
        _copy_session(context.session, session)
        return CommandResult(f"Created session {session.session_id}.")
