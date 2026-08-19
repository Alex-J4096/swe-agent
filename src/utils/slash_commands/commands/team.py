from src.utils.slash_commands.base import BaseCommand, CommandContext, CommandResult


class TeamCommand(BaseCommand):
    name = "team"
    description = "Start, approve, inspect, or stop an Agent Team"
    usage = "/team start <team-id> [worker-count]"

    def execute(self, args: list[str], context: CommandContext) -> CommandResult:
        controller = context.team_controller
        if controller is None:
            return CommandResult("Team runtime is not configured.")
        if not args or args[0] == "help":
            return CommandResult(
                "Usage:\n"
                "  /team start <team-id> [worker-count]\n"
                "  /team approve <plan-id>\n"
                "  /team status\n"
                "  /team shutdown"
            )

        action = args[0].lower()
        try:
            if action == "start":
                if len(args) not in {2, 3}:
                    return CommandResult(f"Usage: {self.usage}")
                count = int(args[2]) if len(args) == 3 else None
                runtime = controller.start_team(args[1], count)
                context.team_runtime = runtime
                return CommandResult(
                    f"Started team {runtime.team_id}. Lead must propose a plan."
                )

            if action == "approve":
                if len(args) != 2:
                    return CommandResult("Usage: /team approve <plan-id>")
                count = controller.approve_and_start(args[1])
                return CommandResult(f"Approved plan and started {count} task(s).")

            if action == "status":
                runtime = controller.runtime
                if runtime is None:
                    return CommandResult("No active team.")
                state = runtime.state()
                tasks = runtime.board.list_tasks()
                members = ", ".join(
                    f"{member.agent_id}={member.state.value}"
                    for member in state.members.values()
                )
                return CommandResult(
                    f"Team: {state.team_id}\n"
                    f"Plan: {state.plan.status.value if state.plan else 'none'}\n"
                    f"Shutdown: {state.shutdown.value}\n"
                    f"Members: {members or 'none'}\n"
                    f"Tasks: {len(tasks)}"
                )

            if action == "shutdown":
                controller.shutdown()
                context.team_runtime = None
                return CommandResult("Team stopped.")
        except (TypeError, ValueError) as exc:
            return CommandResult(str(exc))

        return CommandResult(f"Unknown team action: {action}")
