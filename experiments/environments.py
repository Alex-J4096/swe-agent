"""Deploy and preflight A1/T1 fixtures without model calls or external services.

python -m experiments.environments deploy LAB
python -m experiments.environments check LAB
python -m experiments.environments create a1 /tmp/project --branch changed
"""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

from experiments.a1 import fixture as a1
from experiments.a1.evaluate import grade as grade_a1
from experiments.c1.sandbox import ExperimentSandbox
from experiments.task_support import hashes, project_files, save, write_project
from experiments.t1 import fixture as t1
from experiments.t1.evaluate import FAILURES, TraceExecutor, grade as grade_t1
from src.runtime.sandbox import SandboxPolicy

ROOT = Path(__file__).resolve().parents[1]


def deploy(lab: Path) -> None:
    if lab.exists() and any(lab.iterdir()):
        raise ValueError("Choose a new empty LAB; deployment never overwrites prior data")
    lab.mkdir(parents=True, exist_ok=True)
    projects = {}
    for task, module in (("a1", a1), ("t1", t1)):
        for branch in ("stable", "changed"):
            key = f"{task}/{branch}"
            module.create(lab / "fixtures" / key, branch)
            projects[key] = hashes(module.sources(branch))
    a1.create(lab / "fixtures/a1/learn", "stable", "learn")
    projects["a1/learn"] = hashes(a1.sources("stable", "learn"))
    source_paths = [ROOT / "experiments/environments.py", ROOT / "experiments/task_support.py",
                    ROOT / "experiments/c1/sandbox.py", ROOT / "src/runtime/sandbox.py"]
    for task in ("a1", "t1"):
        source_paths.extend((ROOT / f"experiments/{task}").glob("*.py"))
    source_hashes = {}
    for path in sorted(source_paths):
        relative = path.relative_to(ROOT)
        dest = lab / "environment-source" / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
        source_hashes[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
    save(lab / "manifest.json", {
        "purpose": "environment preflight; no agent/model experiment",
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "environment_sources_sha256": source_hashes, "projects": projects,
        "python": sys.version, "platform": platform.platform(),
        "prompts": {"a1": [a1.LEARN, a1.OBSERVE, a1.TASK], "t1": [t1.LEARN, t1.OBSERVE, t1.TASK]},
    })
    print(f"Deployed five project snapshots to {lab / 'fixtures'}")


def patch_cancellation(repo: Path, routes: tuple[str, ...], *, wrong_id=False, repeat=False) -> None:
    path = repo / "order_service.py"
    text = path.read_text()
    calls = [a1.CALLS[route].replace("order.created", "order.cancelled") for route in routes]
    if wrong_id:
        calls = [call.replace(', order_id)', ', "WRONG-ORDER")').replace('"order_id": order_id', '"order_id": "WRONG-ORDER"') for call in calls]
    if repeat:
        calls *= 2
    insert = "".join("        " + call + "\n" for call in calls)
    text = text.replace('        order["status"] = "cancelled"\n', '        order["status"] = "cancelled"\n' + insert)
    path.write_text(text)


def check(lab: Path) -> None:
    manifest = json.loads((lab / "manifest.json").read_text())
    if (lab / "controls.json").exists() or (lab / "command-journals").exists():
        raise ValueError("Preflight evidence already exists; deploy a new LAB to repeat")
    for relative, digest in manifest["environment_sources_sha256"].items():
        if hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Environment code changed since deployment: {relative}")
    for key, expected in manifest["projects"].items():
        if hashes(project_files(lab / "fixtures" / key)) != expected:
            raise ValueError(f"Deployed snapshot changed: {key}")
    common_git = Path(subprocess.check_output(["git", "rev-parse", "--git-common-dir"], cwd=ROOT, text=True).strip())
    if not common_git.is_absolute():
        common_git = ROOT / common_git
    main_root = common_git.resolve().parent
    database = Path.home() / ".swe-agent/state.db"
    sandbox = ExperimentSandbox([ROOT, main_root, lab, database,
                                 Path(str(database) + "-wal"), Path(str(database) + "-shm")])
    if not sandbox.available:
        raise RuntimeError("No sandbox backend. Install bubblewrap on Linux; no host fallback.")
    controls = []
    with TemporaryDirectory(prefix="agent-task-") as tmp:
        parent = Path(tmp).resolve()
        repo = parent / "workspace"
        repo.mkdir()
        marker = lab / "isolation-check"
        marker.write_text("A1/T1 evaluator data")
        for path in (marker, ROOT / "experiments/t1/evaluate.py", main_root / "main.py", database):
            if not path.exists():
                continue
            result = sandbox.execute("cat " + shlex.quote(str(path)) + " >/dev/null", cwd=repo,
                                     timeout_seconds=10, policy=SandboxPolicy(repo))
            if result.exit_code == 0:
                raise RuntimeError(f"Sandbox exposed evaluator or host state: {path}")
            if "sandbox_apply" in result.stderr:
                raise RuntimeError("OS denied nested sandbox execution; rerun preflight outside the outer sandbox")

        def reset(sources):
            for path in repo.iterdir():
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            write_project(repo, sources)

        def record(task, branch, name, result, expected):
            if result["strict_success"] != expected:
                save(lab / "failed-control.json", {"task": task, "branch": branch, "control": name, "result": result})
                raise AssertionError(f"Control failed: {task}/{branch}/{name}")
            controls.append({"task": task, "branch": branch, "control": name,
                             "expected_strict_success": expected, "result": result})

        # The learning project initially lacks the requested creation notification.
        reset(a1.sources("stable", "learn"))
        record("a1", "learn", "missing_creation", grade_a1(repo, "stable", sandbox, goal="create", stage="learn"), False)
        reset(a1.sources("stable"))
        record("a1", "learn", "correct_creation", grade_a1(repo, "stable", sandbox, goal="create", stage="learn"), True)

        for branch in ("stable", "changed"):
            active = a1.backend(branch)
            for name, routes, kwargs, expected in (
                ("missing", (), {}, False),
                ("legacy_path", ("legacy",), {}, active == "legacy"),
                ("bus_path", ("bus",), {}, active == "bus"),
                ("both_backends", ("legacy", "bus"), {}, False),
                ("wrong_order_id", (active,), {"wrong_id": True}, False),
                ("duplicate_send", (active,), {"repeat": True}, False),
            ):
                reset(a1.sources(branch))
                patch_cancellation(repo, routes, **kwargs)
                result = grade_a1(repo, branch, sandbox)
                record("a1", branch, name, result, expected)
                if routes and name.endswith("_path"):
                    # Even the unconsumed backend must accept and store its event.
                    assert all(case["legacy_events"] or case["published_events"] for case in result["cases"])
            reset(a1.sources(branch))
            patch_cancellation(repo, (active,))
            service = repo / "order_service.py"
            service.write_text(service.read_text().replace(a1.CALLS[active], "pass"))
            record("a1", branch, "creation_regression", grade_a1(repo, branch, sandbox), False)

        reset(a1.sources("stable"))
        patch_cancellation(repo, ("legacy",))
        result = grade_a1(repo, "changed", sandbox)
        assert result["success"] and not result["protected_files_intact"]
        record("a1", "changed", "rewired_application", result, False)

        for branch in ("stable", "changed"):
            for sequence in (("a",), ("b",), ("a", "b"), ("b", "a")):
                reset(t1.sources(branch))
                name = "then".join(sequence)
                trace = TraceExecutor(sandbox, repo, branch, lab / f"command-journals/{branch}-{name}.jsonl")
                for label in sequence:
                    trace.execute(f"python3 scripts/test_pricing_{label}.py", cwd=repo,
                                  timeout_seconds=10, policy=SandboxPolicy(repo))
                result = grade_t1(repo, branch, trace.records, FAILURES[sequence[-1]])
                expected = t1.entry(branch) == sequence[-1]
                record("t1", branch, name, result, expected)
                assert result["first_action"] == ("correct" if sequence[0] == t1.entry(branch) else "inactive")
            reset(t1.sources(branch))
            trace = TraceExecutor(sandbox, repo, branch)
            trace.execute(f"python3 scripts/test_pricing_{t1.entry(branch)}.py", cwd=repo,
                          timeout_seconds=10, policy=SandboxPolicy(repo))
            other = "b" if t1.entry(branch) == "a" else "a"
            record("t1", branch, "wrong_report", grade_t1(repo, branch, trace.records, FAILURES[other]), False)
            (repo / "pricing.py").write_text("# altered\n" + (repo / "pricing.py").read_text())
            record("t1", branch, "modified_source", grade_t1(repo, branch, trace.records, FAILURES[t1.entry(branch)]), False)
            reset(t1.sources(branch))
            trace = TraceExecutor(sandbox, repo, branch)
            # Fabricated stdout alone must never count as a verified test run.
            receipt = {"entry": t1.entry(branch), "executed": [], "tests_run": 0,
                       "failures": FAILURES[t1.entry(branch)], "errors": [], "exit_code": 1}
            command = "printf '%s\\n' " + shlex.quote("PRICING_TEST_RUN " + json.dumps(receipt))
            trace.execute(command, cwd=repo, timeout_seconds=10, policy=SandboxPolicy(repo))
            result = grade_t1(repo, branch, trace.records, FAILURES[t1.entry(branch)])
            record("t1", branch, "fabricated_stdout", result, False)
            assert result["first_action"] == "unverified"
    save(lab / "controls.json", controls)
    save(lab / "verification.json", {"backend": sandbox.backend_name, "controls_passed": len(controls),
                                     "model_calls": 0, "isolation_passed": True,
                                     "runtime_task_paths_neutral": True})
    print(f"{len(controls)} controls and isolation probes passed ({sandbox.backend_name}); no model calls")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("deploy", "check"):
        commands.add_parser(name).add_argument("lab", type=Path)
    create = commands.add_parser("create")
    create.add_argument("task", choices=("a1", "t1"))
    create.add_argument("destination", type=Path)
    create.add_argument("--branch", choices=("stable", "changed"), required=True)
    create.add_argument("--stage", choices=("learn", "test"), default="test")
    args = parser.parse_args()
    if args.action == "deploy":
        deploy(args.lab.resolve())
    elif args.action == "check":
        check(args.lab.resolve())
    elif args.task == "a1":
        a1.create(args.destination.resolve(), args.branch, args.stage)
    else:
        t1.create(args.destination.resolve(), args.branch)


if __name__ == "__main__":
    main()
