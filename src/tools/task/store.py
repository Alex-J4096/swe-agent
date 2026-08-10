"""Small atomic JSON-file storage primitives used by task managers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import RLock
from uuid import uuid4


def atomic_write_json(path: Path, data: object) -> None:
    """Write JSON without exposing a partially written target file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        temporary_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


class JsonFileStore:
    """Thread-safe JSON object storage for one directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()
        self._lock = RLock()

    def save(self, name: str, data: object) -> None:
        with self._lock:
            atomic_write_json(self.directory / name, data)

    def load(self, name: str) -> object:
        with self._lock:
            return json.loads(
                (self.directory / name).read_text(encoding="utf-8")
            )

    def list(self, pattern: str) -> list[Path]:
        with self._lock:
            if not self.directory.exists():
                return []
            return sorted(self.directory.glob(pattern))
