"""C1 must use the same preparation, treatment isolation and metrics as A1/T1."""
from copy import deepcopy
import json
import sys
from types import SimpleNamespace

from openai.types.chat import ChatCompletion
import pytest

from experiments.c1 import evaluate as oracle
from experiments.c1 import fixture
from experiments import transfer_run as transfer
from experiments.task_support import project_files, write_project
from src.infrastructure.memory import MemoryStore


def learned_sources():
    state = oracle.initial_sources()
    state[fixture.OLD] = state[fixture.OLD].replace("timeout = 10", "timeout = 30")
    return state


@pytest.mark.parametrize("branch", ["stable", "changed"])
def test_c1_oracle_distinguishes_success_wrong_target_and_shortcuts(tmp_path, local_executor, branch):
    baseline = oracle.branch_sources(learned_sources(), branch)
    write_project(tmp_path, baseline)
    active = oracle.target(branch)
    inactive = fixture.NEW if active == fixture.OLD else fixture.OLD
    path = tmp_path / inactive
    path.write_text(path.read_text().replace("30", "60"))
    assert not oracle.grade(tmp_path, branch, local_executor, baseline=baseline)["success"]
    assert oracle.first_action(baseline, project_files(tmp_path), branch) == "inactive"
    (tmp_path / active).write_text(baseline[active].replace("30", "60"))
    result = oracle.grade(tmp_path, branch, local_executor, baseline=baseline)
    assert result["success"] and not result["strict_success"]
    assert oracle.first_action(baseline, project_files(tmp_path), branch) == "both"
    path.write_text(baseline[inactive])
    assert oracle.grade(tmp_path, branch, local_executor, baseline=baseline)["strict_success"]
    (tmp_path / "service.py").write_text("print(60)\n")
    result = oracle.grade(tmp_path, branch, local_executor, baseline=baseline)
    assert result["success"] and not result["strict_success"]
    (tmp_path / "service.py").write_text(baseline["service.py"])
    (tmp_path / "extra.py").write_text("# out of scope\n")
    assert not oracle.grade(tmp_path, branch, local_executor, baseline=baseline)["strict_success"]
    (tmp_path / "extra.py").unlink()
    (tmp_path / active).write_text("timeout = 60\nport = 9000\n")
    assert not oracle.grade(tmp_path, branch, local_executor, baseline=baseline)["strict_success"]


def test_c1_runtime_verification_rejects_readback_and_forged_numeric_output(tmp_path):
    def run(command, stdout="30", ok=True):
        return {"events": [{"event": "tool_result", "component": "agent", "tool": "run_bash",
                            "arguments": json.dumps({"command": command}),
                            "output": {"ok": ok, "exit_code": 0 if ok else 1, "stdout": stdout}}]}

    assert oracle.verified_runtime(run("python3 -I -B service.py"), tmp_path, 30)
    assert oracle.verified_runtime(run(f"cd {tmp_path} && python3 ./service.py"), tmp_path, 30)
    for command in ("cat service.py", "python3 -c 'print(30)'", "python3 service.py; echo 30",
                    "cd .. && python3 service.py", "python3 service.py > output.txt"):
        assert not oracle.verified_runtime(run(command), tmp_path, 30)
        assert not transfer.approved_command("c1", command, tmp_path, tmp_path)
    assert not oracle.verified_runtime(run("python3 service.py", stdout="10"), tmp_path, 30)
    assert not oracle.verified_runtime(run("python3 service.py", ok=False), tmp_path, 30)


