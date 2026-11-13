from unittest.mock import patch

import pytest

from src.infrastructure.memory import MemoryStore
from src.infrastructure.memory_revision import evidence_dialogue
from src.tools.memory.revise_memory import (
    SearchMemoryTool,
    ReadMemoryTool,
    UpdateMemoryTool,
    SearchMemoryArgs,
    ReadMemoryArgs,
    CreateMemoryTool,
)

from tests.memory.support import (
    NEW,
    OLD,
    VERIFY,
    creation_args,
    read_then_update,
    seed,
    tools,
    transcript,
    update_args,
)


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
], ids=["unknown-evidence", "outside-path", "frontmatter", "unexecuted-command"])
def test_invalid_updates_do_not_modify_memory(tmp_path, changes):
    store = seed(tmp_path)
    original = store.read_memory_file("config")
    revision, context = tools(store, transcript(ok=False))
    note = ReadMemoryTool(revision).run(ReadMemoryArgs(filename="config.md"), context)
    result = UpdateMemoryTool(revision).run(update_args(note["version"], **changes), context)
    assert not result["ok"]
    if changes.get("evidence_ids") == ["invented"]:
        assert result["error"] == "invalid_evidence"
        assert result["available_evidence_ids"] == ["read-1"]
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


def test_index_failure_rolls_back_note(tmp_path):
    store = seed(tmp_path)
    original, index = store.read_memory_file("config"), store.read_memory_index()
    with patch.object(store, "_rebuild_index", side_effect=PermissionError("denied")):
        assert read_then_update(store)["error"] == "write_error"
    assert store.read_memory_file("config") == original
    assert store.read_memory_index() == index


def test_missing_update_points_to_creation_without_guessing_versions(tmp_path):
    store = MemoryStore(tmp_path, model="test", maintenance_mode="revise")
    revision, context = tools(store)
    result = UpdateMemoryTool(revision).run(update_args("base", filename="notification-wiring.md"), context)
    assert result["error"] == "unknown_project_memory" and "create_memory" in result["guidance"]
    result = CreateMemoryTool(revision).run(creation_args(), context)
    assert result["ok"]
    read = ReadMemoryTool(revision).run(ReadMemoryArgs(filename=result["filename"]), context)
    updated = UpdateMemoryTool(revision).run(
        update_args(read["version"], filename=result["filename"], body=NEW + "\n" + VERIFY + "\nKeep port 8080."),
        context)
    assert updated["ok"] and revision.created[result["filename"]].content.endswith("Keep port 8080.")


def test_creation_never_overwrites_existing_notes_or_exact_fact_aliases(tmp_path):
    store = seed(tmp_path)
    revision, context = tools(store)
    original = store.read_memory_file("config")
    writer = CreateMemoryTool(revision)
    assert writer.run(creation_args(name="config"), context)["error"] == "memory_exists"
    assert store.read_memory_file("config") == original
    assert writer.run(creation_args(), context)["ok"]
    assert writer.run(creation_args(name="another-name"), context)["error"] == "duplicate_memory"


def test_cancelled_creation_and_index_failure_leave_previous_memory_intact(tmp_path):
    store = seed(tmp_path)
    original = store.read_memory_index()
    revision, context = tools(store, should_commit=lambda: False)
    assert CreateMemoryTool(revision).run(creation_args(), context)["error"] == "cancelled"
    revision, context = tools(store)
    with patch.object(store, "_rebuild_index", side_effect=PermissionError("denied")):
        assert CreateMemoryTool(revision).run(creation_args(), context)["error"] == "write_error"
    assert not (store.memory_dir / "notification-wiring.md").exists()
    assert store.read_memory_index() == original


@pytest.mark.parametrize("name", ["../escape", "config.md", "memory", "MEMORY"],
                         ids=["outside-path", "extension", "reserved-index", "uppercase"])
def test_creation_storage_rejects_paths_and_index_names(tmp_path, name):
    store = seed(tmp_path)
    _, evidence = evidence_dialogue(transcript())
    assert store.create_revision_memory(name, NEW, NEW, status="active", reason="source",
                                        evidence=list(evidence.values()))["error"] == "invalid_name"


@pytest.mark.parametrize("changes,error", [
    ({"evidence_ids": ["invented"]}, "invalid_evidence"),
    ({"evidence_ids": ["edit-1"]}, "missing_observation"),
    ({"body": "---\nname: other\n---\nbody"}, "body_must_exclude_frontmatter"),
], ids=["unknown-evidence", "write-only-evidence", "frontmatter"])
def test_creation_evidence_and_body_checks_match_revision_checks(tmp_path, changes, error):
    store = MemoryStore(tmp_path, model="test", maintenance_mode="revise")
    revision, context = tools(store)
    revision.evidence["edit-1"] = {"evidence_id": "edit-1", "tool": "edit_file", "result": {"ok": True}}
    assert CreateMemoryTool(revision).run(creation_args(**changes), context)["error"] == error
    assert store.list_memory_files() == []


def test_multiline_command_evidence_does_not_close_frontmatter_early(tmp_path):
    store = MemoryStore(tmp_path, maintenance_mode="revise")
    packet = {"tool": "run_bash", "evidence_kind": "command_failed",
              "result": {"ok": False, "exit_code": 1, "stdout": "before\n---\nafter", "stderr": ""}}
    result = store.create_revision_memory("test-result", "Observed command failure.", "The command exited 1.",
                                         status="active", reason="observed failure", evidence=[packet])
    assert result["ok"]
    assert store.read_revision_memory(result["filename"])["body"] == "The command exited 1."
    header, _ = store._parse_frontmatter(store.read_memory_file("test-result"))
    assert header["revision"]["evidence"][0]["result"]["stdout"] == "before\n---\nafter"
    assert "Observed command failure." in store.visible_index()
