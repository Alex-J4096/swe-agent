"""C1/A1/T1 transfer: shared learning/observation, four fresh-session arms."""
import argparse
import ast
from contextlib import chdir
from io import StringIO
import json
from pathlib import Path
import platform
import random
import re
import shlex
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import traceback
import zipfile
import hashlib

from experiments.a1 import fixture as a1
from experiments.a1.evaluate import grade as grade_a1
from experiments.c1 import run as c
from experiments.c1 import fixture as c1
from experiments.c1 import evaluate as c1_eval
from experiments.task_support import hashes, project_files, save, write_project
from experiments.t1 import fixture as t1
from experiments.t1.evaluate import TraceExecutor, command_entry, grade as grade_t1
from src.ui.console import ConsoleRenderer

ROOT = Path(__file__).resolve().parents[1]
CONDITIONS = ("none", "full", "default", "revise")
TASKS = ("c1", "a1", "t1")
PROTOCOL = "transfer-four-arm-v4"
REPORT_INSTRUCTION = ('\n最后给出一个 JSON 对象，字段为 tests_run（实际执行数量）、'
                      'failure_ids（失败用例完整 ID 数组）、exit_code（退出码）。')


def prompt(task, stage):
    module = {"c1": c1, "a1": a1, "t1": t1}[task]
    text = getattr(module, stage.upper())
    if task == "c1" and stage == "learn":
        text += ("\n只修改服务实际使用的配置文件，不要同步修改未使用的配置文件。"
                 "保持启动代码、无关配置字段和现有文件集合不变。")
    if task == "c1" and stage == "observe":
        text = ("请读取服务启动代码，说明启动时实际加载哪个配置文件。"
                "请实际执行 `python3 service.py`，根据执行输出报告当前生效的超时值和命令退出状态。"
                "不要修改任何文件。")
    if task == "a1" and stage != "observe":
        text += ("\n保持现有应用依赖装配、订阅主题和通知接口不变，只修改订单业务流程。"
                 "每次事件只调用当前已接入的一个后端，不向未接入的后端同时发送。")
    return text + REPORT_INSTRUCTION if task == "t1" and stage != "observe" else text


def parse_test_report(text):
    decoder = json.JSONDecoder()
    found = None
    for match in re.finditer(r"\{", text):
        try:
            item, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if (isinstance(item, dict) and type(item.get("tests_run")) is int
                and type(item.get("exit_code")) is int and isinstance(item.get("failure_ids"), list)
                and all(isinstance(value, str) for value in item["failure_ids"])):
            found = item
    return found


def migrate_a1(learned):
    """Retain learned code; change only app wiring and the creation method."""
    result = dict(learned)
    result["app.py"] = a1.sources("changed")["app.py"]

    def method(text):
        cls = next(n for n in ast.parse(text).body if isinstance(n, ast.ClassDef) and n.name == "OrderService")
        return next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "create_order")

    text = learned["order_service.py"]
    old = method(text)
    replacement = a1.sources("changed")["order_service.py"]
    new = method(replacement)
    lines = text.splitlines(keepends=True)
    lines[old.lineno - 1:old.end_lineno] = replacement.splitlines(keepends=True)[new.lineno - 1:new.end_lineno]
    result["order_service.py"] = "".join(lines)
    return result


def a1_invocations(command, cwd, repo):
    repo = repo.resolve()
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        parts = list(lexer)
    except ValueError:
        return None
    if any(any(symbol in part for symbol in ("$", chr(96))) for part in parts):
        return None
    segments = [[]]
    for part in parts:
        if part in {"&&", ";"}:
            if not segments[-1]:
                return None
            segments.append([])
        else:
            segments[-1].append(part)
    if not segments[-1]:
        return None
    invocations = []
    for segment in segments:
        if segment[0] == "cd" and len(segment) == 2:
            cwd = (cwd / segment[1]).resolve()
            if not cwd.is_relative_to(repo):
                return None
        elif segment[0] == "echo":
            if any(part in {"|", "||", ">", ">>", "<", "&", "(", ")"} for part in segment):
                return None
        else:
            if not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(segment[0]).name):
                return None
            args = segment[1:]
            while args and args[0] in {"-B", "-I", "-u", "-E", "-s"}:
                args = args[1:]
            if not (len(args) == 3 and (cwd / args[0]).resolve() == repo / "app.py"
                    and args[1] in {"create", "cancel"} and re.fullmatch(r"[\w-]+", args[2])):
                return None
            invocations.append((args[1], args[2]))
    return invocations or None


