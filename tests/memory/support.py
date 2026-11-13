"""Deterministic model responses and observations for memory revision tests."""
import json
from pathlib import Path
from types import SimpleNamespace

from openai.types.chat import ChatCompletion

from src.infrastructure.memory import MemoryStore
from src.infrastructure.memory_revision import evidence_dialogue
from src.runtime.session import Session
from src.tools.base import ToolContext
from src.tools.memory.revise_memory import (
    RevisionContext,
    ReadMemoryTool,
    UpdateMemoryTool,
    ReadMemoryArgs,
    UpdateMemoryArgs,
    CreateMemoryArgs,
)


OLD = "The service reads settings.toml."
NEW = "The service reads app.toml."
VERIFY = "Run python3 service.py to verify changes."
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "memory_revision"


def transcript(ok=True):
    return [
        {"role": "user", "content": "Change the service timeout in settings.toml."},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "run-1", "function": {"name": "run_bash", "arguments": '{"command":"python3 service.py"}'}}
        ]},
        {"role": "tool", "tool_call_id": "run-1", "content": json.dumps({"ok": ok, "stdout": "30\n"})},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "read-1", "function": {"name": "read_file", "arguments": '{"file_path":"service.py"}'}}
        ]},
        {"role": "tool", "tool_call_id": "read-1", "content": json.dumps({"ok": True, "path": "service.py", "content": 'config = "app.toml"'})},
    ]


def seed(tmp_path, mode="revise"):
    store = MemoryStore(tmp_path, client=object(), model="test", maintenance_mode=mode)
    store.write_memory_file("config", "project", OLD, OLD + "\n" + VERIFY)
    return store


def tools(store, messages=None, should_commit=None):
    _, evidence = evidence_dialogue(messages or transcript())
    revision = RevisionContext(store, evidence, should_commit, "parent")
    context = ToolContext(session=Session(model="test"))
    return revision, context


def update_args(version, **changes):
    values = dict(filename="config.md", expected_version=version, description=NEW,
                  body=NEW + "\n" + VERIFY, status="active", evidence_ids=["read-1"],
                  reason="Current service.py loads app.toml.")
    values.update(changes)
    return UpdateMemoryArgs(**values)


def read_then_update(store, **changes):
    revision, context = tools(store)
    read = ReadMemoryTool(revision).run(ReadMemoryArgs(filename="config.md"), context)
    return UpdateMemoryTool(revision).run(update_args(read["version"], **changes), context)


def scripted_client(handler):
    requests = []
    def create(**kwargs):
        requests.append(kwargs)
        message = handler(kwargs, len(requests))
        return ChatCompletion(id=f"response-{len(requests)}", created=1, model="test",
                              object="chat.completion", choices=[{
                                  "index": 0, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                                  "message": {"role": "assistant", **message},
                              }], usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), requests


def call(name, arguments):
    return {"content": "", "tool_calls": [{"id": "call-" + name, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}]}


def last_output(request):
    return json.loads(request["messages"][-1]["content"])


def revision_script(request, step):
    if step == 1:
        return call("search_memory", {"query": "service configuration"})
    if step == 2:
        return call("read_memory", {"filename": last_output(request)["memories"][0]["filename"]})
    if step == 3:
        return call("update_memory", update_args(last_output(request)["version"]).model_dump())
    if step == 4:
        assert last_output(request)["ok"]
        return call("read_memory", {"filename": "config.md"})
    assert last_output(request)["body"] == NEW + "\n" + VERIFY
    return {"content": '{"kept":[],"memories":[]}'}


def creation_args(**changes):
    values = dict(name="notification-wiring", description="The service reads app.toml.",
                  body=NEW + "\n" + VERIFY, status="active", evidence_ids=["read-1"],
                  reason="Current source establishes the project configuration.")
    values.update(changes)
    return CreateMemoryArgs(**values)
