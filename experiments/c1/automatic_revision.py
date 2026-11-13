"""Run the fixed automatic revision evaluation from its written protocol."""
import argparse
from concurrent.futures import ProcessPoolExecutor
from contextlib import chdir
import hashlib
import json
from pathlib import Path
import random
import shutil
import subprocess
from tempfile import TemporaryDirectory
import time
import traceback
import zipfile

from . import run as c
from src.tools.base import ToolContext
from src.tools.file.read_file import ReadFileArgs
from src.tools.file.edit_file import EditFileArgs
from src.tools.shell.bash import BashArgs

BASE_ROOT = Path(subprocess.check_output(["git", "rev-parse", "--git-common-dir"], text=True).strip()).resolve().parent

MODES = ("default", "evidence", "revise")
SCENES = ("stable", "conflict", "located")
TASK = "请把服务的实际请求超时调到 90 秒，并验证修改生效。"


def setup(lab, seed_root):
    db = c.default_database_path().expanduser().resolve()
    sandbox = c.ExperimentSandbox([BASE_ROOT, seed_root, c.ROOT, lab, db,
                                  db.parent / (db.name + "-wal"), db.parent / (db.name + "-shm")])
    profile = c.BUILTIN_PROVIDERS[0]
    database = c.AppDatabase(db)
    try:
        credential = c.CredentialService(database).resolve(profile)
    finally:
        database.close()
    return sandbox, c.Provider(profile.name, credential.token, profile.base_url)


def digest_files(directory):
    return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.rglob("*")) if p.is_file()}


def prepare(repo, old, target, scene, sandbox):
    c.fixture(repo, target, 30)
    context = ToolContext(session=c.Session(model=c.MODEL), workspace_root=repo)
    history = [{"role": "user", "content": c.TASK}]

    def call(tool, args):
        output = tool.run(args, context)
        assert output["ok"], output
        key = f"prepare-{len(history)}"
        history.extend([
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": key, "type": "function", "function": {
                    "name": tool.name, "arguments": args.model_dump_json(exclude_defaults=True)}}]},
            {"role": "tool", "tool_call_id": key, "content": json.dumps(output)},
        ])
        return output

    call(c.EditFileTool(repo), EditFileArgs(file_path=old, old_string="timeout = 30", new_string="timeout = 60"))
    assert "timeout = 60" in call(c.ReadFileTool(repo), ReadFileArgs(file_path=old))["content"]
    runtime = call(c.BashTool(repo, sandbox), BashArgs(command="python3 service.py"))
    assert runtime["stdout"].strip() == ("60" if scene == "stable" else "30")
    if scene != "conflict":
        code = call(c.ReadFileTool(repo), ReadFileArgs(file_path="service.py"))
        assert f'with_name("{target}")' in code["content"]
    return history


