"""Durable file inboxes with atomic delivery and acknowledgement."""

from __future__ import annotations

import json
import os
import re
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path

from src.tools.task.store import atomic_write_json
from src.teams.protocol import TeamEvent

try:
    import fcntl
except ImportError:  # pragma: no cover - the supported runtime is POSIX.
    fcntl = None


SAFE_PART = re.compile(r"^[A-Za-z0-9_.-]+$")


class FileLock(AbstractContextManager["FileLock"]):
    """A process-shared advisory lock for one local state directory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+")
        if fcntl is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._handle is None:
            return
        if fcntl is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


class MessageDelivery:
    def __init__(self, event: TeamEvent, processing_path: Path) -> None:
        self.event = event
        self.processing_path = processing_path


class FileMessageBus:
    """At-least-once local delivery with explicit ack/requeue operations."""

    def __init__(self, team_root: Path) -> None:
        self.root = team_root.resolve()
        self.inbox_root = self.root / "inbox"
        self.processing_root = self.root / "processing"
        self.processed_root = self.root / "processed"
        self.failed_root = self.root / "failed"
        self.lock_root = self.root / ".locks"

    @staticmethod
    def _validate_part(value: str) -> None:
        if not isinstance(value, str) or not SAFE_PART.fullmatch(value):
            raise ValueError("Invalid message path component")

    def _recipient_dir(self, root: Path, recipient_id: str) -> Path:
        self._validate_part(recipient_id)
        return root / recipient_id

    def send(self, event: TeamEvent) -> None:
        self._validate_part(event.sender_id)
        self._validate_part(event.recipient_id)
        directory = self._recipient_dir(self.inbox_root, event.recipient_id)
        with FileLock(self.lock_root / f"inbox-{event.recipient_id}.lock"):
            atomic_write_json(directory / f"{event.event_id}.json", event.model_dump(mode="json"))

    def poll(self, recipient_id: str, limit: int = 50) -> list[MessageDelivery]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        inbox = self._recipient_dir(self.inbox_root, recipient_id)
        processing = self._recipient_dir(self.processing_root, recipient_id)
        deliveries: list[MessageDelivery] = []
        with FileLock(self.lock_root / f"inbox-{recipient_id}.lock"):
            for source in sorted(inbox.glob("*.json"))[:limit]:
                target = processing / source.name
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.replace(source, target)
                    event = TeamEvent.model_validate(
                        json.loads(target.read_text(encoding="utf-8"))
                    )
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    failed = self._recipient_dir(self.failed_root, recipient_id)
                    failed.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        os.replace(target, failed / source.name)
                    continue
                deliveries.append(MessageDelivery(event, target))
        return deliveries

    def ack(self, delivery: MessageDelivery) -> None:
        processed = self._recipient_dir(
            self.processed_root,
            delivery.event.recipient_id,
        )
        with FileLock(
            self.lock_root / f"inbox-{delivery.event.recipient_id}.lock"
        ):
            processed.mkdir(parents=True, exist_ok=True)
            if delivery.processing_path.exists():
                os.replace(
                    delivery.processing_path,
                    processed / delivery.processing_path.name,
                )

    def requeue(self, delivery: MessageDelivery) -> None:
        inbox = self._recipient_dir(self.inbox_root, delivery.event.recipient_id)
        with FileLock(
            self.lock_root / f"inbox-{delivery.event.recipient_id}.lock"
        ):
            inbox.mkdir(parents=True, exist_ok=True)
            if delivery.processing_path.exists():
                os.replace(delivery.processing_path, inbox / delivery.processing_path.name)

    def pending_count(self, recipient_id: str) -> int:
        return len(list(self._recipient_dir(self.inbox_root, recipient_id).glob("*.json")))

    def recover_processing(
        self,
        recipient_id: str | None = None,
        *,
        older_than_seconds: float = 0,
    ) -> int:
        """Move abandoned in-flight messages back to an inbox.

        A message is moved to ``processing`` before an Agent Loop handles it.
        If that process exits before acking, the file would otherwise be
        invisible forever. Recovery is intentionally explicit so a live worker
        does not race with another worker's in-flight delivery.
        """
        if older_than_seconds < 0:
            raise ValueError("older_than_seconds cannot be negative")
        recipients = [recipient_id] if recipient_id is not None else [
            path.name for path in self.processing_root.iterdir()
            if path.is_dir()
        ] if self.processing_root.exists() else []
        recovered = 0
        now = datetime.now(timezone.utc).timestamp()
        for current_recipient in recipients:
            self._validate_part(current_recipient)
            processing = self._recipient_dir(self.processing_root, current_recipient)
            inbox = self._recipient_dir(self.inbox_root, current_recipient)
            with FileLock(self.lock_root / f"inbox-{current_recipient}.lock"):
                if not processing.exists():
                    continue
                inbox.mkdir(parents=True, exist_ok=True)
                for source in processing.glob("*.json"):
                    age = now - source.stat().st_mtime
                    if age < older_than_seconds:
                        continue
                    target = inbox / source.name
                    if target.exists():
                        continue
                    os.replace(source, target)
                    recovered += 1
        return recovered
