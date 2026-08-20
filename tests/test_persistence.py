import sqlite3
from io import StringIO
from pathlib import Path

from main import save_session_if_needed
from src.infrastructure.database import (
    AppDatabase,
    ModelProfile,
    ProviderCredential,
    ProviderProfile,
)
from src.infrastructure.session_store import SessionStore
from src.runtime.session import Session
from src.tools.toolset import ToolSet
from src.ui.console import ConsoleRenderer
from src.utils.slash_commands.base import CommandContext
from src.utils.slash_commands.commands.clear import ClearCommand
from src.utils.slash_commands.commands.new import NewSessionCommand
from src.utils.slash_commands.commands.resume import ResumeCommand
from src.utils.slash_commands.commands.sessions import SessionsCommand
from src.utils.slash_commands.commands.status import StatusCommand


def make_store(tmp_path: Path) -> tuple[AppDatabase, SessionStore]:
    database = AppDatabase(tmp_path / "state.db")
    database.upsert_provider(
        ProviderProfile(
            id="provider-1",
            name="Test Provider",
            base_url=None,
            api_key_env="TEST_API_KEY",
        )
    )
    return database, SessionStore(database)


def test_database_v3_migrates_permission_scopes(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE session_state (
            session_id TEXT PRIMARY KEY,
            tool_state_json TEXT NOT NULL DEFAULT '{}',
            approved_prefixes_json TEXT NOT NULL DEFAULT '[]',
            memory_enabled INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    connection.execute("PRAGMA user_version = 3")
    connection.commit()
    connection.close()

    database = AppDatabase(path)
    columns = {
        row[1]
        for row in database.connection.execute("PRAGMA table_info(session_state)")
    }

    assert "approved_scopes_json" in columns
    assert "context_state_json" in columns
    assert database.connection.execute("PRAGMA user_version").fetchone()[0] == 5
    database.close()


def test_database_v4_migrates_context_state(tmp_path: Path) -> None:
    path = tmp_path / "legacy-v4.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE session_state (
            session_id TEXT PRIMARY KEY,
            tool_state_json TEXT NOT NULL DEFAULT '{}',
            approved_prefixes_json TEXT NOT NULL DEFAULT '[]',
            approved_scopes_json TEXT NOT NULL DEFAULT '[]',
            memory_enabled INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    connection.execute("PRAGMA user_version = 4")
    connection.commit()
    connection.close()

    database = AppDatabase(path)
    columns = {
        row[1]
        for row in database.connection.execute("PRAGMA table_info(session_state)")
    }

    assert "context_state_json" in columns
    assert database.connection.execute("PRAGMA user_version").fetchone()[0] == 5
    database.close()


def test_database_persists_provider_and_default_model(tmp_path: Path) -> None:
    database, _ = make_store(tmp_path)
    database.upsert_provider(
        ProviderProfile(
            id="provider-1",
            name="Test Provider",
            base_url="https://example.test/v1",
            api_key_env="TEST_API_KEY",
        )
    )
    database.upsert_model(
        ModelProfile(
            id="model-1",
            provider_id="provider-1",
            model_id="test-model",
            config={"temperature": 0.2},
            is_default=True,
        )
    )

    assert database.get_provider("provider-1") == ProviderProfile(
        id="provider-1",
        name="Test Provider",
        base_url="https://example.test/v1",
        api_key_env="TEST_API_KEY",
    )
    assert database.get_default_model("provider-1") == ModelProfile(
        id="model-1",
        provider_id="provider-1",
        model_id="test-model",
        config={"temperature": 0.2},
        is_default=True,
    )
    database.close()


def test_database_persists_credentials_and_selects_active_default(
    tmp_path: Path,
) -> None:
    database, _ = make_store(tmp_path)
    first = ProviderCredential(
        id="credential-1",
        provider_id="provider-1",
        name="personal",
        access_token="token-1",
        expires_at="2030-01-01T00:00:00Z",
        scopes=["models:read"],
        is_default=True,
    )
    second = ProviderCredential(
        id="credential-2",
        provider_id="provider-1",
        name="work",
        access_token="token-2",
        is_default=True,
    )

    database.upsert_credential(first)
    database.upsert_credential(second)

    selected = database.get_default_credential("provider-1")
    assert selected == second
    assert database.connection.execute(
        "SELECT is_default FROM provider_credentials WHERE id = ?",
        (first.id,),
    ).fetchone()[0] == 0
    assert "token-2" not in repr(selected)
    database.close()


def test_session_round_trip_preserves_messages_and_runtime_state(
    tmp_path: Path,
) -> None:
    database, store = make_store(tmp_path)
    session = Session(
        model="test-model",
        provider_id="provider-1",
        session_id="session-1",
        history=[
            {"role": "user", "content": "inspect files"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {"name": "ls", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
        ],
        tool_state={"todo": [{"subject": "inspect files"}]},
        approved_command_prefixes={("git", "status")},
        approved_permission_scopes={"sandbox.network:abc123"},
        memory_enabled=False,
        context_checkpoints=[
            {
                "checkpoint_id": "cp_0123456789abcdef0123456789abcdef",
                "branch_id": "branch_0123456789abcdef0123456789abcdef",
                "history_length": 1,
                "history_digest": "digest",
                "reason": "model_step_1",
                "created_at": "2026-08-20T00:00:00+00:00",
            }
        ],
        context_branch_id="branch_0123456789abcdef0123456789abcdef",
        context_generation=2,
    )

    store.save(session, title="First session", workdir=tmp_path)
    loaded = store.load("session-1")

    assert loaded is not None
    assert loaded.session_id == session.session_id
    assert loaded.provider_id == "provider-1"
    assert loaded.history == session.history
    assert loaded.tool_state == session.tool_state
    assert loaded.approved_command_prefixes == {("git", "status")}
    assert loaded.approved_permission_scopes == {"sandbox.network:abc123"}
    assert loaded.memory_enabled is False
    assert loaded.context_checkpoints == session.context_checkpoints
    assert loaded.context_branch_id == session.context_branch_id
    assert loaded.context_generation == 2
    assert loaded.pending_context_rewind is None
    assert store.list()[0].title == "First session"
    database.close()


def test_session_store_delete_cascades_messages_and_state(tmp_path: Path) -> None:
    database, store = make_store(tmp_path)
    session = store.create("test-model", provider_id="provider-1")

    assert store.delete(session.session_id) is True
    assert store.load(session.session_id) is None
    assert database.connection.execute(
        "SELECT COUNT(*) FROM session_messages WHERE session_id = ?",
        (session.session_id,),
    ).fetchone()[0] == 0
    database.close()


def test_empty_startup_session_is_not_persisted_until_it_has_content(
    tmp_path: Path,
) -> None:
    database, store = make_store(tmp_path)
    session = Session(model="test-model", provider_id="provider-1")

    assert save_session_if_needed(store, session, tmp_path) is False
    assert store.exists(session.session_id) is False

    session.history.append({"role": "user", "content": "hello"})

    assert save_session_if_needed(store, session, tmp_path) is True
    assert store.exists(session.session_id) is True
    database.close()


def test_clear_does_not_persist_an_empty_transient_session(tmp_path: Path) -> None:
    database, store = make_store(tmp_path)
    session = Session(model="test-model", provider_id="provider-1")
    context = CommandContext(
        session=session,
        toolset=ToolSet(),
        provider=object(),
        session_store=store,
        workdir=tmp_path,
    )

    original_branch = session.context_branch_id
    session.context_checkpoints.append({"checkpoint_id": "cp_test"})
    session.context_generation = 2

    ClearCommand().execute([], context)

    assert store.exists(session.session_id) is False
    assert session.context_checkpoints == []
    assert session.context_generation == 0
    assert session.context_branch_id != original_branch
    database.close()


def test_status_command_reports_current_session_metadata(tmp_path: Path) -> None:
    database, store = make_store(tmp_path)
    current = store.create(
        "model-a",
        provider_id="provider-1",
        title="Investigate auth",
        workdir=tmp_path,
    )
    context = CommandContext(
        session=current,
        toolset=ToolSet(),
        provider=object(),
        session_store=store,
        workdir=tmp_path,
    )

    result = StatusCommand().execute([], context)

    assert result.message is not None
    assert "Model:       model-a" in result.message
    assert f"Directory:   {tmp_path}" in result.message
    assert "Thread name: Investigate auth" in result.message
    assert f"Session UID: {current.session_id}" in result.message
    assert f"Context:     {current.context_branch_id}" in result.message
    assert "(0 checkpoints, generation 0)" in result.message
    database.close()


def test_session_commands_create_list_resume_and_clear(tmp_path: Path) -> None:
    database, store = make_store(tmp_path)
    current = Session(model="model-a", provider_id="provider-1")
    context = CommandContext(
        session=current,
        toolset=ToolSet(),
        provider=object(),
        session_store=store,
        workdir=tmp_path,
    )

    created = NewSessionCommand().execute(["demo"], context)
    assert created.message is not None
    created_id = current.session_id
    current.history.append({"role": "user", "content": "hello"})
    current.tool_state["key"] = "value"
    store.save(current)

    listed = SessionsCommand().execute([], context)
    assert listed.message is not None
    assert created_id in listed.message
    assert "demo" in listed.message

    other = store.create("model-b", provider_id="provider-1")
    other.history = [
        {"role": "user", "content": f"message-{index}"}
        for index in range(12)
    ]
    store.save(other)
    resumed = ResumeCommand().execute([other.session_id], context)
    assert resumed.message is not None
    assert current.session_id == other.session_id
    assert current.model == "model-b"
    assert "Recent session history (last 10 messages):" in resumed.message
    assert "\n    message-1\n" not in resumed.message
    assert "\n    message-2\n" in resumed.message
    assert "message-11" in resumed.message

    current.history.append({"role": "user", "content": "remove me"})
    current.tool_state["temporary"] = True
    cleared = ClearCommand().execute([], context)
    assert cleared.message is not None
    loaded = store.load(current.session_id)
    assert loaded is not None
    assert loaded.history == []
    assert loaded.tool_state == {}
    database.close()


def test_resume_uses_conversation_renderer_when_available(tmp_path: Path) -> None:
    database, store = make_store(tmp_path)
    session = store.create("model-a", provider_id="provider-1")
    session.history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi there."},
    ]
    store.save(session)

    output = StringIO()
    context = CommandContext(
        session=Session(model="other-model"),
        toolset=ToolSet(),
        provider=object(),
        session_store=store,
        renderer=ConsoleRenderer(output=output, use_color=False),
    )

    result = ResumeCommand().execute([session.session_id], context)

    assert result.message is None
    rendered = output.getvalue()
    assert "Resumed session" in rendered
    assert "› hello" in rendered
    assert "╭─ assistant" in rendered
    assert '"role": "assistant"' not in rendered
    database.close()
