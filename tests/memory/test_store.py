"""Storage and the unchanged extraction/consolidation baseline."""
import json
from unittest.mock import patch

import pytest

from src.infrastructure.memory import Memory, MemoryStore

from tests.memory.support import scripted_client


def test_writes_reads_and_indexes_memory(tmp_path):
    store = MemoryStore(tmp_path)
    path = store.write(Memory(name="user-preference-tabs", type="user",
                              description="User prefers tabs", content="Always use tabs."))
    assert path == (tmp_path / ".swe-agent/memory/user-preference-tabs.md").resolve()
    assert "name: user-preference-tabs" in path.read_text()
    assert store.read_memory_file("user-preference-tabs") == path.read_text()
    assert "- [user-preference-tabs](user-preference-tabs.md)" in store.read_memory_index()
    notes = store.list_memory_files()
    assert len(notes) == 1 and notes[0]["type"] == "user"
    assert notes[0]["body"] == "Always use tabs."


def test_rejects_unknown_type(tmp_path):
    with pytest.raises(ValueError):
        MemoryStore(tmp_path).write_memory_file("bad-memory", "unknown", "Description", "Content")


def test_does_not_read_outside_memory_directory(tmp_path):
    with pytest.raises(ValueError):
        MemoryStore(tmp_path).read_memory_file("../outside.md")


def test_extraction_persists_preferences_across_sessions(tmp_path):
    store = MemoryStore(tmp_path, model="test")
    response = json.dumps([dict(name="user-preference-tabs", type="user",
                               description="User prefers tabs", body="Always use tabs.")])
    store.client, _ = scripted_client(lambda request, step: {"content": response})
    created = store.extract_memories([{"role": "user", "content": "Use tabs."}], session_id="a")
    assert len(created) == 1 and created[0].name == "user-preference-tabs"
    assert created[0].path is not None
    assert created[0].scope == "project" and created[0].session_id is None
    assert "user-preference-tabs" in store.read_memory_index()
    assert store.list_visible_memories("b")[0]["filename"] == "user-preference-tabs.md"


def test_consolidates_memories_and_removes_replaced_files(tmp_path):
    store = MemoryStore(tmp_path, client=object(), model="test")
    store.CONSOLIDATE_THRESHOLD = 3
    store.write_memory_file("style-tabs", "user", "Tabs are preferred", "Use tabs.")
    store.write_memory_file("style-spaces", "user", "Spaces are preferred", "Use spaces.")
    store.write_memory_file("project-language", "project", "The project uses Python", "Use Python.")
    response = json.dumps({
        "memories": [dict(name="coding-style", type="user", description="Preferred coding style",
                          body="Use tabs consistently.")],
        "remove": ["style-tabs.md", "style-spaces.md"],
    })
    with patch.object(store, "_call_llm", return_value=response):
        created = store.consolidate_memories()
    assert [memory.name for memory in created] == ["coding-style"]
    assert store.read_memory_file("coding-style") is not None
    assert store.read_memory_file("style-tabs") is store.read_memory_file("style-spaces") is None
    assert store.read_memory_file("project-language") is not None
    assert "coding-style" in store.read_memory_index()
    assert "style-tabs" not in store.read_memory_index()
