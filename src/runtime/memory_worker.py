from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
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

    def submit(
        self,
        messages: list[dict[str, Any]],
    ) -> Future[tuple[list[Memory], list[Memory]]]:
        """Queue an immutable snapshot of one completed agent round."""
        snapshot = deepcopy(messages)
        return self._executor.submit(self._process, snapshot)

    def _process(
        self,
        messages: list[dict[str, Any]],
    ) -> tuple[list[Memory], list[Memory]]:
        extracted: list[Memory] = []
        consolidated: list[Memory] = []

        try:
            extracted = self.store.extract_memories(messages)
        except Exception:
            # Memory maintenance must not affect the agent's next turn.
            pass

        try:
            consolidated = self.store.consolidate_memories()
        except Exception:
            # A failed maintenance pass is safe to retry on a later round.
            pass

        return extracted, consolidated

    def shutdown(self, wait: bool = False) -> None:
        """Stop accepting new rounds while allowing queued work to finish."""
        self._executor.shutdown(wait=wait, cancel_futures=False)
