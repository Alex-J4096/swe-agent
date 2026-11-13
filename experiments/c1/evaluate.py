"""C1 adapter for the shared four-arm transfer protocol.

The original C1 driver and oracle remain available for historical replication.
This adapter adds strict file/baseline checks used by the new transfer batch.
"""
import json
from pathlib import Path
import re
import shlex

from experiments.c1 import fixture
from experiments.task_support import project_files


def target(branch):
    return {"stable": fixture.OLD, "changed": fixture.NEW}[branch]


def initial_sources():
    return {
        fixture.OLD: "timeout = 10\nport = 8080\n",
        fixture.NEW: "timeout = 10\nport = 8080\n",
        "service.py": fixture.service(fixture.OLD),
    }


def branch_sources(learned, branch):
    state = dict(learned)
    # Equal values prevent the observation's numeric output revealing the loader.
    state[fixture.NEW] = "timeout = 30\nport = 8080\n"
    state["service.py"] = fixture.service(target(branch))
    return state


def service_command(command, cwd: Path, repo: Path):
    """Recognize only the actual service script, with an optional local cd."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        parts = list(lexer)
    except ValueError:
        return False
    repo, cwd = repo.resolve(), cwd.resolve()
    if parts[:1] == ["cd"] and len(parts) >= 4 and parts[2] == "&&":
        cwd = (cwd / parts[1]).resolve()
        parts = parts[3:]
    if not cwd.is_relative_to(repo) or not parts:
        return False
    if not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(parts[0]).name):
        return False
    args = parts[1:]
    while args and args[0] in {"-B", "-I", "-u", "-E", "-s"}:
        args = args[1:]
    return len(args) == 1 and (cwd / args[0]).resolve() == repo / "service.py"


def verified_runtime(run, repo, expected):
    for event in run["events"]:
        if (event["event"] != "tool_result" or event.get("component") != "agent"
                or event.get("tool") != "run_bash"):
            continue
        args = json.loads(event["arguments"])
        output = event["output"]
        if (service_command(args.get("command", ""), repo / args.get("cwd", "."), repo)
                and output.get("ok") and output.get("exit_code", 0) == 0
                and output.get("stdout", "").strip() == str(expected)):
            return True
    return False


def grade(repo, branch, executor, *, baseline, goal="task"):
    if goal not in {"learn", "task"}:
        raise ValueError(goal)
    state = project_files(repo)
    if any(path.is_symlink() for path in repo.rglob("*")):
        return {"success": False, "strict_success": False, "error": "symlink_in_task"}
    active = target(branch)
    inactive = fixture.NEW if active == fixture.OLD else fixture.OLD
    result = fixture.grade(repo, executor, active, 30 if goal == "learn" else 60)
    intact = set(state) == set(baseline) == set(fixture.FILES)
    inactive_intact = state.get(inactive) == baseline.get(inactive)
    result.update(task_file_set_intact=intact, inactive_file_intact=inactive_intact)
    result["strict_success"] = bool(
        result["success"] and result["loader_intact"] and result["unrelated_fields_intact"]
        and intact and inactive_intact
    )
    return result


def first_action(before, after, branch):
    changed = fixture.diff(before, after)
    active = target(branch)
    inactive = fixture.NEW if active == fixture.OLD else fixture.OLD
    return ("both" if active in changed and inactive in changed else
            "correct" if active in changed else "inactive" if inactive in changed else
            "other" if changed else "none")
