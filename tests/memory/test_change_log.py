import json
from unittest.mock import patch

import pytest

from src.infrastructure.experiment_log import configure_experiment_log, stop_experiment_log
from src.infrastructure.memory import MemoryStore
from src.runtime.memory_worker import MemoryWorker


def test_logs_committed_note_and_index_changes(tmp_path):
    log = tmp_path / "events.jsonl"
    store = MemoryStore(tmp_path, client=object(), model="test")
    store.CONSOLIDATE_THRESHOLD = 1
    extracted = {"name": "config", "description": "Config location", "type": "project",
                 "body": "settings.toml", "scope": "project"}
    replacement = dict(extracted, name="current-config", body="app.toml")
    responses = [json.dumps([extracted]), json.dumps({"memories": [replacement], "remove": ["config.md"]})]
    configure_experiment_log(log)
    worker = MemoryWorker(store)
    try:
        with patch.object(store, "_call_llm", side_effect=responses):
            worker.submit([], session_id="session-A").result()
        path = store.write_memory_file("current-config", "project", "Config location", "new.toml")
        with patch("pathlib.Path.replace", side_effect=PermissionError("denied")):
            with pytest.raises(PermissionError):
                store.write_memory_file("current-config", "project", "Config location", "failed")
    finally:
        worker.shutdown(wait=True)
        stop_experiment_log()

    events = [json.loads(line) for line in log.read_text().splitlines()]
    changes = [e for e in events if e["event"] == "memory_change"]
    notes = [e for e in changes if e["target"] == "note"]
    assert [e["operation"] for e in notes] == ["create", "create", "delete", "update"]
    assert notes[0]["before"] is None and notes[0]["source"] == "extract"
    assert notes[2]["before"] == notes[0]["after"] and notes[2]["after"] is None
    assert notes[2]["session_id"] == "session-A" and notes[2]["source"] == "consolidate"
    assert notes[1]["maintenance_id"] == notes[2]["maintenance_id"] != notes[0]["maintenance_id"]
    assert notes[3]["before"] == notes[1]["after"] and notes[3]["after"] == path.read_text()
    indices = [e for e in changes if e["target"] == "index"]
    assert len(indices) == 3 and indices[-1]["after"] == store.memory_index.read_text()
    assert events[-2]["event"] == "memory_change_error"
