"""SQLite database and configuration persistence primitives."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 5


@dataclass(frozen=True)
class ProviderProfile:
    id: str
    name: str
    base_url: str | None
    api_key_env: str | None


@dataclass(frozen=True)
class ModelProfile:
    id: str
    provider_id: str
    model_id: str
    config: dict[str, Any]
    is_default: bool


@dataclass(frozen=True)
class ProviderCredential:
    id: str
    provider_id: str
    name: str
    access_token: str = field(repr=False)
    credential_type: str = "access_token"
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: str | None = None
    scopes: list[str] = field(default_factory=list)
    is_default: bool = False
    is_active: bool = True


def default_database_path() -> Path:
    configured_path = os.getenv("SWE_AGENT_DB_PATH")
    if configured_path:
        return Path(configured_path).expanduser()
    return Path.home() / ".swe-agent" / "state.db"


class AppDatabase:
    """Own the SQLite connection and the application-level schema."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                self.path.chmod(0o600)

        self.connection = sqlite3.connect(self.path, timeout=5.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        if str(self.path) != ":memory:":
            self.connection.execute("PRAGMA journal_mode = WAL")
        self.migrate()
        if str(self.path) != ":memory:":
            self.path.chmod(0o600)

    def close(self) -> None:
        self.connection.close()

    def migrate(self) -> None:
        current_version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if current_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database version {current_version} is newer than supported "
                f"version {SCHEMA_VERSION}."
            )

        if current_version < 1:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS provider_profiles (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    base_url TEXT,
                    api_key_env TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS model_profiles (
                    id TEXT PRIMARY KEY,
                    provider_id TEXT NOT NULL
                        REFERENCES provider_profiles(id) ON DELETE CASCADE,
                    model_id TEXT NOT NULL,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    is_default INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(provider_id, model_id)
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    provider_id TEXT REFERENCES provider_profiles(id),
                    model TEXT NOT NULL,
                    workdir TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS session_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL
                        REFERENCES sessions(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(session_id, sequence)
                );

                CREATE TABLE IF NOT EXISTS session_state (
                    session_id TEXT PRIMARY KEY
                        REFERENCES sessions(id) ON DELETE CASCADE,
                    tool_state_json TEXT NOT NULL DEFAULT '{}',
                    approved_prefixes_json TEXT NOT NULL DEFAULT '[]'
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_updated_at
                    ON sessions(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_session_messages_session
                    ON session_messages(session_id, sequence);
                """
            )

        if current_version < 2:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS provider_credentials (
                    id TEXT PRIMARY KEY,
                    provider_id TEXT NOT NULL
                        REFERENCES provider_profiles(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    credential_type TEXT NOT NULL DEFAULT 'access_token',
                    access_token TEXT NOT NULL,
                    refresh_token TEXT,
                    expires_at TEXT,
                    scopes_json TEXT NOT NULL DEFAULT '[]',
                    is_default INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(provider_id, name)
                );

                CREATE INDEX IF NOT EXISTS idx_provider_credentials_provider
                    ON provider_credentials(provider_id, is_active, updated_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_default_credential
                    ON provider_credentials(provider_id)
                    WHERE is_default = 1;

                PRAGMA user_version = 2;
                """
            )

        if current_version < 3:
            self.connection.execute(
                "ALTER TABLE session_state ADD COLUMN memory_enabled "
                "INTEGER NOT NULL DEFAULT 1"
            )
            self.connection.execute("PRAGMA user_version = 3")
        if current_version < 4:
            self.connection.execute(
                "ALTER TABLE session_state ADD COLUMN approved_scopes_json "
                "TEXT NOT NULL DEFAULT '[]'"
            )
            self.connection.execute("PRAGMA user_version = 4")
        if current_version < 5:
            self.connection.execute(
                "ALTER TABLE session_state ADD COLUMN context_state_json "
                "TEXT NOT NULL DEFAULT '{}'"
            )
            self.connection.execute("PRAGMA user_version = 5")
        self.connection.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def upsert_provider(self, profile: ProviderProfile) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO provider_profiles (id, name, base_url, api_key_env)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    base_url = excluded.base_url,
                    api_key_env = excluded.api_key_env,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (profile.id, profile.name, profile.base_url, profile.api_key_env),
            )

    def get_provider(self, provider_id: str) -> ProviderProfile | None:
        row = self.connection.execute(
            "SELECT id, name, base_url, api_key_env FROM provider_profiles WHERE id = ?",
            (provider_id,),
        ).fetchone()
        if row is None:
            return None
        return ProviderProfile(**dict(row))

    def list_providers(self) -> list[ProviderProfile]:
        rows = self.connection.execute(
            """
            SELECT id, name, base_url, api_key_env
            FROM provider_profiles
            ORDER BY name COLLATE NOCASE
            """
        ).fetchall()
        return [ProviderProfile(**dict(row)) for row in rows]

    def upsert_model(self, profile: ModelProfile) -> None:
        config_json = json.dumps(profile.config, ensure_ascii=False, sort_keys=True)
        with self.transaction() as connection:
            if profile.is_default:
                connection.execute(
                    "UPDATE model_profiles SET is_default = 0 WHERE provider_id = ?",
                    (profile.provider_id,),
                )
            connection.execute(
                """
                INSERT INTO model_profiles
                    (id, provider_id, model_id, config_json, is_default)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    provider_id = excluded.provider_id,
                    model_id = excluded.model_id,
                    config_json = excluded.config_json,
                    is_default = excluded.is_default,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    profile.id,
                    profile.provider_id,
                    profile.model_id,
                    config_json,
                    int(profile.is_default),
                ),
            )

    def get_default_model(self, provider_id: str) -> ModelProfile | None:
        row = self.connection.execute(
            """
            SELECT id, provider_id, model_id, config_json, is_default
            FROM model_profiles
            WHERE provider_id = ?
            ORDER BY is_default DESC, updated_at DESC
            LIMIT 1
            """,
            (provider_id,),
        ).fetchone()
        if row is None:
            return None
        return ModelProfile(
            id=row["id"],
            provider_id=row["provider_id"],
            model_id=row["model_id"],
            config=json.loads(row["config_json"]),
            is_default=bool(row["is_default"]),
        )

    def upsert_credential(self, credential: ProviderCredential) -> None:
        scopes_json = json.dumps(credential.scopes, ensure_ascii=False, sort_keys=True)
        with self.transaction() as connection:
            if credential.is_default:
                connection.execute(
                    "UPDATE provider_credentials SET is_default = 0 WHERE provider_id = ?",
                    (credential.provider_id,),
                )
            connection.execute(
                """
                INSERT INTO provider_credentials
                    (id, provider_id, name, credential_type, access_token,
                     refresh_token, expires_at, scopes_json, is_default, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    provider_id = excluded.provider_id,
                    name = excluded.name,
                    credential_type = excluded.credential_type,
                    access_token = excluded.access_token,
                    refresh_token = excluded.refresh_token,
                    expires_at = excluded.expires_at,
                    scopes_json = excluded.scopes_json,
                    is_default = excluded.is_default,
                    is_active = excluded.is_active,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    credential.id,
                    credential.provider_id,
                    credential.name,
                    credential.credential_type,
                    credential.access_token,
                    credential.refresh_token,
                    credential.expires_at,
                    scopes_json,
                    int(credential.is_default),
                    int(credential.is_active),
                ),
            )

    def get_default_credential(
        self,
        provider_id: str,
    ) -> ProviderCredential | None:
        row = self.connection.execute(
            """
            SELECT id, provider_id, name, credential_type, access_token,
                   refresh_token, expires_at, scopes_json, is_default, is_active
            FROM provider_credentials
            WHERE provider_id = ? AND is_active = 1
            ORDER BY is_default DESC, updated_at DESC
            LIMIT 1
            """,
            (provider_id,),
        ).fetchone()
        if row is None:
            return None
        return ProviderCredential(
            id=row["id"],
            provider_id=row["provider_id"],
            name=row["name"],
            credential_type=row["credential_type"],
            access_token=row["access_token"],
            refresh_token=row["refresh_token"],
            expires_at=row["expires_at"],
            scopes=json.loads(row["scopes_json"]),
            is_default=bool(row["is_default"]),
            is_active=bool(row["is_active"]),
        )