def approved_command(task, command, cwd, repo):
    if task == "c1":
        return c1_eval.service_command(command, cwd, repo)
    if task == "t1":
        return command_entry(command, cwd, repo) is not None
    if task == "a1":
        return a1_invocations(command, cwd, repo) is not None
    raise ValueError(task)


def restore(repo, state, memory_source=None):
    if repo.exists():
        shutil.rmtree(repo)
    write_project(repo, state)
    if memory_source:
        c.copy_memory(memory_source, repo)


def snapshot(repo, destination):
    write_project(destination, project_files(repo))
    c.copy_memory(repo, destination)


def turn(directory, repo, sandbox, provider, task, branch, instruction, *, memory, prior=(),
         mode="default", maintain=True):
    directory.mkdir(parents=True, exist_ok=True)
    c.configure_experiment_log(directory / "turn.jsonl", task=task, phase=directory.name,
                               model=c.MODEL, memory_maintenance=mode, workdir=str(repo))
    started = time.monotonic()
    history = c.Transcript()
    session = c.Session(model=c.MODEL, history=history, memory_enabled=memory, provider_id="siliconflow")
    trace = TraceExecutor(sandbox, repo, branch, directory / "commands.jsonl") if task == "t1" else None
    shell = c.BashTool(repo, trace or sandbox)
    if task == "c1":
        shell.description += (" Validate using python3 service.py (ordinary Python isolation flags"
                              " and a workspace-local cd prefix are preauthorized)."
                              " Extra verification scripts and compound commands are not preauthorized.")
    if task == "a1":
        shell.description += (" Validation uses the project's existing CLI: python3 app.py create ORDER-17"
                              " or python3 app.py cancel ORDER-17. Workspace-local cd and sequences of"
                              " these CLI commands with echo separators are preauthorized."
                              " Arbitrary Python snippets and extra verification scripts are not preauthorized.")
    tools = c.ToolSet(registry=c.ToolRegistry([
        c.ReadFileTool(repo), c.WriteFileTool(repo), c.EditFileTool(repo),
        shell, c.TodoManager(),
    ]))
    store = c.MemoryStore(repo, client=provider.client, model=c.MODEL, maintenance_mode=mode)
    cache = c.MemorySelectionCache()
    worker = c.MemoryWorker(store)
    selector = c.MemorySelectionWorker(store, cache)
    state = project_files(repo)
    first_state = None

    def measure(name, output):
        nonlocal state, first_state
        current = project_files(repo)
        changes = c.diff(state, current)
        c.record_event("workspace_delta", tool=name, changes=changes)
        if changes and first_state is None:
            first_state = current
        state = current

    def approve(name, arguments):
        args = json.loads(arguments)
        cwd = (repo / args.get("cwd", ".")).resolve()
        return (name == "run_bash" and not args.get("network_access")
                and approved_command(task, args.get("command", ""), cwd, repo))

    try:
        with chdir(repo):
            history.append({"role": "user", "content": instruction})
            system = c.prepare_turn_prompt(session, tools, repo, store, cache)
            result = c.AgentRunner(provider, repo, c.MAX_TOKENS, c.MAX_TOOL_ITERATIONS,
                                   prior_messages=list(prior),
                                   renderer=ConsoleRenderer(output=StringIO(), use_color=False)).run(
                session, tools, system, confirm_permission=approve, on_tool_result=measure)
            c.record_event("turn_finished", ok=result.ok, permission_stop=result.stopped_by_permission,
                           error=result.error, final_content=result.content)
            save(directory / "maintenance-input.json", list(session.history))
            if memory and maintain:
                worker.submit(session.history, session.session_id)
                selector.submit(session.session_id, session.history)
    finally:
        worker.shutdown(wait=True)
        selector.shutdown(wait=True)
        c.stop_experiment_log()
        save(directory / "history.json", history.original)
    events = [json.loads(line) for line in (directory / "turn.jsonl").read_text().splitlines()]
    requests = [e for e in events if e["event"] == "model_request" and e["component"] == "agent"]
    expected = [*prior, {"role": "user", "content": instruction}]
    if not requests or any(e["request"]["messages"][1:len(expected)+1] != expected for e in requests):
        raise RuntimeError("Imported history or current task changed in submitted messages")
    if first_state is not None:
        write_project(directory / "first-write", first_state)
    save(directory / "final-files.json", project_files(repo))
    snapshot(repo, directory / "after")
    save(directory / "turn-result.json", {"ok": result.ok, "content": result.content,
                                         "elapsed_seconds": time.monotonic() - started})
    return {"events": events, "content": result.content or "", "records": trace.records if trace else [],
            "first_state": first_state, "history": history.original,
            "maintenance_input": list(session.history), "session_id": session.session_id}


