"""The shared retrieval baseline and its asynchronous cache."""
from types import SimpleNamespace

from src.infrastructure.memory import MemoryStore
from src.infrastructure.memory_selection_cache import MemorySelectionCache
from src.runtime.memory_selection_worker import MemorySelectionWorker

from tests.memory.support import scripted_client


def language_store(tmp_path):
    store = MemoryStore(tmp_path, model="test")
    store.write_memory_file("project-language", "project", "The project uses Python", "Use Python 3.13.")
    return store


def test_selects_and_loads_relevant_memories_by_keywords(tmp_path):
    store = language_store(tmp_path)
    store.write_memory_file("user-preference-tabs", "user", "User prefers tabs for indentation", "Always use tabs.")
    messages = [{"role": "user", "content": "Please preserve tab indentation."}]
    assert store.select_relevant_memories(messages) == ["user-preference-tabs.md"]
    assert "Always use tabs." in store.load_memories(messages)


def test_selects_memories_from_llm_json_response(tmp_path):
    store = language_store(tmp_path)
    store.client, _ = scripted_client(lambda request, step: {"content": '["project-language.md"]'})
    assert store.select_relevant_memories([{"role": "user", "content": "What language is this?"}]) == ["project-language.md"]


def test_refines_only_requested_candidates(tmp_path):
    store = language_store(tmp_path)
    store.client, _ = scripted_client(lambda request, step: {"content": '["project-language.md", "unknown.md"]'})
    assert store.refine_relevant_memories(
        [{"role": "user", "content": "What language is this?"}], ["project-language.md"]
    ) == ["project-language.md"]


def test_model_getter_is_evaluated_for_each_llm_request(tmp_path):
    current_model = ["model-a"]
    store = MemoryStore(tmp_path, model_getter=lambda: current_model[0])
    store.write_memory_file("project-language", "project", "The project uses Python", "Use Python 3.13.")
    store.client, requests = scripted_client(lambda request, step: {"content": '["project-language.md"]'})
    messages = [{"role": "user", "content": "What language is this?"}]
    store.select_relevant_memories(messages)
    current_model[0] = "model-b"
    store.select_relevant_memories(messages)
    assert [request["model"] for request in requests] == ["model-a", "model-b"]


def test_selection_cache_accepts_history_extensions():
    cache = MemorySelectionCache()
    messages = [{"role": "user", "content": "Use Python."}]
    cache.put(session_id="s", messages=messages, memory_revision="rev-1", filenames=["project-language.md"])
    assert cache.get_compatible(session_id="s", messages=[*messages, {"role": "assistant", "content": "Okay."}],
                                memory_revision="rev-1") == ["project-language.md"]
    assert cache.get_compatible(session_id="s", messages=messages, memory_revision="rev-2") == []


def test_selection_worker_writes_refined_snapshot_to_cache():
    store = SimpleNamespace(
        memory_revision=lambda: "rev-1",
        select_relevant_memories=lambda *args, **kwargs: ["project-language.md"],
        refine_relevant_memories=lambda *args, **kwargs: ["project-language.md"],
    )
    cache = MemorySelectionCache()
    worker = MemorySelectionWorker(store, cache)
    messages = [{"role": "user", "content": "Use Python."}]
    try:
        future = worker.submit("s", messages)
        assert future is not None
        future.result(timeout=2)
    finally:
        worker.shutdown(wait=True)
    messages.append({"role": "assistant", "content": "Done."})
    assert cache.get_compatible(session_id="s", messages=messages, memory_revision="rev-1") == ["project-language.md"]
