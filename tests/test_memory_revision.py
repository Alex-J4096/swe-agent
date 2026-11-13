import json
from types import SimpleNamespace
from unittest.mock import patch

from openai.types.chat import ChatCompletion
import pytest

from main import parse_args
from src.infrastructure.experiment_log import configure_experiment_log, stop_experiment_log
from src.infrastructure.memory import MemoryStore
from src.infrastructure.memory_revision import evidence_dialogue
from src.infrastructure.memory_selection_cache import MemorySelectionCache
from src.runtime.memory_worker import MemoryWorker
from src.tools.base import ToolContext
from src.runtime.session import Session
from src.tools.memory.revise_memory import (
    RevisionContext, SearchMemoryTool, ReadMemoryTool, UpdateMemoryTool,
    SearchMemoryArgs, ReadMemoryArgs, UpdateMemoryArgs,
)

OLD = "The service reads settings.toml."
NEW = "The service reads app.toml."
VERIFY = "Run python3 service.py to verify changes."


def transcript(ok=True):
    return [
        {"role": "user", "content": "Change the service timeout in settings.toml."},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "run-1", "function": {"name": "run_bash", "arguments": '{"command":"python3 service.py"}'}}
        ]},
        {"role": "tool", "tool_call_id": "run-1", "content": json.dumps({"ok": ok, "stdout": "30\n"})},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "read-1", "function": {"name": "read_file", "arguments": '{"file_path":"service.py"}'}}
        ]},
        {"role": "tool", "tool_call_id": "read-1", "content": json.dumps({"ok": True, "path": "service.py", "content": 'config = "app.toml"'})},
    ]


def seed(tmp_path, mode="revise"):
    store = MemoryStore(tmp_path, client=object(), model="test", maintenance_mode=mode)
    store.write_memory_file("config", "project", OLD, OLD + "\n" + VERIFY)
    return store


def tools(store, messages=None, should_commit=None):
    _, evidence = evidence_dialogue(messages or transcript())
    revision = RevisionContext(store, evidence, should_commit, "parent")
    context = ToolContext(session=Session(model="test"))
    return revision, context


def update_args(version, **changes):
    values = dict(filename="config.md", expected_version=version, description=NEW,
                  body=NEW + "\n" + VERIFY, status="active", evidence_ids=["read-1"],
                  reason="Current service.py loads app.toml.")
    values.update(changes)
    return UpdateMemoryArgs(**values)


def read_then_update(store, **changes):
    revision, context = tools(store)
    read = ReadMemoryTool(revision).run(ReadMemoryArgs(filename="config.md"), context)
    return UpdateMemoryTool(revision).run(update_args(read["version"], **changes), context)


def scripted_client(handler):
    requests = []
    def create(**kwargs):
        requests.append(kwargs)
        message = handler(kwargs, len(requests))
        return ChatCompletion(id=f"response-{len(requests)}", created=1, model="test",
                              object="chat.completion", choices=[{
                                  "index": 0, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                                  "message": {"role": "assistant", **message},
                              }], usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), requests


def call(name, arguments):
    return {"content": "", "tool_calls": [{"id": "call-" + name, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}]}


def last_output(request):
    return json.loads(request["messages"][-1]["content"])


def revision_script(request, step):
    if step == 1:
        return call("search_memory", {"query": "service configuration"})
    if step == 2:
        return call("read_memory", {"filename": last_output(request)["memories"][0]["filename"]})
    if step == 3:
        return call("update_memory", update_args(last_output(request)["version"]).model_dump())
    if step == 4:
        assert last_output(request)["ok"]
        return call("read_memory", {"filename": "config.md"})
    assert last_output(request)["body"] == NEW + "\n" + VERIFY
    return {"content": '{"kept":[],"memories":[]}'}


def test_worker_runs_isolated_subagent_and_logs_full_update(tmp_path):
    store = seed(tmp_path)
    store.client, requests = scripted_client(revision_script)
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
    assert {t["function"]["name"] for t in requests[0]["tools"]} == {"search_memory", "read_memory", "update_memory"}
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


def test_formatting_and_summary_wording_do_not_need_old_claim_match(tmp_path):
    store = seed(tmp_path)
    store.write_memory_file("config", "project", "Service configuration location",
                            "The Python service (`service.py`) reads `settings.toml`.\n" + VERIFY)
    assert read_then_update(store)["ok"]
    assert store.list_memory_files()[0]["body"] == NEW + "\n" + VERIFY


def test_suspect_can_be_resolved_after_reading_latest_version(tmp_path):
    store = seed(tmp_path)
    read_then_update(store, status="suspect", description="Check the loader before editing.",
                     body="The previous timeout edit did not affect runtime. Check the current loader.\n" + VERIFY)
    assert "Pending verification" in store.visible_index()
    assert VERIFY in store.list_memory_files()[0]["body"]
    assert read_then_update(store)["ok"]
    assert "Pending verification" not in store.visible_index()
    assert "status: active" in store.read_memory_file("config")