def run_job(args):
    lab, seed_root, job = args
    lab, seed_root = Path(lab), Path(seed_root)
    direction, scene, rep, mode = job.split("-")
    old = c.OLD if direction == "settings" else c.NEW
    target = old if scene == "stable" else (c.NEW if old == c.OLD else c.OLD)
    jobdir = lab / job
    jobdir.mkdir()
    started = time.monotonic()
    try:
        sandbox, provider = setup(lab, seed_root)
        with TemporaryDirectory(prefix="auto-memory-") as tmp:
            repo = Path(tmp).resolve()
            history = prepare(repo, old, target, scene, sandbox)
            c.copy_memory(seed_root / direction, repo)
            c.snapshot(repo, jobdir / "before")
            c.save(jobdir / "input.json", history)
            store = c.MemoryStore(repo, client=provider.client, model=c.MODEL, maintenance_mode=mode)
            assert len(store.list_memory_files()) < 10
            candidates = store.select_relevant_memories(history, use_llm=False, session_id=job)
            c.save(jobdir / "candidates.json", candidates)
            c.configure_experiment_log(jobdir / "maintenance.jsonl", stage=job, model=c.MODEL,
                                       memory_maintenance=mode)
            worker = c.MemoryWorker(store)
            try:
                worker.submit(history, job).result()
            finally:
                worker.shutdown(wait=True)
                c.stop_experiment_log()
            c.snapshot(repo, jobdir / "after-maintenance")
            c.save(jobdir / "visible-before-task.json", {
                "index": store.visible_index("next-session"),
                "notes": store.list_visible_memories("next-session"),
            })
            maintenance = [json.loads(line) for line in (jobdir / "maintenance.jsonl").read_text().splitlines()]
            maintenance_seconds = time.monotonic() - started
            with chdir(repo):
                events = c.run_turn(jobdir, "follow", repo, sandbox, provider, TASK, True, [],
                                    maintenance_mode=mode)
            first = c.summarize(events, target)
            grade = c.grade(repo, sandbox, target, 90)
            final = c.files(repo)
            deltas = [e for e in events if e["event"] == "workspace_delta" and e["changes"]]
            wrong = [e for e in events if e["event"] == "tool_result" and e["tool"] == "run_bash"
                     and json.loads(e["arguments"]).get("command", "").strip() in c.SERVICE_COMMANDS
                     and e["output"].get("ok") and e["output"].get("stdout", "").strip() != "90"]
            wrong_first = first["first_action"] in {"inactive", "both", "other"}
            rework = wrong_first and any(
                first["delta_sequence"] < w["sequence"] < e["sequence"] for w in wrong for e in deltas)
            row = dict(first, **grade, job=job, direction=direction, scene=scene, mode=mode,
                       strict_success=grade["success"] and grade["loader_intact"]
                       and grade["unrelated_fields_intact"] and set(final) == set(c.FILES),
                       extra_files=sorted(set(final) - set(c.FILES)),
                       rework=rework,
                       idle_written=any(old in e["changes"] for e in deltas) if scene != "stable"
                       else any((c.NEW if old == c.OLD else c.OLD) in e["changes"] for e in deltas),
                       initial_maintenance=c.usage(maintenance, "memory"),
                       protocol_deviation=len(c.MemoryStore(jobdir / "after-maintenance").list_memory_files()) >= 10,
                       maintenance_seconds=maintenance_seconds, elapsed_seconds=time.monotonic() - started)
            c.save(jobdir / "final-files.json", final)
            c.copy_memory(repo, jobdir / "final-memory")
            c.save(jobdir / "result.json", row)
            print(job, row["first_action"], row["strict_success"], flush=True)
    except Exception:
        c.save(jobdir / "failure.json", {"job": job, "traceback": traceback.format_exc()})
        print(job, "FAILED", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("lab", type=Path)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    lab = args.lab.resolve()
    if args.check_only:
        lab.mkdir(parents=True, exist_ok=True)
        sandbox, _ = setup(lab, args.previous.resolve())
        with TemporaryDirectory() as tmp:
            c.preflight(lab, Path(tmp).resolve(), sandbox, c.default_database_path().expanduser().resolve())
            for direction in ("settings", "app"):
                old = c.OLD if direction == "settings" else c.NEW
                for scene in SCENES:
                    target = old if scene == "stable" else (c.NEW if old == c.OLD else c.OLD)
                    prepare(Path(tmp).resolve(), old, target, scene, sandbox)
        print("sandbox, independent controls and six input conditions passed")
        return
    lab.mkdir(parents=True)
    seed_root = lab / "seeds"
    for direction in ("settings", "app"):
        c.copy_memory(args.previous.resolve() / (direction + "-single"), seed_root / direction)
        assert c.MemoryStore(seed_root / direction).list_memory_files()
    files = subprocess.check_output(["git", "ls-files", "-co", "--exclude-standard"], text=True).splitlines()
    with zipfile.ZipFile(lab / "sources.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(set(files)):
            path = c.ROOT / name
            if path.is_file():
                archive.write(path, name)
    jobs = [f"{d}-{s}-{r}-{m}" for d in ("settings", "app") for s in SCENES
            for r in (1, 2) for m in MODES]
    random.Random(1133).shuffle(jobs)
    manifest = {"commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                "sources_sha256": hashlib.sha256((lab / "sources.zip").read_bytes()).hexdigest(),
                "seeds": digest_files(seed_root), "model": c.MODEL, "max_tokens": c.MAX_TOKENS,
                "max_iterations": c.MAX_TOOL_ITERATIONS, "task": TASK, "order": jobs, "random_seed": 1133}
    c.save(lab / "manifest.json", manifest)
    with ProcessPoolExecutor(3) as pool:
        list(pool.map(run_job, [(str(lab), str(seed_root), job) for job in jobs]))


if __name__ == "__main__":
    main()
