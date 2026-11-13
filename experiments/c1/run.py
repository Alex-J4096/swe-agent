"""Run from the agent checkout: python -m experiments.c1.run LAB [--check-only]."""
import argparse
from contextlib import chdir
from copy import deepcopy
import json
from pathlib import Path
import platform
import random
import secrets
import shlex
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

from main import (
    BUILTIN_PROVIDERS, MAX_TOKENS, MAX_TOOL_ITERATIONS, MODEL, prepare_turn_prompt,
)
from src.infrastructure.credential_service import CredentialService
from src.infrastructure.database import AppDatabase, default_database_path
from src.infrastructure.experiment_log import (
    configure_experiment_log, record_event, stop_experiment_log,
)
from src.infrastructure.memory import MemoryStore
from src.infrastructure.memory_selection_cache import MemorySelectionCache
from src.infrastructure.model_provider import Provider
from src.runtime.agent_runner import AgentRunner
from src.runtime.memory_selection_worker import MemorySelectionWorker
from src.runtime.memory_worker import MemoryWorker
from src.runtime.sandbox import SandboxPolicy
from src.runtime.session import Session
from src.tools.file.edit_file import EditFileTool
from src.tools.file.read_file import ReadFileTool
from src.tools.file.write_file import WriteFileTool
from src.tools.shell.bash import BashTool
from src.tools.todo_manager.todo_manager import TodoManager
from src.tools.toolset import ToolRegistry, ToolSet
from src.utils.shell import is_readonly_command

from .fixture import FILES, LEARN, NEW, OBSERVE, OLD, TASK, diff, files, fixture, grade, save, service
from .sandbox import ExperimentSandbox

ROOT = Path(__file__).resolve().parents[2]
TARGETS = {"stable": OLD, "changed": NEW}
SERVICE_COMMANDS = {
    "python service.py", "python3 service.py", "python ./service.py",
    "python3 ./service.py", "python3 -I -B service.py",
}


class Transcript(list):
    """Capture fresh messages at append time, before runner compaction mutates history."""
    def __init__(self):
        super().__init__()
        self.original = []

    def append(self, message):
        self.original.append(deepcopy(message))
        super().append(message)


def run_turn(lab, label, repo, sandbox, provider, prompt, memory, prior):
    log = lab / f"{label}.jsonl"
    configure_experiment_log(log, stage=label, model=MODEL, workdir=str(repo))
    history = Transcript()
    session = Session(model=MODEL, history=history, memory_enabled=memory, provider_id="siliconflow")
    tools = ToolSet(registry=ToolRegistry([
        ReadFileTool(repo), WriteFileTool(repo), EditFileTool(repo),
        BashTool(repo, sandbox), TodoManager(),
    ]))
    store = MemoryStore(repo, client=provider.client, model_getter=lambda: session.model)
    cache = MemorySelectionCache()
    worker = MemoryWorker(store)
    selector = MemorySelectionWorker(store, cache)
    state = files(repo)

    def measure(name, output):
        nonlocal state
        current = files(repo)
        record_event("workspace_delta", tool=name, changes=diff(state, current))
        state = current

    def approve(name, arguments):
        if name != "run_bash":
            return False
        # Grant the ordinary runtime check only. All other approval-gated commands
        # receive the same refusal in every condition.
        args = json.loads(arguments)
        return (not args.get("network_access") and args.get("command", "").strip()
                in SERVICE_COMMANDS)

    try:
        history.append({"role": "user", "content": prompt})
        system = prepare_turn_prompt(session, tools, repo, store, cache)
        result = AgentRunner(
            provider, repo, MAX_TOKENS, MAX_TOOL_ITERATIONS, prior_messages=prior,
        ).run(session, tools, system, confirm_permission=approve, on_tool_result=measure)
        record_event("turn_finished", ok=result.ok, permission_stop=result.stopped_by_permission,
                     error=result.error if result.error and result.error.startswith("Stopped after") else None)
        if memory:
            worker.submit(session.history, session.session_id)
            selector.submit(session.session_id, session.history)
    finally:
        worker.shutdown(wait=True)
        selector.shutdown(wait=True)
        stop_experiment_log()
        save(lab / f"{label}.history.json", history.original)
    events = [json.loads(line) for line in log.read_text().splitlines()]
    requests = [e for e in events if e["event"] == "model_request" and e["component"] == "agent"]
    expected = [*prior, {"role": "user", "content": prompt}]
    if not requests or any(e["request"]["messages"][1:1+len(expected)] != expected for e in requests):
        raise RuntimeError(f"{label}: submitted history mismatch; see {log}")
    return events


def usage(events, component):
    requests = [e for e in events if e.get("component") == component and e["event"] == "model_request"]
    replies = {e["request_id"]: e["response"] for e in events if e["event"] == "model_response"}
    totals = {}
    for field in ("prompt_tokens", "completion_tokens"):
        values = [(replies.get(e["request_id"], {}).get("usage") or {}).get(field) for e in requests]
        totals[field] = sum(values) if all(isinstance(n, int) for n in values) else None
    return totals


