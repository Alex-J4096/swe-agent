from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from threading import Lock
from typing import Any

from src.infrastructure.memory import Memory, MemoryStore


class MemoryWorker:
    """Run memory extraction and consolidation outside the agent loop."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="memory-worker",
        )
        self._lock = Lock()
        self._generation = 0
        self._futures: set[Future[tuple[list[Memory], list[Memory]]]] = set()

    def submit(
        self,
        messages: list[dict[str, Any]],
    ) -> Future[tuple[list[Memory], list[Memory]]]:
        """Queue an immutable snapshot of one completed agent round."""
        snapshot = deepcopy(messages)
        with self._lock:
            generation = self._generation
            future = self._executor.submit(self._process, generation, snapshot)
            self._futures.add(future)
        future.add_done_callback(self._discard_future)
        return future

    def _discard_future(
        self,
        future: Future[tuple[list[Memory], list[Memory]]],
    ) -> None:
        with self._lock:
            self._futures.discard(future)

    def _is_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation

    def invalidate(self) -> None:
        """Cancel memory writes sourced from a context branch that was rewound."""
        with self._lock:
            self._generation += 1
            futures = list(self._futures)
        for future in futures:
            if not future.running():
                future.cancel()

    def _process(
        self,
        generation: int,
        messages: list[dict[str, Any]],
    ) -> tuple[list[Memory], list[Memory]]:
        extracted: list[Memory] = []
        consolidated: list[Memory] = []

        if not self._is_current(generation):
            return extracted, consolidated

        try:
            extracted = self.store.extract_memories(
                messages,
                should_commit=lambda: self._is_current(generation),
            )
        except Exception:
            # Memory maintenance must not affect the agent's next turn.
            pass

        if not self._is_current(generation):
            return extracted, consolidated

        try:
            consolidated = self.store.consolidate_memories()
        except Exception:
            # A failed maintenance pass is safe to retry on a later round.
            pass

        return extracted, consolidated

    def shutdown(self, wait: bool = False) -> None:
        """Stop accepting new rounds while allowing queued work to finish."""
        self._executor.shutdown(wait=wait, cancel_futures=False)
