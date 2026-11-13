"""Background maintenance must consume a stable, current transcript."""
from threading import Event

from src.runtime.memory_worker import MemoryWorker


def test_memory_worker_processes_a_snapshot_in_order():
    class Store:
        def __init__(self):
            self.received, self.steps = [], []

        def extract_memories(self, messages, should_commit=None, session_id=None):
            self.received.append(messages)
            self.steps.append("extract")
            return []

        def consolidate_memories(self):
            self.steps.append("consolidate")
            return []

    store = Store()
    worker = MemoryWorker(store)
    messages = [{"role": "user", "content": "original"}]
    try:
        future = worker.submit(messages)
        messages[0]["content"] = "mutated after submit"
        future.result(timeout=2)
    finally:
        worker.shutdown(wait=True)
    assert store.received[0][0]["content"] == "original"
    assert store.steps == ["extract", "consolidate"]


def test_memory_worker_invalidates_running_branch_before_commit():
    started, release = Event(), Event()

    class Store:
        committed = consolidated = False

        def extract_memories(self, messages, should_commit=None, session_id=None):
            started.set()
            release.wait(timeout=2)
            if should_commit is None or should_commit():
                self.committed = True
            return []

        def consolidate_memories(self):
            self.consolidated = True
            return []

    store = Store()
    worker = MemoryWorker(store)
    try:
        future = worker.submit([{"role": "user", "content": "old branch"}])
        assert started.wait(timeout=2)
        worker.invalidate()
        release.set()
        future.result(timeout=2)
    finally:
        release.set()
        worker.shutdown(wait=True)
    assert not store.committed and not store.consolidated