def test_grade(repo, branch, run):
    report = parse_test_report(run["content"])
    grade = grade_t1(repo, branch, run["records"], report["failure_ids"] if report else [])
    complete = bool(report and report["tests_run"] == len(grade["expected_case_ids"])
                    and report["exit_code"] == 1)
    grade.update(reported_summary=report, report_complete=complete)
    grade["success"] &= complete
    grade["strict_success"] &= complete
    return grade


def verified_creation(run, repo):
    for e in run["events"]:
        if e["event"] != "tool_result" or e.get("component") != "agent" or e["tool"] != "run_bash":
            continue
        args = json.loads(e["arguments"])
        invocations = a1_invocations(args.get("command", ""), (repo / args.get("cwd", ".")).resolve(), repo)
        if not invocations or not e["output"].get("ok"):
            continue
        for line in e["output"].get("stdout", "").splitlines():
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict) and any(
                action == "create" and data.get("events") == [{"event": "order.created", "order_id": order_id}]
                for action, order_id in invocations
            ):
                return True
    return False


def evidence_sequence(run, marker, source_path=None):
    """Require the source evidence to reach a subsequent foreground model request."""
    for e in run["events"]:
        if e["event"] != "model_request" or e.get("component") != "agent":
            continue
        for message in e["request"]["messages"]:
            if message.get("role") != "tool":
                continue
            try:
                output = json.loads(message["content"])
            except (ValueError, TypeError):
                continue
            source_matches = source_path is None or source_path in output.get("path", "")
            if not source_matches:
                for previous in e["request"]["messages"]:
                    for call in previous.get("tool_calls") or []:
                        if call["id"] == message.get("tool_call_id"):
                            source_matches = source_path in call["function"]["arguments"]
            if source_matches and output.get("ok") and marker in (output.get("content", "") + output.get("stdout", "")):
                return e["sequence"]
    return None


def maintain(directory, repo, provider, run, mode):
    directory.mkdir(parents=True)
    c.configure_experiment_log(directory / "maintenance.jsonl", phase="observation-maintenance",
                               model=c.MODEL, memory_maintenance=mode)
    store = c.MemoryStore(repo, client=provider.client, model=c.MODEL, maintenance_mode=mode)
    worker = c.MemoryWorker(store)
    try:
        worker.submit(run["maintenance_input"], run["session_id"]).result()
    finally:
        worker.shutdown(wait=True)
        c.stop_experiment_log()
    snapshot(repo, directory / "after")
    save(directory / "input.json", run["maintenance_input"])
    events = [json.loads(line) for line in (directory / "maintenance.jsonl").read_text().splitlines()]
    cost = c.usage(events, "memory")
    save(directory / "cost.json", cost)
    return cost


