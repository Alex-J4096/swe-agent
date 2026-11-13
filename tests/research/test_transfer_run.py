import json
from pathlib import Path

from experiments.a1.fixture import sources
from experiments.transfer_run import (
    approved_command,
    evidence_sequence,
    migrate_a1,
    parse_test_report,
    verified_creation,
)


def test_migration_preserves_learned_helpers_and_cancel_body():
    learned = sources("stable")
    learned["order_service.py"] += "\n# learned implementation detail\n"
    migrated = migrate_a1(learned)
    assert migrated["order_service.py"].endswith("# learned implementation detail\n")
    assert 'self.event_bus.publish("orders"' in migrated["order_service.py"]
    assert 'self.legacy_notifier.send("order.created"' not in migrated["order_service.py"]
    assert migrated["notifications.py"] == learned["notifications.py"]
    assert migrated["README.md"] == learned["README.md"]


def test_test_report_is_read_from_actual_final_answer_without_filling_missing_fields():
    assert parse_test_report("Ran 4 tests; exit 1") is None
    assert parse_test_report('{"tests_run": 4, "exit_code": 1}') is None
    assert parse_test_report('{"tests_run": true, "failure_ids": [], "exit_code": 1}') is None
    report = {"tests_run": 3, "failure_ids": ["a.b.C.test_failure"], "exit_code": 1}
    assert parse_test_report("Reported results:\n" + json.dumps(report)) == report


def test_reading_old_command_in_readme_does_not_prove_current_ci_observed():
    command = "python3 scripts/test_pricing_b.py"
    message = {"role": "tool", "tool_call_id": "read-1", "content": json.dumps({
        "ok": True, "path": "README.md", "content": command})}
    run = {"events": [{"event": "model_request", "component": "agent", "sequence": 7,
                       "request": {"messages": [message]}}]}
    assert evidence_sequence(run, command, ".github/workflows/tests.yml") is None
    message["content"] = json.dumps({"ok": True, "path": ".github/workflows/tests.yml", "content": command})
    assert evidence_sequence(run, command, ".github/workflows/tests.yml") == 7


def test_source_readback_is_not_runtime_creation_verification():
    event = {"event": "tool_result", "component": "agent", "tool": "run_bash",
             "arguments": json.dumps({"command": "cat order_service.py"}),
             "output": {"ok": True, "stdout": '"order.created"'}}
    root = Path("/tmp/example")
    assert not verified_creation({"events": [event]}, root)
    event["arguments"] = json.dumps({"command": "python3 app.py create ORDER-17"})
    event["output"]["stdout"] = json.dumps({"events": [{"event": "order.created", "order_id": "ORDER-17"}]})
    assert verified_creation({"events": [event]}, root)
    assert not approved_command("t1", "python3 scripts/test_pricing_a.py; echo success", root, root)


def test_a1_equivalent_cd_prefix_is_allowed_without_allowing_other_shell_operations():
    root = Path("/tmp/example").resolve()
    assert approved_command("a1", f"cd {root} && python3 app.py create ORDER-17", root, root)
    assert approved_command("a1", "cd .&&python3 app.py create ORDER-17", root, root)
    assert not approved_command("a1", "cd .. && python3 app.py create ORDER-17", root, root)
    assert approved_command("a1", "python3 app.py create ORDER-17 && echo done", root, root)
    assert not approved_command("a1", "python3 app.py create ORDER-17 && cat /etc/passwd", root, root)


def test_a1_local_cli_sequence_really_executes_and_verifies_creation(tmp_path, local_executor):
    from experiments.a1.fixture import create
    from src.runtime.sandbox import SandboxPolicy

    create(tmp_path, "stable")
    command = f'cd {tmp_path} && python3 app.py create ORDER-17 && echo "---" && python3 app.py cancel ORDER-17'
    assert approved_command("a1", command, tmp_path, tmp_path)
    output = local_executor.execute(command, cwd=tmp_path, timeout_seconds=10, policy=SandboxPolicy(tmp_path))
    assert output.exit_code == 0
    event = {"event": "tool_result", "component": "agent", "tool": "run_bash",
             "arguments": json.dumps({"command": command}),
             "output": {"ok": True, "stdout": output.stdout}}
    assert verified_creation({"events": [event]}, tmp_path)


def test_a1_command_sequences_do_not_allow_redirection_or_substitution():
    root = Path("/tmp/example").resolve()
    for command in (
        "python3 app.py create ORDER-17 > output.json",
        "echo x | python3 app.py create ORDER-17",
        "cd ..; python3 app.py create ORDER-17",
        "python3 app.py create $(echo ORDER-17)",
    ):
        assert not approved_command("a1", command, root, root)