@pytest.mark.parametrize("changes", [
    {"evidence_ids": ["invented"]}, {"filename": "../outside.md"},
    {"body": "---\nname: config\n---\n" + NEW}, {"evidence_ids": ["run-1"]},
])
def test_invalid_updates_do_not_modify_memory(tmp_path, changes):
    store = seed(tmp_path)
    original = store.read_memory_file("config")
    revision, context = tools(store, transcript(ok=False))
    note = ReadMemoryTool(revision).run(ReadMemoryArgs(filename="config.md"), context)
    result = UpdateMemoryTool(revision).run(update_args(note["version"], **changes), context)
    assert not result["ok"]
    assert store.read_memory_file("config") == original


def test_update_requires_read_and_rejects_stale_version(tmp_path):
    store = seed(tmp_path)
    revision, context = tools(store)
    writer = UpdateMemoryTool(revision)
    version = store.read_revision_memory("config.md")["version"]
    assert writer.run(update_args(version), context)["error"] == "read_required"
    read = ReadMemoryTool(revision).run(ReadMemoryArgs(filename="config.md"), context)
    store.write_memory_file("config", "project", "Newer", OLD + "\nKeep this newer fact.")
    assert writer.run(update_args(read["version"]), context)["error"] == "stale_version"
    assert "Keep this newer fact." in store.read_memory_file("config")


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


def test_user_and_session_notes_cannot_be_revised(tmp_path):
    store = seed(tmp_path)
    for memory_type, scope in [("user", "project"), ("project", "session")]:
        store.write_memory_file("config", memory_type, OLD, OLD, scope=scope, session_id="s")
        assert store.read_revision_memory("config.md") is None
        revision, context = tools(store)
        assert not SearchMemoryTool(revision).run(SearchMemoryArgs(query="config"), context)["memories"]


def test_search_can_find_body_fact_beyond_initial_keyword_suggestions(tmp_path):
    store = seed(tmp_path)
    store.write_memory_file("unrelated-title", "project", "Internal details", "Timeout loading configuration lives here.")
    revision, context = tools(store)
    found = SearchMemoryTool(revision).run(SearchMemoryArgs(query="timeout configuration"), context)
    assert found["memories"][0]["filename"] == "unrelated-title.md"


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


def test_index_failure_rolls_back_note(tmp_path):
    store = seed(tmp_path)
    original, index = store.read_memory_file("config"), store.read_memory_index()
    with patch.object(store, "_rebuild_index", side_effect=PermissionError("denied")):
        assert read_then_update(store)["error"] == "write_error"
    assert store.read_memory_file("config") == original
    assert store.read_memory_index() == index


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


def test_modes_preserve_default_and_enrich_evidence(tmp_path):
    for mode in ("default", "evidence"):
        store = seed(tmp_path / mode, mode)
        with patch.object(store, "_call_llm", return_value="[]") as request:
            store.extract_memories(transcript())
        assert ("python3 service.py" in request.call_args.args[0]) == (mode == "evidence")
    assert parse_args([]).memory_maintenance == "default"


def test_evidence_pairs_parallel_results_and_keeps_complete_packets():
    messages = transcript()
    parallel = [messages[0], {"role": "assistant", "content": "",
                "tool_calls": messages[1]["tool_calls"] + messages[3]["tool_calls"]}, messages[4], messages[2]]
    _, evidence = evidence_dialogue(parallel)
    assert evidence["run-1"]["arguments"]["command"] == "python3 service.py"
    assert evidence["read-1"]["result"]["path"] == "service.py"
    assert evidence_dialogue(messages, max_chars=10) == ("", {})


@pytest.mark.parametrize("mode", ["default", "evidence", "revise"])
def test_cli_initializes_normal_runtime_and_exits(tmp_path, monkeypatch, mode):
    import main as application
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(application, "default_database_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(application.CredentialService, "resolve", lambda *args: SimpleNamespace(token=None))
    monkeypatch.setattr(application.CredentialService, "stamp", lambda *args: None)
    monkeypatch.setattr(application, "Provider", lambda **kwargs: SimpleNamespace(client=object()))
    monkeypatch.setattr(application, "print_welcome", lambda *args, **kwargs: None)
    def stop_at_prompt(*args, **kwargs):
        raise EOFError
    monkeypatch.setattr(application, "prompt", stop_at_prompt)
    with patch.object(application, "MemoryStore", wraps=MemoryStore) as constructor:
        application.main(["--memory-maintenance", mode])
    assert constructor.call_args.kwargs["maintenance_mode"] == mode


def test_edit_evidence_can_accompany_observations_but_cannot_replace_them(tmp_path):
    store = seed(tmp_path)
    revision, context = tools(store)
    revision.evidence["edit-1"] = {"evidence_id": "edit-1", "tool": "edit_file", "result": {"ok": True}}
    read = ReadMemoryTool(revision).run(ReadMemoryArgs(filename="config.md"), context)
    writer = UpdateMemoryTool(revision)
    assert writer.run(update_args(read["version"], evidence_ids=["edit-1"]), context)["error"] == "missing_observation"
    assert writer.run(update_args(read["version"], evidence_ids=["edit-1", "read-1"]), context)["ok"]


def test_invalid_evidence_returns_eligible_ids_for_retry(tmp_path):
    store = seed(tmp_path)
    revision, context = tools(store)
    read = ReadMemoryTool(revision).run(ReadMemoryArgs(filename="config.md"), context)
    result = UpdateMemoryTool(revision).run(update_args(read["version"], evidence_ids=["wrong"]), context)
    assert result["available_evidence_ids"] == ["read-1", "run-1"]
