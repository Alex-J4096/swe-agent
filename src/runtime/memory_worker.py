from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from threading import Lock
from typing import Any

from src.infrastructure.experiment_log import experiment_log_status, memory_maintenance, record_event
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
        session_id: str | None = None,
    ) -> Future[tuple[list[Memory], list[Memory]]]:
        """Queue an immutable snapshot of one completed agent round."""
        snapshot = deepcopy(messages)
        with self._lock:
            generation = self._generation
            path, run_id = experiment_log_status()
            future = self._executor.submit(
                self._process,
                generation,
                snapshot,
                session_id,
                run_id if path else None,
            )
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
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> tuple[list[Memory], list[Memory]]:
        extracted: list[Memory] = []
        consolidated: list[Memory] = []

        if not self._is_current(generation):
            return extracted, consolidated

        mode = getattr(self.store, "maintenance_mode", "default")
        source = "revise" if mode == "revise" else "extract"
        try:
            with memory_maintenance(run_id, session_id, source):
                record_event("memory_maintenance_started", mode=mode)
                extracted = self.store.extract_memories(
                    messages,
                    should_commit=lambda: self._is_current(generation),
                    session_id=session_id,
                )
        except Exception as exc:
            with memory_maintenance(run_id, session_id, source):
                record_event("memory_maintenance_error", mode=mode, error_type=type(exc).__name__)

        if not self._is_current(generation):
            return extracted, consolidated
        if mode == "revise":
            # The legacy consolidator rewrites whole notes without evidence.
            with memory_maintenance(run_id, session_id, source):
                record_event("memory_consolidation_skipped", reason="revision_mode")
            return extracted, consolidated

        try:
            with memory_maintenance(run_id, session_id, "consolidate"):
                consolidated = self.store.consolidate_memories()
        except Exception:
            # A failed maintenance pass is safe to retry on a later round.
            pass

        return extracted, consolidated

    def shutdown(self, wait: bool = False) -> None:
        """Stop accepting new rounds while allowing queued work to finish."""
        self._executor.shutdown(wait=wait, cancel_futures=False)
