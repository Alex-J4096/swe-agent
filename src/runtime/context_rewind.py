"""Checkpointed context rewind with immutable branch transcripts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.runtime.session import Session


CHECKPOINT_ID_PATTERN = re.compile(r"cp_[0-9a-f]{32}")
BRANCH_ID_PATTERN = re.compile(r"branch_[0-9a-f]{32}")


@dataclass(frozen=True)
class ContextCheckpoint:
    checkpoint_id: str
    branch_id: str
    history_length: int
    history_digest: str
    reason: str
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "branch_id": self.branch_id,
            "history_length": self.history_length,
            "history_digest": self.history_digest,
            "reason": self.reason,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ContextRewindResult:
    checkpoint_id: str
    abandoned_branch_id: str
    new_branch_id: str
    transcript_path: Path
    discarded_messages: int
    long_term_candidates: tuple[str, ...]


class ContextCheckpointManager:
    """Persist context snapshots and apply model-requested history rewinds."""

    MAX_ACTIVE_CHECKPOINTS = 32
    MAX_PROMPT_CHECKPOINTS = 8

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir.expanduser().resolve()
        self.root = self.workdir / ".swe-agent" / "context"

    @staticmethod
    def _history_digest(messages: list[dict[str, Any]]) -> str:
        payload = json.dumps(
            messages,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _session_key(session_id: str) -> str:
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]

    def _session_root(self, session: Session) -> Path:
        return self.root / self._session_key(session.session_id)

    def _checkpoint_path(self, session: Session, checkpoint_id: str) -> Path:
        if not CHECKPOINT_ID_PATTERN.fullmatch(checkpoint_id):
            raise ValueError("Invalid context checkpoint ID")
        return self._session_root(session) / "checkpoints" / f"{checkpoint_id}.jsonl"

    def _branch_path(self, session: Session, branch_id: str) -> Path:
        if not BRANCH_ID_PATTERN.fullmatch(branch_id):
            raise ValueError("Invalid context branch ID")
        return self._session_root(session) / "branches" / f"{branch_id}.jsonl"

    @staticmethod
    def _new_branch_id() -> str:
        return f"branch_{uuid4().hex}"

    @staticmethod
    def _write_jsonl_atomic(
        path: Path,
        messages: list[dict[str, Any]],
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                for message in messages:
                    stream.write(
                        json.dumps(
                            message,
                            ensure_ascii=False,
                            default=str,
                        )
                        + "\n"
                    )
            temporary.replace(path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(
                        f"Invalid checkpoint record at line {line_number}"
                    )
                messages.append(value)
        return messages

    @staticmethod
    def _checkpoint_from_record(record: Any) -> ContextCheckpoint | None:
        if not isinstance(record, dict):
            return None
        try:
            checkpoint = ContextCheckpoint(
                checkpoint_id=str(record["checkpoint_id"]),
                branch_id=str(record["branch_id"]),
                history_length=int(record["history_length"]),
                history_digest=str(record["history_digest"]),
                reason=str(record.get("reason", "model_step")),
                created_at=str(record["created_at"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if not CHECKPOINT_ID_PATTERN.fullmatch(checkpoint.checkpoint_id):
            return None
        if not BRANCH_ID_PATTERN.fullmatch(checkpoint.branch_id):
            return None
        if checkpoint.history_length < 0:
            return None
        return checkpoint

    def available(self, session: Session) -> list[ContextCheckpoint]:
        checkpoints: list[ContextCheckpoint] = []
        for record in session.context_checkpoints:
            checkpoint = self._checkpoint_from_record(record)
            if checkpoint is None:
                continue
            if not self._checkpoint_path(
                session,
                checkpoint.checkpoint_id,
            ).is_file():
                continue
            checkpoints.append(checkpoint)
        return checkpoints

    def create_checkpoint(
        self,
        session: Session,
        *,
        reason: str = "model_step",
    ) -> ContextCheckpoint:
        if not BRANCH_ID_PATTERN.fullmatch(session.context_branch_id):
            session.context_branch_id = self._new_branch_id()

        digest = self._history_digest(session.history)
        available = self.available(session)
        if available:
            latest = available[-1]
            if (
                latest.branch_id == session.context_branch_id
                and latest.history_digest == digest
            ):
                return latest

        checkpoint = ContextCheckpoint(
            checkpoint_id=f"cp_{uuid4().hex}",
            branch_id=session.context_branch_id,
            history_length=len(session.history),
            history_digest=digest,
            reason=reason.strip() or "model_step",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._write_jsonl_atomic(
            self._checkpoint_path(session, checkpoint.checkpoint_id),
            session.history,
        )
        session.context_checkpoints.append(checkpoint.as_dict())
        session.context_checkpoints[:] = session.context_checkpoints[
            -self.MAX_ACTIVE_CHECKPOINTS:
        ]
        return checkpoint

    def prompt(self, session: Session) -> str:
        checkpoints = self.available(session)[-self.MAX_PROMPT_CHECKPOINTS:]
        if not checkpoints:
            return ""
        lines = [
            "Context rewind is available for removing irrelevant working history.",
            "It rewrites only conversation memory; files, commands, network calls, "
            "approvals, tasks, and team events are not rolled back.",
            "Call rewind_context only as the sole tool call in a model step, and "
            "include all completed work and external effects in carry_forward.",
            "Available checkpoints:",
        ]
        lines.extend(
            f"- {checkpoint.checkpoint_id}: {checkpoint.reason}, "
            f"{checkpoint.history_length} messages"
            for checkpoint in checkpoints
        )
        return "\n".join(lines)

    def apply_pending(self, session: Session) -> ContextRewindResult | None:
        pending = session.pending_context_rewind
        if pending is None:
            return None
        session.pending_context_rewind = None

        checkpoint_id = pending.get("checkpoint_id")
        carry_forward = pending.get("carry_forward")
        if not isinstance(checkpoint_id, str) or not isinstance(carry_forward, dict):
            raise ValueError("Invalid pending context rewind")

        checkpoints = self.available(session)
        target_index = next(
            (
                index
                for index, checkpoint in enumerate(checkpoints)
                if checkpoint.checkpoint_id == checkpoint_id
            ),
            None,
        )
        if target_index is None:
            raise ValueError(f"Unknown context checkpoint: {checkpoint_id}")
        target = checkpoints[target_index]
        checkpoint_path = self._checkpoint_path(session, target.checkpoint_id)
        checkpoint_history = self._read_jsonl(checkpoint_path)
        if self._history_digest(checkpoint_history) != target.history_digest:
            raise ValueError("Context checkpoint snapshot failed integrity validation")

        abandoned_branch_id = session.context_branch_id
        if not BRANCH_ID_PATTERN.fullmatch(abandoned_branch_id):
            abandoned_branch_id = self._new_branch_id()
        transcript_path = self._branch_path(session, abandoned_branch_id)
        if transcript_path.exists():
            abandoned_branch_id = self._new_branch_id()
            transcript_path = self._branch_path(session, abandoned_branch_id)
        self._write_jsonl_atomic(transcript_path, session.history)

        new_branch_id = self._new_branch_id()
        long_term_candidates = carry_forward.get("long_term_candidates", [])
        if not isinstance(long_term_candidates, list):
            long_term_candidates = []
        candidates = tuple(
            item.strip()
            for item in long_term_candidates
            if isinstance(item, str) and item.strip()
        )
        try:
            displayed_path = transcript_path.relative_to(self.workdir).as_posix()
        except ValueError:
            displayed_path = transcript_path.as_posix()
        bridge = {
            "role": "user",
            "content": (
                "[Context rewind applied. This is an internal message from the "
                "agent's future working state, not a new user request. External "
                "side effects were not rolled back.]\n"
                f"Abandoned branch transcript: {displayed_path}\n"
                f"Carry-forward:\n{json.dumps(carry_forward, ensure_ascii=False, indent=2)}"
            ),
        }
        discarded_messages = max(0, len(session.history) - len(checkpoint_history))
        session.history[:] = [*checkpoint_history, bridge]
        session.context_checkpoints[:] = [
            checkpoint.as_dict()
            for checkpoint in checkpoints[: target_index + 1]
        ]
        session.context_branch_id = new_branch_id
        session.context_generation += 1

        return ContextRewindResult(
            checkpoint_id=checkpoint_id,
            abandoned_branch_id=abandoned_branch_id,
            new_branch_id=new_branch_id,
            transcript_path=transcript_path,
            discarded_messages=discarded_messages,
            long_term_candidates=candidates,
        )

    @staticmethod
    def clear(session: Session) -> None:
        session.context_checkpoints.clear()
        session.context_branch_id = ContextCheckpointManager._new_branch_id()
        session.context_generation = 0
        session.pending_context_rewind = None