def prepare(task, lab, repo, sandbox, provider):
    directory = lab / task
    directory.mkdir()
    initial = (c1_eval.initial_sources() if task == "c1" else
               a1.sources("stable", "learn") if task == "a1" else t1.sources("stable"))
    restore(repo, initial)
    learning = turn(directory / "learn", repo, sandbox, provider, task, "stable", prompt(task, "learn"), memory=True)
    if task == "c1":
        grade = c1_eval.grade(repo, "stable", sandbox, baseline=initial, goal="learn")
        verified = c1_eval.verified_runtime(learning, repo, 30)
        grade["agent_runtime_verification"] = verified
        accepted = grade["strict_success"] and verified
    elif task == "a1":
        grade = grade_a1(repo, "stable", sandbox, goal="create", stage="learn")
        cancellation = grade_a1(repo, "stable", sandbox)
        verified = verified_creation(learning, repo)
        grade.update(agent_runtime_verification=verified, cancellation_still_missing=cancellation["action"] == "none")
        accepted = grade["strict_success"] and verified and grade["cancellation_still_missing"]
    else:
        grade = test_grade(repo, "stable", learning)
        accepted = grade["strict_success"]
    save(directory / "learn-grade.json", grade)
    if not accepted:
        raise RuntimeError(f"{task}: learning prerequisite failed; no retry in this batch")
    learned = project_files(repo)
    notes = c.MemoryStore(repo).list_visible_memories()
    facts = [note for note in notes if note["type"] == "project" and note["scope"] == "project"]
    save(directory / "learning-memories.json", {
        "notes": notes, "project_fact_count": len(facts),
        "related_old_memory": None if notes else False,
        "review": "Review relevance against the learned target; do not inject missing notes.",
    })
    costs = {"learning": {"agent": c.usage(learning["events"], "agent"),
                          "memory": c.usage(learning["events"], "memory")},
             "observation": {}, "maintenance": {}}
    save(directory / "costs.json", costs)
    branches = {}
    for branch in ("stable", "changed"):
        if task == "c1":
            state = c1_eval.branch_sources(learned, branch)
        elif branch == "stable":
            state = learned
        elif task == "a1":
            state = migrate_a1(learned)
        else:
            state = dict(learned)
            state[".github/workflows/tests.yml"] = t1.sources(branch)[".github/workflows/tests.yml"]
        restore(repo, state, directory / "learn/after")
        save(directory / f"{branch}-migration.json", c.diff(learned, state))
        observed = turn(directory / f"observe-{branch}", repo, sandbox, provider, task, branch,
                        prompt(task, "observe"), memory=True, maintain=False)
        if project_files(repo) != state or observed["first_state"] is not None:
            raise RuntimeError(f"{task}/{branch}: observation changed task files")
        if task == "c1":
            marker, source = f'with_name("{c1_eval.target(branch)}")', "service.py"
        elif task == "a1":
            marker = 'legacy.subscribe(events.append)' if branch == "stable" else 'bus.subscribe("orders", events.append)'
            source = "app.py"
        else:
            marker, source = f'python3 scripts/test_pricing_{t1.entry(branch)}.py', ".github/workflows/tests.yml"
        sequence = evidence_sequence(observed, marker, source)
        interface_sequence = evidence_sequence(observed, 'def publish(self, topic, *, payload):', "notifications.py") if task == "a1" else sequence
        if task == "c1":
            runtime_verified = c1_eval.verified_runtime(observed, repo, 30)
        elif task == "a1":
            runtime_verified = verified_creation(observed, repo)
        else:
            runtime_verified = not any(r.get("entry") or r.get("receipts") for r in observed["records"])
        save(directory / f"observe-{branch}/evidence.json", {"source_request_sequence": sequence,
             "interface_request_sequence": interface_sequence, "runtime_requirement_met": runtime_verified})
        if not sequence or not interface_sequence or not runtime_verified:
            raise RuntimeError(f"{task}/{branch}: required observation evidence missing")
        costs["observation"][branch] = {"agent": c.usage(observed["events"], "agent"),
                                       "memory": c.usage(observed["events"], "memory")}
        costs["maintenance"][branch] = {}
        for mode in ("default", "revise"):
            restore(repo, state, directory / "learn/after")
            costs["maintenance"][branch][mode] = maintain(
                directory / f"{branch}-{mode}-maintenance", repo, provider, observed, mode)
            save(directory / "costs.json", costs)
        branches[branch] = {"state": state, "prior": learning["history"] + observed["history"]}
    return branches


def run_job(task, branch, condition, branch_data, lab, repo, sandbox, provider):
    directory = lab / f"{task}-{branch}-{condition}"
    state = branch_data["state"]
    memory = condition in {"default", "revise"}
    source = lab / task / f"{branch}-{condition}-maintenance/after" if memory else None
    restore(repo, state, source)
    directory.mkdir()
    snapshot(repo, directory / "before")
    mode = "revise" if condition == "revise" else "default"
    prior = branch_data["prior"] if condition == "full" else []
    run = turn(directory, repo, sandbox, provider, task, branch, prompt(task, "task"),
               memory=memory, prior=prior, mode=mode)
    if task == "t1":
        grade = test_grade(repo, branch, run)
        first_sequence = next((e["sequence"] for e in run["events"] if e["event"] == "tool_result"
                               and e.get("component") == "agent" and e["tool"] == "run_bash"
                               and command_entry(json.loads(e["arguments"])["command"],
                                                 (repo / json.loads(e["arguments"]).get("cwd", ".")).resolve(), repo)), None)
        first_action = grade["first_action"]
    else:
        def task_grade(path):
            return (c1_eval.grade(path, branch, sandbox, baseline=state) if task == "c1"
                    else grade_a1(path, branch, sandbox))

        grade = task_grade(repo)
        first_sequence = next((e["sequence"] for e in run["events"] if e["event"] == "workspace_delta" and e["changes"]), None)
        if run["first_state"] is None:
            first_grade, first_action = None, "none"
        else:
            with TemporaryDirectory(prefix="action-audit-") as tmp:
                copy = Path(tmp).resolve()
                write_project(copy, run["first_state"])
                first_grade = task_grade(copy)
            first_action = (c1_eval.first_action(state, run["first_state"], branch) if task == "c1"
                            else first_grade.get("action", "unclassified"))
        save(directory / "first-grade.json", first_grade)
    tools = [e for e in run["events"] if e["event"] == "tool_result" and e.get("component") == "agent"]
    before = [e for e in tools if first_sequence is None or e["sequence"] < first_sequence]
    if task != "t1" and first_sequence:
        before = before[:-1]
    row = {"task": task, "branch": branch, "condition": condition, "first_action": first_action,
           "first_sequence": first_sequence, "reads_before": sum(c.successful_shell_read(e) for e in before),
           "tools": len(tools), "agent": c.usage(run["events"], "agent"),
           "post_memory": c.usage(run["events"], "memory"), "grade": grade,
           "model_errors": sum(e["event"] == "model_error" for e in run["events"])}
    save(directory / "result.json", row)
    print(task, branch, condition, first_action, grade["strict_success"], flush=True)
    return row


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lab", type=Path)
    parser.add_argument("--tasks", nargs="+", choices=TASKS, default=list(TASKS))
    parser.add_argument("--seed", type=int, default=1134)
    return parser.parse_args(argv)