def successful_shell_read(tool):
    if tool["tool"] == "read_file":
        return bool(tool["output"].get("ok"))
    if tool["tool"] != "run_bash" or not tool["output"].get("ok"):
        return False
    return is_readonly_command(json.loads(tool["arguments"]).get("command", ""))


def summarize(events, target):
    tools = [e for e in events if e["event"] == "tool_result"]
    first = next((e for e in events if e["event"] == "workspace_delta" and e["changes"]), None)
    changed = list(first["changes"]) if first else []
    if OLD in changed and NEW in changed:
        action = "both"
    elif target in changed:
        action = "correct"
    elif (NEW if target == OLD else OLD) in changed:
        action = "inactive"
    else:
        action = "other" if changed else "none"
    before = tools
    if first:
        # The result of the mutating call is logged immediately before its delta.
        before = [e for e in tools if e["sequence"] < first["sequence"]][:-1]
    return {
        "run_id": events[0]["run_id"], "first_files": changed, "first_action": action,
        "delta_sequence": first["sequence"] if first else None,
        "reads_before": sum(successful_shell_read(e) for e in before),
        "tools": len(tools), "agent": usage(events, "agent"), "memory": usage(events, "memory"),
        "provider_models": sorted({e["response"]["model"] for e in events if e["event"] == "model_response"}),
        "turn": next(e for e in events if e["event"] == "turn_finished"),
    }


def observe_evidence(events, target):
    for e in events:
        if e["event"] != "model_request" or e["component"] != "agent":
            continue
        for m in e["request"]["messages"]:
            if m["role"] == "tool":
                out = json.loads(m["content"]) if m["content"].startswith("{") else {}
                if out.get("ok") and f'with_name("{target}")' in (out.get("content", "") + out.get("stdout", "")):
                    return e["sequence"]
    return None


def preflight(lab, repo, sandbox, db):
    marker = lab / "isolation-check"
    marker.write_text("C1")
    # Probe real reads, including the fallback CLI database, without printing any contents.
    paths = [marker, ROOT / "main.py"]
    if db.exists():
        paths.append(db)
    for path in paths:
        check = sandbox.execute(f"cat {shlex.quote(str(path))} >/dev/null", cwd=repo,
                                timeout_seconds=10, policy=SandboxPolicy(repo))
        if check.exit_code == 0:
            raise RuntimeError(f"Sandbox could read {path}")
    controls = []
    for target in TARGETS.values():
        fixture(repo, target, 30)
        for chosen in (OLD, NEW):
            (repo / chosen).write_text("timeout = 60\nport = 8080\n")
            result = grade(repo, sandbox, target, 60)
            expected = "60" if chosen == target else "30"
            if result["runtime"]["exit_code"] or result["runtime"]["stdout"] != expected:
                raise RuntimeError(f"Independent control failed: {result['runtime']}")
            controls.append({"target": target, "edited": chosen, **result})
            fixture(repo, target, 30)
    save(lab / "controls.json", controls)


def copy_memory(source, dest):
    relative = Path(".swe-agent/memory")
    if (source / relative).is_dir():
        shutil.copytree(source / relative, dest / relative)


def snapshot(repo, dest):
    dest.mkdir()
    for name in FILES:
        shutil.copyfile(repo / name, dest / name)
    copy_memory(repo, dest)


