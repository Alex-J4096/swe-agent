"""Background LLM reranking for memory candidates."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from threading import Lock
from typing import Any

from src.infrastructure.memory import MemoryStore
from src.infrastructure.memory_selection_cache import MemorySelectionCache


class MemorySelectionWorker:
    """Run one latest-wins memory selection task outside the agent loop."""

    CANDIDATE_LIMIT = 12
    RESULT_LIMIT = 5

    def __init__(
        self,
        store: MemoryStore,
        cache: MemorySelectionCache,
    ) -> None:
        self.store = store
        self.cache = cache
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="memory-selection",
        )
        self._lock = Lock()
        self._generation = 0
        self._pending: Future[object] | None = None

    def submit(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
    ) -> Future[object] | None:
        """Queue a snapshot, replacing work that has not started yet."""
        snapshot = deepcopy(messages)
        revision = self.store.memory_revision()
        with self._lock:
            self._generation += 1
            generation = self._generation
            if self._pending is not None and not self._pending.running():
                self._pending.cancel()
            future = self._executor.submit(
                self._process,
                generation,
                session_id,
                snapshot,
                revision,
            )
            self._pending = future
            return future

    def _is_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation

    def _process(
        self,
        generation: int,
        session_id: str,
        messages: list[dict[str, Any]],
        revision: str,
    ) -> None:
        if not self._is_current(generation):
            return

        candidates = self.store.select_relevant_memories(
            messages,
            max_items=self.CANDIDATE_LIMIT,
            use_llm=False,
        )
        if not self._is_current(generation):
            return

        selected = self.store.refine_relevant_memories(
            messages,
            candidates,
            max_items=self.RESULT_LIMIT,
        )
        if selected is None:
            return
        if not self._is_current(generation):
            return
        if self.store.memory_revision() != revision:
            return

        self.cache.put(
            session_id=session_id,
            messages=messages,
            memory_revision=revision,
            filenames=selected,
        )

    def shutdown(self, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=False)
