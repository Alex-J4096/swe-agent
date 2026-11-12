from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from src.infrastructure.experiment_log import (
    configure_experiment_log,
    experiment_log_status,
    stop_experiment_log,
)
from src.utils.slash_commands.base import BaseCommand, CommandContext, CommandResult


class ExperimentCommand(BaseCommand):
    name = "experiment"
    description = "Start, stop, or inspect experiment logging"
    usage = "/experiment [on [path]|off|status]"

    def execute(self, args: list[str], context: CommandContext) -> CommandResult:
        mode = args[0].lower() if args else "status"
        if mode not in {"on", "off", "status"} or len(args) > (2 if mode == "on" else 1):
            return CommandResult(f"Usage: {self.usage}")
        current_path, run_id = experiment_log_status()
        if mode == "off":
            stop_experiment_log()
            return CommandResult("Experiment logging disabled.")
        if mode == "status" or current_path is not None:
            if current_path is None:
                return CommandResult("Experiment logging is off.")
            return CommandResult(f"Experiment logging is on.\nFile: {current_path}\nRun: {run_id}")
        workdir = (context.workdir or Path.cwd()).resolve()
        if len(args) == 2:
            path = Path(args[1]).expanduser()
            if not path.is_absolute():
                path = workdir / path
        else:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            path = workdir / ".swe-agent" / "experiments" / f"{timestamp}-{uuid4().hex[:8]}.jsonl"
        path = path.resolve()
        try:
            configure_experiment_log(path, workdir=str(workdir),
                                     session_id=context.session.session_id,
                                     model=context.session.model)
        except OSError as exc:
            stop_experiment_log()
            return CommandResult(f"Could not start experiment logging: {exc}")
        return CommandResult(f"Experiment logging enabled.\nFile: {path}")