def main():
    args = parse_args()
    if len(args.tasks) != len(set(args.tasks)):
        raise ValueError("Each task may appear only once; use a new LAB for an independent repeat")
    lab = args.lab.resolve()
    lab.mkdir(parents=True)
    common = Path(subprocess.check_output(["git", "rev-parse", "--git-common-dir"], cwd=ROOT, text=True).strip())
    main_root = (ROOT / common).resolve().parent
    db = c.default_database_path().expanduser().resolve()
    sandbox = c.ExperimentSandbox([ROOT, main_root, lab, db, Path(str(db)+"-wal"), Path(str(db)+"-shm")])
    files = subprocess.check_output(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, text=True).splitlines()
    with zipfile.ZipFile(lab / "sources.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(set(files)):
            if (ROOT / name).is_file():
                archive.write(ROOT / name, name)
    order = [(task, branch, condition) for task in args.tasks for branch in ("stable", "changed") for condition in CONDITIONS]
    random.Random(args.seed).shuffle(order)
    save(lab / "manifest.json", {"purpose": "four-arm transfer; one learning history per task, one evaluation per cell",
         "protocol": PROTOCOL, "tasks": args.tasks, "conditions": CONDITIONS,
         "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
         "sources_sha256": hashlib.sha256((lab / "sources.zip").read_bytes()).hexdigest(),
         "model": c.MODEL, "max_tokens": c.MAX_TOKENS, "max_iterations": c.MAX_TOOL_ITERATIONS,
         "random_seed": args.seed, "order": order, "backend": sandbox.backend_name,
         "platform": platform.platform(), "python": sys.version,
         "prompts": {task: {stage: prompt(task, stage) for stage in ("learn", "observe", "task")} for task in args.tasks}})
    profile = c.BUILTIN_PROVIDERS[0]
    database = c.AppDatabase(db)
    try:
        credential = c.CredentialService(database).resolve(profile)
    finally:
        database.close()
    if not credential.token:
        raise RuntimeError("Configure the existing SiliconFlow provider first")
    provider = c.Provider(profile.name, credential.token, profile.base_url)
    prepared, failures, rows = {}, [], []
    with TemporaryDirectory(prefix="agent-task-") as tmp:
        repo = Path(tmp).resolve() / "workspace"
        repo.mkdir()
        marker = lab / "isolation-check"
        marker.write_text("Evaluator only")
        for hidden in (marker, ROOT / "experiments/transfer_run.py", db):
            check = sandbox.execute("cat " + shlex.quote(str(hidden)) + " >/dev/null", cwd=repo,
                                    timeout_seconds=10, policy=c.SandboxPolicy(repo))
            if check.exit_code == 0 or "sandbox_apply" in check.stderr:
                raise RuntimeError("Task sandbox isolation unavailable")
        for task in args.tasks:
            try:
                prepared[task] = prepare(task, lab, repo, sandbox, provider)
                print(task, "preparation verified", flush=True)
            except Exception:
                failures.append({"phase": "prepare", "task": task, "traceback": traceback.format_exc()})
                save(lab / "failures.json", failures)
                print(task, "preparation FAILED", flush=True)
        for task, branch, condition in order:
            if task not in prepared:
                continue
            try:
                rows.append(run_job(task, branch, condition, prepared[task][branch], lab, repo, sandbox, provider))
            except Exception:
                failures.append({"phase": "follow", "task": task, "branch": branch,
                                 "condition": condition, "traceback": traceback.format_exc()})
                print(task, branch, condition, "FAILED", flush=True)
            save(lab / "results.json", rows)
            save(lab / "failures.json", failures)
    print("Completed", len(rows), "follow-up tasks; failures", len(failures), flush=True)


if __name__ == "__main__":
    main()
