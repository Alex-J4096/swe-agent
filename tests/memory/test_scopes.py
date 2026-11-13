"""Session isolation and provenance are prerequisites for cross-session trials."""
import json

from src.infrastructure.memory import MemoryStore

from tests.memory.support import scripted_client


def test_session_memories_do_not_leak_into_other_sessions(tmp_path):
    store = MemoryStore(tmp_path)
    store.write_memory_file("task-plan", "project", "Sorting task plan", "Implement sort.py.",
                            scope="session", session_id="a")
    store.write_memory_file("user-preference-tabs", "user", "User prefers tabs", "Use tabs.")
    messages = [{"role": "user", "content": "Sorting task plan for sort.py"}]
    assert "task-plan.md" in store.select_relevant_memories(messages, session_id="a")
    assert "task-plan.md" not in store.select_relevant_memories(messages, session_id="b")
    assert "task-plan" not in store.visible_index("b")
    assert "user-preference-tabs" in store.visible_index("b")


def test_legacy_memories_are_inert_until_rescoped(tmp_path):
    store = MemoryStore(tmp_path)
    store.memory_dir.mkdir(parents=True, exist_ok=True)
    for name, kind in (("old-task", "project"), ("old-pref", "user")):
        (store.memory_dir / f"{name}.md").write_text(
            f"---\nname: {name}\ndescription: Old fact\ntype: {kind}\n---\n\nOld fact.\n")
    assert store.list_visible_memories("new-session") == []
    (store.memory_dir / "old-pref.md").write_text(
        "---\nname: old-pref\ndescription: Old preference\ntype: user\nscope: project\n---\n\nPrefer X.\n")
    assert {m["filename"] for m in store.list_visible_memories("new-session")} == {"old-pref.md"}


def test_extraction_records_session_provenance(tmp_path):
    store = MemoryStore(tmp_path, model="test")
    response = json.dumps([dict(name="task-state", type="project", description="Current task state",
                               body="Working on sorting.")])
    store.client, _ = scripted_client(lambda request, step: {"content": response})
    created = store.extract_memories([{"role": "user", "content": "Implement sorting."}], session_id="a")
    assert created[0].scope == "session" and created[0].session_id == "a"
    assert [m["filename"] for m in store.list_visible_memories("a")] == ["task-state.md"]
    assert store.list_visible_memories("b") == []