def run(lab, check_only, seed, exposure_pair=False, preparation=None):
    lab.mkdir(parents=True, exist_ok=True)
    if any(lab.iterdir()):
        raise ValueError("Choose a new empty LAB directory to keep previous runs intact.")
    order = [(branch, condition) for branch in TARGETS for condition in ("none", "full", "default")]
    if exposure_pair:
        order *= 2
    random.Random(seed).shuffle(order)
    db = default_database_path().expanduser().resolve()
    sandbox = ExperimentSandbox([lab, ROOT, db, db.parent / (db.name+"-wal"), db.parent / (db.name+"-shm")])
    manifest = {"task": "C1", "mode": "controls" if check_only else "live", "seed": seed,
                "exposure_pair": exposure_pair, "order": order, "platform": platform.platform(), "python": sys.version,
                "model": MODEL, "backend": sandbox.backend_name, "max_tokens": MAX_TOKENS,
                "max_tool_iterations": MAX_TOOL_ITERATIONS, "prompts": [LEARN, OBSERVE, TASK],
                "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()}
    save(lab / "manifest.json", manifest)
    driver_source = lab / "driver-source"
    driver_source.mkdir()
    for source in Path(__file__).resolve().parent.glob("*.py"):
        shutil.copyfile(source, driver_source / source.name)
    (lab / "agent-working.patch").write_bytes(subprocess.check_output(
        ["git", "diff", "HEAD", "--", "main.py", "src"], cwd=ROOT,
    ))
    with TemporaryDirectory(prefix="agent-work-") as work:
        repo = Path(work).resolve() / "repo"
        repo.mkdir()
        preflight(lab, repo, sandbox, db)
        print("C1 sandbox and four runtime controls passed", flush=True)
        if check_only:
            return
        profile = BUILTIN_PROVIDERS[0]
        database = AppDatabase(db)
        try:
            credential = CredentialService(database).resolve(profile)
        finally:
            database.close()
        if not credential.token:
            raise RuntimeError("Configure SiliconFlow in the agent with /keys.")
        provider = Provider(profile.name, credential.token, profile.base_url)
        if preparation:
            if not exposure_pair:
                raise ValueError("Reused preparation requires an exposure pair")
            observations = json.loads((preparation / "observations.json").read_text())
            learned = json.loads((preparation / "learn-grade.json").read_text())
            if not all(observations.values()) or not (learned["success"] and learned["agent_verification"]):
                raise ValueError("Preparation lacks verified learning or observation evidence")
            for branch in TARGETS:
                shutil.copytree(preparation / branch, lab / branch)
            for name in ("learn.history.json", "observe-stable.history.json", "observe-changed.history.json", "observations.json", "learn-grade.json"):
                shutil.copyfile(preparation / name, lab / name)
            manifest["preparation"] = str(preparation.resolve())
            save(lab / "manifest.json", manifest)
        else:
            fixture(repo, OLD, 10)
            with chdir(repo):
                events = run_turn(lab, "learn", repo, sandbox, provider, LEARN, True, [])
            learned = grade(repo, sandbox, OLD, 30)
            verified = any(e["event"] == "tool_result" and e["tool"] == "run_bash"
                           and json.loads(e["arguments"]).get("command", "").strip() in SERVICE_COMMANDS
                           and e["output"].get("ok") and e["output"].get("stdout", "").strip() == "30"
                           for e in events)
            save(lab / "learn-grade.json", {**learned, "agent_verification": verified})
            if not (learned["success"] and learned["loader_intact"] and learned["unrelated_fields_intact"]
                    and verified and learned["config_values"].get(OLD) == 30):
                raise RuntimeError("Learning prerequisite failed. Inspect learn.jsonl before starting a new run.")
            snapshot(repo, lab / "seed")
            observations = {}
            for branch, target in TARGETS.items():
                shutil.rmtree(repo)
                shutil.copytree(lab / "seed", repo)
                (repo / NEW).write_text("timeout = 30\nport = 8080\n")
                (repo / "service.py").write_text(service(target))
                initial = files(repo)
                label = f"observe-{branch}"
                with chdir(repo):
                    events = run_turn(lab, label, repo, sandbox, provider, OBSERVE, True, [])
                if files(repo) != initial or any(e["event"] == "workspace_delta" and e["changes"] for e in events):
                    raise RuntimeError(f"{label}: task files changed during observation; inspect the log")
                observations[branch] = observe_evidence(events, target)
                save(lab / "observations.json", observations)
                snapshot(repo, lab / branch)
        rows = []
        counts = {}
        for branch, condition in order:
            label = f"{branch}-{condition}"
            if exposure_pair:
                counts[label] = counts.get(label, 0) + 1
                label += f"-{counts[label]}"
            shutil.rmtree(repo)
            repo.mkdir()
            for name in FILES:
                shutil.copyfile(lab / branch / name, repo / name)
            if exposure_pair:
                (repo / "service.py").write_text(service(NEW))
            if condition == "default":
                copy_memory(lab / branch, repo)
            initial = files(repo)
            print(f"Running {label}", flush=True)
            prior = []
            if condition == "full":
                for stage in ("learn", f"observe-{branch}"):
                    prior.extend(json.loads((lab / f"{stage}.history.json").read_text()))
            with chdir(repo):
                events = run_turn(lab, label, repo, sandbox, provider, TASK,
                                     condition == "default", prior)
            target = NEW if exposure_pair else TARGETS[branch]
            delta = diff(initial, files(repo))
            row = {"task": "C1", "branch": branch, "condition": condition, "model": MODEL,
                   "log": label+".jsonl", "observed_evidence_sequence": observations[branch],
                   **summarize(events, target), **grade(repo, sandbox, target, 60),
                   "final_diff": delta, "inactive_changed": (NEW if target == OLD else OLD) in delta}
            rows.append(row)
            save(lab / "results.json", rows)
            print(label, row["first_action"], row["runtime"]["stdout"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lab", type=Path)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--exposure-pair", action="store_true", help="Observe old/new loader, then test both on new loader twice per condition.")
    parser.add_argument("--preparation", type=Path, help="Reuse verified C1 learning and observation snapshots.")
    args = parser.parse_args()
    run(args.lab.resolve(), args.check_only, args.seed if args.seed is not None else secrets.randbits(32), args.exposure_pair, args.preparation)
