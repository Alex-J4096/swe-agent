"""External command journal and case-level grading for T1.

The journal is owned by the tool executor, not by the task repository. Only an
unchanged official script invoked directly by Python yields a verified receipt.
"""
import json
from pathlib import Path
import re
import shlex

from experiments.task_support import project_files
from experiments.t1.fixture import entry, sources

CASES = {
    "a": ["tests.test_pricing_classic.ClassicPricingTests." + name for name in (
        "test_coupon_cannot_make_total_negative", "test_coupon_is_subtracted", "test_plain_subtotal")],
    "b": ["tests.test_pricing_rules.PricingRuleTests." + name for name in (
        "test_empty_order", "test_percentage_discount", "test_plain_subtotal", "test_shipping_is_added")],
}
FAILURES = {"a": [CASES["a"][0]], "b": [CASES["b"][-1]]}
PREFIX = "PRICING_TEST_RUN "


def command_entry(command: str, cwd: Path, repo: Path) -> str | None:
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if not parts or not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(parts[0]).name):
        return None
    args = parts[1:]
    while args and args[0] in {"-B", "-I", "-u", "-E", "-s"}:
        args = args[1:]
    if len(args) != 1:
        return None
    path = (cwd / args[0]).resolve()
    return next((label for label in ("a", "b")
                 if path == (repo / f"scripts/test_pricing_{label}.py").resolve()), None)


class TraceExecutor:
    def __init__(self, executor, repo: Path, branch: str, journal: Path | None = None):
        self.executor = executor
        self.repo = repo.resolve()
        self.baseline = sources(branch)
        self.journal = journal.resolve() if journal else None
        if self.journal and self.journal.is_relative_to(self.repo):
            raise ValueError("The command journal must be outside the task checkout")
        self.records = []

    def __getattr__(self, name):
        return getattr(self.executor, name)

    def execute(self, command, *, cwd, timeout_seconds, policy):
        intact_before = project_files(self.repo) == self.baseline
        result = self.executor.execute(command, cwd=cwd, timeout_seconds=timeout_seconds, policy=policy)
        receipts = []
        for line in result.stdout.splitlines():
            if line.startswith(PREFIX):
                try:
                    receipts.append(json.loads(line[len(PREFIX):]))
                except ValueError:
                    pass
        label = command_entry(command, cwd, self.repo)
        intact_after = project_files(self.repo) == self.baseline
        record = {"sequence": len(self.records) + 1, "command": command, "entry": label,
                  "exit_code": result.exit_code, "receipts": receipts,
                  "source_intact": intact_before and intact_after,
                  "verified_invocation": bool(label and intact_before and intact_after),
                  "backend": result.backend}
        self.records.append(record)
        if self.journal:
            self.journal.parent.mkdir(parents=True, exist_ok=True)
            with self.journal.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        return result


def valid_receipt(record: dict) -> bool:
    label = record.get("entry")
    receipts = record.get("receipts", [])
    if label not in CASES or not record.get("verified_invocation") or len(receipts) != 1:
        return False
    receipt = receipts[0]
    return (isinstance(receipt, dict) and receipt.get("entry") == label
            and receipt.get("executed") == CASES[label]
            and receipt.get("tests_run") == len(CASES[label])
            and receipt.get("failures") == FAILURES[label] and receipt.get("errors") == []
            and receipt.get("exit_code") == record.get("exit_code") == 1)


def grade(repo: Path, branch: str, records: list[dict], reported_failures: list[str]) -> dict:
    active = entry(branch)
    attempts = [r for r in records if r.get("entry") or r.get("receipts")]
    first = attempts[0] if attempts else None
    first_action = ("none" if first is None else "unverified" if not valid_receipt(first)
                    else "correct" if first["entry"] == active else "inactive")
    valid = [r for r in attempts if valid_receipt(r)]
    current_runs = [r for r in valid if r["entry"] == active]
    files_intact = project_files(repo) == sources(branch)
    report_correct = sorted(reported_failures) == sorted(FAILURES[active])
    success = bool(current_runs) and report_correct
    return {"first_action": first_action, "success": success,
            "strict_success": success and files_intact and all(valid_receipt(r) for r in attempts),
            "current_entry": active, "expected_case_ids": CASES[active],
            "expected_failure_ids": FAILURES[active], "reported_failure_ids": reported_failures,
            "report_correct": report_correct, "task_files_intact": files_intact,
            "current_runs": len(current_runs), "inactive_runs": sum(r["entry"] != active for r in valid),
            "unverified_attempts": sum(not valid_receipt(r) for r in attempts),
            "records": records}