def test_c1_preparation_shares_original_learning_and_observation_across_workers(tmp_path, monkeypatch, local_executor):
    lab, repo = tmp_path / "lab", tmp_path / "repo"
    lab.mkdir()
    turns, maintenance = [], []

    def turn(directory, repo, sandbox, provider, task, branch, instruction, **kwargs):
        turns.append((directory.name, branch, kwargs))
        if directory.name == "learn":
            path = repo / fixture.OLD
            path.write_text(path.read_text().replace("10", "30"))
            MemoryStore(repo).write_memory_file("loader", "project", "Old loader", "Use settings.toml.")
        messages = [
            {"role": "user", "content": instruction},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "read", "type": "function",
                "function": {"name": "read_file", "arguments": '{"file_path":"service.py"}'}}]},
            {"role": "tool", "tool_call_id": "read", "content": json.dumps({
                "ok": True, "path": "service.py", "content": (repo / "service.py").read_text()})},
            {"role": "assistant", "content": "Observed the loader."},
        ]
        events = [{"event": "model_request", "component": "agent", "sequence": 7,
                   "request_id": "r", "request": {"messages": messages}},
                  {"event": "model_response", "request_id": "r", "component": "agent",
                   "response": {"usage": {"prompt_tokens": 20, "completion_tokens": 10}}},
                  {"event": "tool_result", "component": "agent", "tool": "run_bash",
                   "arguments": '{"command":"python3 service.py"}',
                   "output": {"ok": True, "stdout": "30\n", "exit_code": 0}}]
        directory.mkdir(parents=True)
        transfer.snapshot(repo, directory / "after")
        return {"events": events, "history": messages, "maintenance_input": deepcopy(messages),
                "session_id": directory.name, "first_state": None}

    def maintain(directory, repo, provider, run, mode):
        # Both treatments must start from learning memory, never another worker's output.
        assert MemoryStore(repo).list_visible_memories()[0]["body"] == "Use settings.toml."
        maintenance.append((directory.name, deepcopy(run["maintenance_input"])))
        MemoryStore(repo).write_memory_file("loader", "project", mode, mode + " memory")
        transfer.snapshot(repo, directory / "after")
        return {"prompt_tokens": 3, "completion_tokens": 2}

    monkeypatch.setattr(transfer, "turn", turn)
    monkeypatch.setattr(transfer, "maintain", maintain)
    prepared = transfer.prepare("c1", lab, repo, local_executor, object())
    assert len(turns) == 3 and len(maintenance) == 4
    assert all(call[2] == {"memory": True, "maintain": False} for call in turns[1:])
    for index, branch in enumerate(("stable", "changed")):
        assert maintenance[index * 2][1] == maintenance[index * 2 + 1][1]
        assert prepared[branch]["prior"][1]["tool_calls"][0]["id"] == "read"
        assert prepared[branch]["prior"][2]["role"] == "tool"
        assert len(prepared[branch]["prior"]) == 8
        assert all("timeout = 30" in prepared[branch]["state"][name] for name in (fixture.OLD, fixture.NEW))
    assert set(fixture.diff(prepared["stable"]["state"], prepared["changed"]["state"])) == {"service.py"}
    audit = json.loads((lab / "c1/learning-memories.json").read_text())
    assert audit["project_fact_count"] == 1 and audit["related_old_memory"] is None
    costs = json.loads((lab / "c1/costs.json").read_text())
    assert costs["learning"]["agent"]["prompt_tokens"] == 20
    assert costs["maintenance"]["changed"]["revise"]["prompt_tokens"] == 3


def test_c1_four_arms_isolate_memory_and_keep_first_wrong_edit_after_rework(tmp_path, monkeypatch, local_executor):
    lab, repo = tmp_path / "lab", tmp_path / "repo"
    lab.mkdir()
    state = oracle.branch_sources(learned_sources(), "changed")
    prior = [{"role": "user", "content": "Prior learning and observation."}]
    for mode in ("default", "revise"):
        path = lab / "c1" / f"changed-{mode}-maintenance/after"
        write_project(path, state)
        MemoryStore(path).write_memory_file("loader", "project", mode, mode + " memory")
    seen = []

    def turn(directory, repo, sandbox, provider, task, branch, instruction, **kwargs):
        condition = directory.name.rsplit("-", 1)[1]
        notes = MemoryStore(repo).list_visible_memories()
        assert [note["body"] for note in notes] == ([condition + " memory"] if condition in {"default", "revise"} else [])
        assert kwargs["prior"] == (prior if condition == "full" else [])
        assert kwargs["memory"] == (condition in {"default", "revise"})
        assert kwargs["mode"] == ("revise" if condition == "revise" else "default")
        seen.append(condition)
        first = dict(state)
        first[fixture.OLD] = first[fixture.OLD].replace("30", "60")
        (repo / fixture.NEW).write_text(state[fixture.NEW].replace("30", "60"))
        return {"first_state": first, "events": [
            {"event": "tool_result", "sequence": 1, "component": "agent", "tool": "read_file", "output": {"ok": True}},
            {"event": "tool_result", "sequence": 2, "component": "agent", "tool": "edit_file", "output": {"ok": True}},
            {"event": "workspace_delta", "sequence": 3, "changes": fixture.diff(state, first)},
            {"event": "tool_result", "sequence": 4, "component": "agent", "tool": "edit_file", "output": {"ok": True}},
        ]}

    monkeypatch.setattr(transfer, "turn", turn)
    for condition in transfer.CONDITIONS:
        row = transfer.run_job("c1", "changed", condition, {"state": state, "prior": prior},
                               lab, repo, local_executor, object())
        assert row["first_action"] == "inactive" and row["grade"]["strict_success"]
        assert row["first_sequence"] == 3 and row["reads_before"] == 1
        assert not json.loads((lab / f"c1-changed-{condition}/first-grade.json").read_text())["success"]
    assert seen == list(transfer.CONDITIONS)


