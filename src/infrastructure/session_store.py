"""Persistence for runtime sessions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.infrastructure.database import AppDatabase
from src.runtime.session import Session


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    title: str | None
    model: str
    provider_id: str | None
    workdir: str | None
    status: str
    message_count: int
    updated_at: str


class SessionStore:
    def __init__(self, database: AppDatabase) -> None:
        self.database = database

    def create(
        self,
        model: str,
        *,
        provider_id: str | None = None,
        title: str | None = None,
        workdir: Path | None = None,
    ) -> Session:
        session = Session(
            model=model,
            provider_id=provider_id,
        )
        self.save(session, title=title, workdir=workdir)
        return session

    def save(
        self,
        session: Session,
        *,
        title: str | None = None,
        workdir: Path | None = None,
    ) -> None:
        approved_prefixes = [list(prefix) for prefix in session.approved_command_prefixes]
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT title, workdir FROM sessions WHERE id = ?",
                (session.session_id,),
            ).fetchone()
            stored_title = title if title is not None else (
                existing["title"] if existing is not None else None
            )
            stored_workdir = str(workdir) if workdir is not None else (
                existing["workdir"] if existing is not None else None
            )
            connection.execute(
                """
                INSERT INTO sessions
                    (id, title, provider_id, model, workdir, status)
                VALUES (?, ?, ?, ?, ?, 'active')
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    provider_id = excluded.provider_id,
                    model = excluded.model,
                    workdir = excluded.workdir,
                    status = 'active',
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    session.session_id,
                    stored_title,
                    session.provider_id,
                    session.model,
                    stored_workdir,
                ),
            )
            connection.execute(
                "DELETE FROM session_messages WHERE session_id = ?",
                (session.session_id,),
            )
            connection.executemany(
                """
                INSERT INTO session_messages
                    (session_id, sequence, role, content, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        session.session_id,
                        sequence,
                        str(message.get("role", "unknown")),
                        message.get("content"),
                        json.dumps(message, ensure_ascii=False, sort_keys=True),
                    )
                    for sequence, message in enumerate(session.history)
                ],
            )
            connection.execute(
                """
                INSERT INTO session_state
                    (session_id, tool_state_json, approved_prefixes_json)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    tool_state_json = excluded.tool_state_json,
                    approved_prefixes_json = excluded.approved_prefixes_json
                """,
                (
                    session.session_id,
                    json.dumps(session.tool_state, ensure_ascii=False, sort_keys=True),
                    json.dumps(approved_prefixes, ensure_ascii=False, sort_keys=True),
                ),
            )

    def load(self, session_id: str) -> Session | None:
        row = self.database.connection.execute(
            "SELECT id, provider_id, model FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None

        messages = self.database.connection.execute(
            """
            SELECT payload_json
            FROM session_messages
            WHERE session_id = ?
            ORDER BY sequence
            """,
            (session_id,),
        ).fetchall()
        state = self.database.connection.execute(
            """
            SELECT tool_state_json, approved_prefixes_json
            FROM session_state
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()

        tool_state: dict[str, Any] = {}
        approved_prefixes: set[tuple[str, ...]] = set()
        if state is not None:
            tool_state = json.loads(state["tool_state_json"])
            approved_prefixes = {
                tuple(prefix)
                for prefix in json.loads(state["approved_prefixes_json"])
            }

        return Session(
            model=row["model"],
            session_id=row["id"],
            provider_id=row["provider_id"],
            history=[json.loads(message["payload_json"]) for message in messages],
            tool_state=tool_state,
            approved_command_prefixes=approved_prefixes,
            running=True,
        )

    def exists(self, session_id: str) -> bool:
        """Return whether a session has already been persisted."""
        row = self.database.connection.execute(
            "SELECT 1 FROM sessions WHERE id = ? LIMIT 1",
            (session_id,),
        ).fetchone()
        return row is not None

    def list(self, limit: int = 20) -> list[SessionSummary]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        rows = self.database.connection.execute(
            """
            SELECT s.id, s.title, s.model, s.provider_id, s.workdir, s.status,
                   s.updated_at, COUNT(m.id) AS message_count
            FROM sessions AS s
            LEFT JOIN session_messages AS m ON m.session_id = s.id
            GROUP BY s.id
            ORDER BY s.updated_at DESC, s.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            SessionSummary(
                session_id=row["id"],
                title=row["title"],
                model=row["model"],
                provider_id=row["provider_id"],
                workdir=row["workdir"],
                status=row["status"],
                message_count=row["message_count"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def get_summary(self, session_id: str) -> SessionSummary | None:
        """Return persisted metadata for one session without loading its history."""
        row = self.database.connection.execute(
            """
            SELECT s.id, s.title, s.model, s.provider_id, s.workdir, s.status,
                   s.updated_at, COUNT(m.id) AS message_count
            FROM sessions AS s
            LEFT JOIN session_messages AS m ON m.session_id = s.id
            WHERE s.id = ?
            GROUP BY s.id
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            return None

        return SessionSummary(
            session_id=row["id"],
            title=row["title"],
            model=row["model"],
            provider_id=row["provider_id"],
            workdir=row["workdir"],
            status=row["status"],
            message_count=row["message_count"],
            updated_at=row["updated_at"],
        )

    def delete(self, session_id: str) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM sessions WHERE id = ?",
                (session_id,),
            )
            return cursor.rowcount > 0

    def update_status(self, session_id: str, status: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, session_id),
            )
