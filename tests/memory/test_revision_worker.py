import json

import pytest

from src.infrastructure.experiment_log import configure_experiment_log, stop_experiment_log
from src.infrastructure.memory import MemoryStore
from src.infrastructure.memory_revision import evidence_dialogue
from src.infrastructure.memory_selection_cache import MemorySelectionCache
from src.runtime.memory_worker import MemoryWorker
from src.tools.memory.revise_memory import ReadMemoryTool, UpdateMemoryTool, ReadMemoryArgs

from tests.memory.support import (
    FIXTURES,
    NEW,
    OLD,
    VERIFY,
    call,
    creation_args,
    last_output,
    revision_script,
    scripted_client,
    seed,
    tools,
    transcript,
    update_args,
)


def test_worker_runs_isolated_subagent_and_logs_full_update(tmp_path):
    store = seed(tmp_path)
    store.client, requests = scripted_client(revision_script)
    store.write_memory_file("config", "project", "Service configuration location",
                            "The Python service (`service.py`) reads `settings.toml`.\n" + VERIFY)
    original = store.read_memory_file("config")
    cache = MemorySelectionCache()
    cache.put(session_id="s", messages=[], filenames=["config.md"], memory_revision=store.memory_revision())
    log = tmp_path / "log.jsonl"
    configure_experiment_log(log)
    worker = MemoryWorker(store)
    try:
        worker.submit(transcript(), "s").result()
    finally:
        worker.shutdown(wait=True)
        stop_experiment_log()
    assert len(requests) == 5
    assert {t["function"]["name"] for t in requests[0]["tools"]} == {"search_memory", "read_memory", "update_memory", "create_memory"}
    assert store.list_visible_memories()[0]["body"] == NEW + "\n" + VERIFY
    assert NEW in store.visible_index() and NEW in store.read_memory_index()
    assert OLD not in store.visible_index()
    assert NEW in store.load_memories_by_filenames(["config.md"])
    assert cache.get_compatible(session_id="s", messages=[], memory_revision=store.memory_revision()) == []
    events = [json.loads(line) for line in log.read_text().splitlines()]
    decision = next(e for e in events if e["event"] == "memory_revision_decision")
    change = next(e for e in events if e["event"] == "memory_change" and e["target"] == "note")
    assert decision["outcome"] == "committed"
    assert change["before"] == original and change["session_id"] == "s"
    assert change["maintenance_id"] == decision["maintenance_id"]
    assert all(e["component"] == "memory" for e in events if e["event"] == "model_request")
    assert all(e["component"] == "memory" for e in events if e["event"] == "tool_result")
    assert any(e["event"] == "memory_subagent_finished" and e["ok"] for e in events)


def test_subagent_recovers_from_stale_version_feedback(tmp_path):
    store = seed(tmp_path)
    def handler(request, step):
        if step == 1:
            return call("read_memory", {"filename": "config.md"})
        if step == 2:
            old_version = last_output(request)["version"]
            store.write_memory_file("config", "project", "Newer", OLD + "\n" + VERIFY + "\nKeep port 8080.")
            return call("update_memory", update_args(old_version).model_dump())
        if step == 3:
            assert last_output(request)["error"] == "stale_version"
            return call("read_memory", {"filename": "config.md"})
        if step == 4:
            return call("update_memory", update_args(last_output(request)["version"],
                         body=NEW + "\n" + VERIFY + "\nKeep port 8080.").model_dump())
        return {"content": '{"kept":[],"memories":[]}'}
    store.client, requests = scripted_client(handler)
    store.extract_memories(transcript())
    assert len(requests) == 5
    assert store.list_memory_files()[0]["body"].endswith("Keep port 8080.")


