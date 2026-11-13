import json
from unittest.mock import patch

import pytest

from main import parse_args
from src.infrastructure.memory import MemoryStore
from src.infrastructure.memory_revision import evidence_dialogue
from src.tools.memory.revise_memory import (
    ReadMemoryTool,
    UpdateMemoryTool,
    ReadMemoryArgs,
    UpdateMemoryArgs,
)

from tests.memory.support import FIXTURES, seed, tools, transcript, update_args


def test_modes_preserve_default_and_enrich_evidence(tmp_path):
    for mode in ("default", "evidence"):
        store = seed(tmp_path / mode, mode)
        with patch.object(store, "_call_llm", return_value="[]") as request:
            store.extract_memories(transcript())
        assert ("python3 service.py" in request.call_args.args[0]) == (mode == "evidence")
    assert parse_args([]).memory_maintenance == "default"


def test_evidence_pairs_parallel_results_and_keeps_complete_packets():
    messages = transcript()
    parallel = [messages[0], {"role": "assistant", "content": "",
                "tool_calls": messages[1]["tool_calls"] + messages[3]["tool_calls"]}, messages[4], messages[2]]
    _, evidence = evidence_dialogue(parallel)
    assert evidence["run-1"]["arguments"]["command"] == "python3 service.py"
    assert evidence["read-1"]["result"]["path"] == "service.py"
    assert evidence_dialogue(messages, max_chars=10) == ("", {})


def test_edit_evidence_can_accompany_observations_but_cannot_replace_them(tmp_path):
    store = seed(tmp_path)
    revision, context = tools(store)
    revision.evidence["edit-1"] = {"evidence_id": "edit-1", "tool": "edit_file", "result": {"ok": True}}
    read = ReadMemoryTool(revision).run(ReadMemoryArgs(filename="config.md"), context)
    writer = UpdateMemoryTool(revision)
    assert writer.run(update_args(read["version"], evidence_ids=["edit-1"]), context)["error"] == "missing_observation"
    assert writer.run(update_args(read["version"], evidence_ids=["edit-1", "read-1"]), context)["ok"]


def test_actual_t1_failed_run_is_eligible_without_becoming_verification_success(tmp_path):
    data = json.loads((FIXTURES / "t1-failed-observation.json").read_text())
    store = MemoryStore(tmp_path, model="test", maintenance_mode="revise")
    store.write_memory_file("ci-pricing-suite", "project", "CI uses A.", "CI uses scripts/test_pricing_a.py.")
    revision, context = tools(store, data["messages"])
    read = ReadMemoryTool(revision).run(ReadMemoryArgs(filename="ci-pricing-suite.md"), context)
    args = UpdateMemoryArgs(expected_version=read["version"], **data["proposal"])
    result = UpdateMemoryTool(revision).run(args, context)
    assert result["ok"]
    failures = [packet for packet in revision.evidence.values() if packet["evidence_kind"] == "command_failed"]
    assert len(failures) == 1
    assert failures[0]["result"]["ok"] is False and failures[0]["result"]["exit_code"] == 1
    note = store.read_memory_file("ci-pricing-suite")
    assert "command_failed" in note and "test_shipping_is_added" in note


def test_failed_command_alone_can_mark_a_previous_success_claim_suspect(tmp_path):
    messages = transcript()
    messages[2]["content"] = json.dumps({"ok": False, "stdout": "one assertion failed", "stderr": "", "exit_code": 1})
    store = seed(tmp_path)
    revision, context = tools(store, messages)
    read = ReadMemoryTool(revision).run(ReadMemoryArgs(filename="config.md"), context)
    result = UpdateMemoryTool(revision).run(update_args(
        read["version"], description="Previous verification needs review.",
        body="The observed command returned exit code 1 with one assertion failure.",
        status="suspect", evidence_ids=["run-1"]), context)
    assert result["ok"] and "Pending verification" in store.visible_index()


@pytest.mark.parametrize("tool,result", [
    ("run_bash", {"ok": False, "exit_code": -1, "stdout": "", "stderr": "timeout"}),
    ("run_bash", {"ok": False, "exit_code": -9, "stdout": "", "stderr": ""}),
    ("run_bash", {"ok": False, "exit_code": 127, "stdout": "", "stderr": "not found"}),
    ("run_bash", {"ok": False, "exit_code": 126, "stdout": "", "stderr": ""}),
    ("run_bash", {"ok": False, "exit_code": 1, "stdout": "", "stderr": "", "permission_denied": True}),
    ("run_bash", {"ok": False, "exit_code": 1, "stdout": "", "stderr": "", "timed_out": True}),
    ("run_bash", {"ok": False, "exit_code": 1, "stdout": "", "stderr": "", "error": "not executed"}),
    ("run_bash", {"ok": True, "exit_code": 1, "stdout": "", "stderr": ""}),
    ("run_bash", {"ok": False, "exit_code": True, "stdout": "", "stderr": ""}),
    ("edit_file", {"ok": False, "exit_code": 1, "stdout": "", "stderr": ""}),
], ids=[
    "unavailable", "signal", "command-missing", "not-executable", "permission-denied",
    "timeout", "execution-error", "inconsistent-status", "boolean-exit-code", "failed-write",
])
def test_non_execution_and_failed_writes_are_not_eligible(tool, result):
    from src.infrastructure.memory_revision import evidence_kind
    assert evidence_kind(tool, result) is None
