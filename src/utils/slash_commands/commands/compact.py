from src.infrastructure.compact import compact_history
from src.utils.slash_commands.base import (
    BaseCommand,
    CommandContext,
    CommandResult,
)


MAX_COMPACT_FAILURES = 3


class CompactCommand(BaseCommand):
    name = "compact"
    description = "Summarize and compact the current session"
    usage = "/compact"

    def execute(
        self,
        args: list[str],
        context: CommandContext,
    ) -> CommandResult:
        if args:
            return CommandResult("Usage: /compact")

        if not context.session.history:
            return CommandResult("Session history is empty.")

        message_count = len(context.session.history)
        print(
            f"Starting conversation compaction ({message_count} messages)...",
            flush=True,
        )
        last_error = "Unknown compact failure."
        for _ in range(MAX_COMPACT_FAILURES):
            result = compact_history(
                context.session.history,
                context.provider.client,
                context.session.model,
            )
            if result.ok:
                return CommandResult(
                    f"Compacted {message_count} messages into one summary. "
                    f"Full transcript: {result.transcript_path}"
                )
            if result.error:
                last_error = result.error

        return CommandResult(
            f"Compact failed {MAX_COMPACT_FAILURES} consecutive times: "
            f"{last_error} Circuit breaker stopped this compact attempt."
        )