def test_cancelled_maintenance_stops_before_next_request_and_commit(tmp_path):
    store = seed(tmp_path)
    current = [True]
    def handler(request, step):
        current[0] = False
        return call("read_memory", {"filename": "config.md"})
    store.client, requests = scripted_client(handler)
    original = store.read_memory_file("config")
    store.extract_memories(transcript(), should_commit=lambda: current[0])
    assert len(requests) == 1 and store.read_memory_file("config") == original
    revision, context = tools(store, should_commit=lambda: False)
    read = ReadMemoryTool(revision).run(ReadMemoryArgs(filename="config.md"), context)
    assert UpdateMemoryTool(revision).run(update_args(read["version"]), context)["error"] == "cancelled"


def test_kept_note_and_new_independent_fact(tmp_path):
    store = seed(tmp_path)
    def handler(request, step):
        if step == 1:
            return call("read_memory", {"filename": "config.md"})
        return {"content": json.dumps({"kept": ["config.md"], "memories": [
            dict(name="tests", type="reference", scope="project", description="Test command", body="Run pytest.")
        ]})}
    store.client, _ = scripted_client(handler)
    created = store.extract_memories(transcript())
    assert len(created) == 1 and created[0].name == "tests"
    assert store.read_memory_file("config").endswith(OLD + "\n" + VERIFY + "\n")


@pytest.mark.parametrize("branch", ["stable", "changed"])
def test_empty_library_replays_actual_a1_observation_and_creates_via_normal_worker(tmp_path, branch):
    messages = json.loads((FIXTURES / f"a1-empty-{branch}.json").read_text())
    _, evidence = evidence_dialogue(messages)
    identity = next(key for key, packet in evidence.items()
                    if packet["tool"] == "read_file" and packet["arguments"]["file_path"] == "app.py")
    expected = "LegacyNotifier" if branch == "stable" else "EventBus"
    store = MemoryStore(tmp_path, model="test", maintenance_mode="revise")
    def handler(request, step):
        if step == 1:
            assert "Library empty" in request["messages"][1]["content"]
            return call("search_memory", {"query": ""})
        if step == 2:
            assert last_output(request)["total"] == 0
            assert "create_memory" in last_output(request)["guidance"]
            return call("create_memory", creation_args(description=f"Active backend is {expected}.",
                        body=f"The application consumes {expected}.", evidence_ids=[identity]).model_dump())
        if step == 3:
            assert last_output(request)["outcome"] == "created"
            return call("read_memory", {"filename": last_output(request)["filename"]})
        assert expected in last_output(request)["body"]
        # Successful tool commits survive an unparseable final prose response.
        return {"content": "The new project fact has been stored."}
    store.client, requests = scripted_client(handler)
    log = tmp_path / "events.jsonl"
    configure_experiment_log(log)
    worker = MemoryWorker(store)
    try:
        created, _ = worker.submit(messages, "a1").result()
    finally:
        worker.shutdown(wait=True)
        stop_experiment_log()
    assert len(requests) == 4 and len(created) == 1
    assert created[0].scope == "project" and expected in store.visible_index("new-session")
    events = [json.loads(line) for line in log.read_text().splitlines()]
    decision = next(e for e in events if e["event"] == "memory_revision_decision" and e["outcome"] == "created")
    change = next(e for e in events if e["event"] == "memory_change" and e["target"] == "note")
    assert decision["maintenance_id"] == change["maintenance_id"]
    assert change["before"] is None and change["operation"] == "create"


def test_empty_library_can_finish_without_creating_anything(tmp_path):
    store = MemoryStore(tmp_path, model="test", maintenance_mode="revise")
    store.client, requests = scripted_client(lambda request, step: {"content": '{"kept":[],"memories":[]}'})
    assert store.extract_memories(transcript()) == []
    assert len(requests) == 1 and store.list_memory_files() == []


def test_final_project_fact_cannot_bypass_creation_tool_or_evidence(tmp_path):
    store = MemoryStore(tmp_path, model="test", maintenance_mode="revise")
    store.client, _ = scripted_client(lambda request, step: {"content": json.dumps({
        "kept": [], "memories": [{"name": "unchecked", "type": "project", "scope": "project",
                                  "description": "No proof", "body": "Unverified project fact."}]})})
    assert store.extract_memories(transcript()) == [] and store.list_memory_files() == []
