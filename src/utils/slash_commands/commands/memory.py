from src.utils.slash_commands.base import BaseCommand, CommandContext, CommandResult


class MemoryCommand(BaseCommand):
    name = "memory"
    description = "Enable or disable session memory"
    usage = "/memory [on|off|status]"

    def execute(self, args: list[str], context: CommandContext) -> CommandResult:
        if len(args) > 1:
            return CommandResult(f"Usage: {self.usage}")

        mode = args[0].lower() if args else "status"
        if mode == "on":
            context.session.memory_enabled = True
            if context.memory_selection_cache is not None:
                context.memory_selection_cache.set_enabled(
                    context.session.session_id,
                    True,
                )
            message = "Memory enabled for this session."
        elif mode == "off":
            context.session.memory_enabled = False
            if context.memory_selection_cache is not None:
                context.memory_selection_cache.set_enabled(
                    context.session.session_id,
                    False,
                )
            message = "Memory disabled for this session."
        elif mode == "status":
            state = "on" if context.session.memory_enabled else "off"
            return CommandResult(f"Memory is {state} for this session.")
        else:
            return CommandResult(f"Usage: {self.usage}")

        if (
            context.session_store is not None
            and context.session_store.exists(context.session.session_id)
        ):
            context.session_store.save(
                context.session,
                workdir=context.workdir,
            )
        return CommandResult(message)
