"""In-memory cache for asynchronous memory selection results."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any


def history_digest(messages: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        messages,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MemorySelection:
    session_id: str
    history_digest: str
    history_length: int
    memory_revision: str
    filenames: tuple[str, ...]


class MemorySelectionCache:
    """Cache the latest selection for each session without persisting it."""

    def __init__(self) -> None:
        self._items: dict[str, MemorySelection] = {}
        self._disabled_sessions: set[str] = set()
        self._lock = threading.Lock()

    def put(
        self,
        *,
        session_id: str,
        messages: list[dict[str, Any]],
        memory_revision: str,
        filenames: list[str],
    ) -> None:
        selection = MemorySelection(
            session_id=session_id,
            history_digest=history_digest(messages),
            history_length=len(messages),
            memory_revision=memory_revision,
            filenames=tuple(filenames),
        )
        with self._lock:
            if session_id in self._disabled_sessions:
                return
            self._items[session_id] = selection

    def get_compatible(
        self,
        *,
        session_id: str,
        messages: list[dict[str, Any]],
        memory_revision: str,
    ) -> list[str]:
        """Return a selection based on a matching history prefix."""
        with self._lock:
            selection = self._items.get(session_id)

        if selection is None:
            return []
        if selection.memory_revision != memory_revision:
            return []
        if len(messages) < selection.history_length:
            return []

        prefix = messages[:selection.history_length]
        if history_digest(prefix) != selection.history_digest:
            return []
        return list(selection.filenames)

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._items.pop(session_id, None)

    def set_enabled(self, session_id: str, enabled: bool) -> None:
        with self._lock:
            if enabled:
                self._disabled_sessions.discard(session_id)
            else:
                self._disabled_sessions.add(session_id)
                self._items.pop(session_id, None)