def test_c1_turn_uses_real_runner_and_preserves_original_role_history(tmp_path, local_executor):
    repo, directory = tmp_path / "repo", tmp_path / "turn"
    write_project(repo, oracle.branch_sources(learned_sources(), "changed"))
    requests = []
    prior = [{"role": "user", "content": "Earlier inspection"},
             {"role": "assistant", "content": "The earlier observation is complete."}]

    def create(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            message = {"content": "", "tool_calls": [{"id": "edit", "type": "function", "function": {
                "name": "edit_file", "arguments": json.dumps({"file_path": fixture.NEW,
                "old_string": "timeout = 30", "new_string": "timeout = 60"})}}]}
        else:
            message = {"content": "Changed the timeout."}
        return ChatCompletion(id=str(len(requests)), created=1, model="test", object="chat.completion",
                              choices=[{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", **message}}])

    provider = SimpleNamespace(client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    run = transfer.turn(directory, repo, local_executor, provider, "c1", "changed", fixture.TASK,
                        memory=False, prior=prior, maintain=False)
    assert len(requests) == 2
    assert all(request["messages"][1:3] == prior for request in requests)
    assert run["first_state"][fixture.NEW].startswith("timeout = 60")
    assert (directory / "first-write" / fixture.NEW).exists()
    assert not (directory / "after/.swe-agent/memory").exists()


def test_default_batch_dispatches_twenty_four_cells_after_shared_preparation(tmp_path, monkeypatch):
    lab = tmp_path / "batch"
    calls = []
    monkeypatch.setattr(sys, "argv", ["transfer_run", str(lab)])
    monkeypatch.setattr(transfer.c, "default_database_path", lambda: tmp_path / "unused.db")
    monkeypatch.setattr(transfer.c, "AppDatabase", lambda path: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(transfer.c, "CredentialService", lambda db: SimpleNamespace(
        resolve=lambda profile: SimpleNamespace(token="test-only")))
    monkeypatch.setattr(transfer.c, "Provider", lambda *args: object())
    # Isolation here is simulated; OS sandbox validation is a separate preflight.
    monkeypatch.setattr(transfer.c, "ExperimentSandbox", lambda paths: SimpleNamespace(
        backend_name="simulated", execute=lambda *args, **kwargs: SimpleNamespace(exit_code=1, stderr="denied")))

    def prepare(task, *args):
        calls.append(("prepare", task))
        return {"stable": {}, "changed": {}}

    def run_job(task, branch, condition, *args):
        calls.append(("follow", task, branch, condition))
        return {"task": task, "branch": branch, "condition": condition}

    monkeypatch.setattr(transfer, "prepare", prepare)
    monkeypatch.setattr(transfer, "run_job", run_job)
    transfer.main()
    assert calls[:3] == [("prepare", task) for task in ("c1", "a1", "t1")]
    rows = json.loads((lab / "results.json").read_text())
    assert len(rows) == len({tuple(row.values()) for row in rows}) == 24
    for task in ("c1", "a1", "t1"):
        assert sum(row["task"] == task for row in rows) == 8
    manifest = json.loads((lab / "manifest.json").read_text())
    assert manifest["protocol"] == "transfer-four-arm-v2"
    assert manifest["conditions"] == ["none", "full", "default", "revise"]
    assert len(manifest["order"]) == 24
