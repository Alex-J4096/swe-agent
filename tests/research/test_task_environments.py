"""Research graders must reject plausible but ineffective task outcomes."""
import json
import shlex

import pytest

from experiments.a1 import fixture as a1
from experiments.a1.evaluate import grade as grade_a1
from experiments.environments import patch_cancellation
from experiments.t1 import fixture as t1
from experiments.t1.evaluate import CASES, FAILURES, PREFIX, TraceExecutor, grade as grade_t1
from src.runtime.sandbox import SandboxPolicy


def test_a1_old_backend_remains_callable_after_migration(tmp_path, local_executor):
    a1.create(tmp_path, "changed")
    patch_cancellation(tmp_path, ("legacy",))
    result = grade_a1(tmp_path, "changed", local_executor)
    assert result["runtime"]["exit_code"] == 0
    assert result["action"] == "inactive"
    assert all(case["legacy_events"] and not case["events"] for case in result["cases"])
    assert not result["strict_success"]


def test_a1_sending_to_both_is_not_single_backend_success(tmp_path, local_executor):
    a1.create(tmp_path, "changed")
    patch_cancellation(tmp_path, ("legacy", "bus"))
    result = grade_a1(tmp_path, "changed", local_executor)
    assert result["success"] and result["action"] == "both"
    assert not result["strict_success"]


def test_a1_fixed_order_number_cannot_pass_two_cancellation_cases(tmp_path, local_executor):
    a1.create(tmp_path, "changed")
    patch_cancellation(tmp_path, ("bus",))
    path = tmp_path / "order_service.py"
    path.write_text(path.read_text().replace('"event": "order.cancelled", "order_id": order_id',
                                            '"event": "order.cancelled", "order_id": "ORDER-17"'))
    result = grade_a1(tmp_path, "changed", local_executor)
    assert result["cases"][0]["delivered_once"]
    assert not result["cases"][1]["delivered_once"]
    assert not result["strict_success"]


def test_a1_notification_before_idempotency_guard_is_rejected(tmp_path, local_executor):
    a1.create(tmp_path, "stable")
    path = tmp_path / "order_service.py"
    text = path.read_text().replace('        if order["status"] == "cancelled":',
        '        self.legacy_notifier.send("order.cancelled", order_id)\n'
        '        if order["status"] == "cancelled":')
    path.write_text(text)
    result = grade_a1(tmp_path, "stable", local_executor)
    assert all(case["delivered_once"] and not case["idempotent"] for case in result["cases"])
    assert not result["strict_success"]


def test_t1_expected_test_failure_is_a_verified_execution(tmp_path, local_executor):
    t1.create(tmp_path, "changed")
    trace = TraceExecutor(local_executor, tmp_path, "changed")
    for label in ("a", "b"):
        result = trace.execute(f"python3 scripts/test_pricing_{label}.py", cwd=tmp_path,
                               timeout_seconds=10, policy=SandboxPolicy(tmp_path))
        assert result.exit_code == 1
    grade = grade_t1(tmp_path, "changed", trace.records, FAILURES["b"])
    assert grade["first_action"] == "inactive"
    assert grade["strict_success"] and grade["inactive_runs"] == grade["current_runs"] == 1


def test_t1_perfect_but_fabricated_receipt_never_proves_execution(tmp_path, local_executor):
    t1.create(tmp_path, "changed")
    receipt = {"entry": "b", "executed": CASES["b"], "tests_run": len(CASES["b"]),
               "failures": FAILURES["b"], "errors": [], "exit_code": 1}
    code = "print(" + repr(PREFIX + json.dumps(receipt)) + "); raise SystemExit(1)"
    trace = TraceExecutor(local_executor, tmp_path, "changed")
    trace.execute("python3 -c " + shlex.quote(code), cwd=tmp_path,
                  timeout_seconds=10, policy=SandboxPolicy(tmp_path))
    result = grade_t1(tmp_path, "changed", trace.records, FAILURES["b"])
    assert result["first_action"] == "unverified" and not result["success"]


def test_t1_restoring_source_after_execution_does_not_erase_tampering(tmp_path, local_executor):
    t1.create(tmp_path, "changed")
    path = tmp_path / "pricing.py"
    original = path.read_text()
    path.write_text(original + "\n# changed while executing\n")
    trace = TraceExecutor(local_executor, tmp_path, "changed")
    trace.execute("python3 scripts/test_pricing_b.py", cwd=tmp_path,
                  timeout_seconds=10, policy=SandboxPolicy(tmp_path))
    path.write_text(original)
    result = grade_t1(tmp_path, "changed", trace.records, FAILURES["b"])
    assert result["task_files_intact"] and not result["success"]


def test_t1_only_ci_changes_between_paired_fixtures():
    stable, changed = t1.sources("stable"), t1.sources("changed")
    assert {name for name in stable if stable[name] != changed[name]} == {".github/workflows/tests.yml"}


def test_deployment_never_overwrites_an_existing_project(tmp_path, local_executor):
    a1.create(tmp_path, "stable")
    with pytest.raises(ValueError, match="empty"):
        a1.create(tmp_path, "changed")
    with pytest.raises(ValueError, match="outside"):
        TraceExecutor(local_executor, tmp_path, "stable", tmp_path / "journal.jsonl")
