"""A1 behavioral oracle, kept outside the agent's task checkout."""
import json
from pathlib import Path
import shlex

from experiments.a1.fixture import backend, sources
from experiments.task_support import project_files
from src.runtime.sandbox import SandboxPolicy

PROBE = '''import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path.cwd()))
from app import build_application

def inspect(order_id):
    app = build_application()
    order = app.service.create_order(order_id)
    created = {"event": "order.created", "order_id": order_id}
    creation_ok = (order == {"order_id": order_id, "status": "created"}
                   and app.events == [created]
                   and len(app.legacy.sent) + len(app.bus.published) == 1)
    creation_legacy = list(app.legacy.sent)
    creation_published = list(app.bus.published)
    app.events.clear()
    app.legacy.sent.clear()
    app.bus.published.clear()
    cancelled = app.service.cancel_order(order_id)
    events = list(app.events)
    legacy = list(app.legacy.sent)
    published = list(app.bus.published)
    expected = {"event": "order.cancelled", "order_id": order_id}
    delivered_once = events == [expected]
    state_ok = cancelled == {"order_id": order_id, "status": "cancelled"}
    app.service.cancel_order(order_id)
    idempotent = (app.events == events and app.legacy.sent == legacy and app.bus.published == published)
    try:
        app.service.cancel_order("UNKNOWN-ORDER")
        unknown_ok = False
    except KeyError:
        unknown_ok = (app.events == events and app.legacy.sent == legacy and app.bus.published == published)
    try:
        app.service.create_order(order_id)
        duplicate_ok = False
    except ValueError:
        duplicate_ok = (app.events == events and app.legacy.sent == legacy and app.bus.published == published)
    return dict(order_id=order_id, creation_ok=creation_ok, state_ok=state_ok,
                delivered_once=delivered_once, idempotent=idempotent,
                unknown_ok=unknown_ok, duplicate_ok=duplicate_ok,
                creation_legacy_events=creation_legacy, creation_published_events=creation_published,
                events=events, legacy_events=legacy, published_events=published)

print(json.dumps({"cases": [inspect("ORDER-17"), inspect("ORDER-42")]}))
'''


def grade(repo: Path, branch: str, executor, *, goal: str = "cancel", stage: str = "test") -> dict:
    if goal not in {"create", "cancel"}:
        raise ValueError(goal)
    state = project_files(repo)
    expected = sources(branch, stage)
    protected = {name: text for name, text in expected.items() if name != "order_service.py"}
    protected_intact = all(state.get(name) == text for name, text in protected.items())
    files_intact = set(state) == set(expected)
    if any(p.is_symlink() for p in repo.rglob("*")):
        return {"success": False, "strict_success": False, "error": "symlink_in_task"}
    result = executor.execute("python3 -I -B -c " + shlex.quote(PROBE), cwd=repo,
                              timeout_seconds=10, policy=SandboxPolicy(repo))
    try:
        cases = json.loads(result.stdout)["cases"] if result.exit_code == 0 else []
    except (ValueError, KeyError, TypeError):
        cases = []
    active = backend(branch)
    checks = ("creation_ok", "duplicate_ok") if goal == "create" else (
        "creation_ok", "state_ok", "delivered_once", "idempotent", "unknown_ok", "duplicate_ok")
    behavior = len(cases) == 2 and all(all(case[k] for k in checks) for case in cases)
    prefix = "creation_" if goal == "create" else ""
    only_active = len(cases) == 2 and all(
        len(case[prefix + "legacy_events"]) == (1 if active == "legacy" else 0)
        and len(case[prefix + "published_events"]) == (1 if active == "bus" else 0)
        for case in cases)
    routes = {(bool(case[prefix + "legacy_events"]), bool(case[prefix + "published_events"])) for case in cases}
    observed = next(iter(routes)) if len(routes) == 1 else None
    action = ("unclassified" if observed is None else "both" if all(observed)
              else "none" if not any(observed)
              else "correct" if observed == (active == "legacy", active == "bus") else "inactive")
    return {"success": behavior, "strict_success": behavior and only_active and protected_intact and files_intact,
            "action": action,
            "only_active_backend_called": only_active, "protected_files_intact": protected_intact,
            "task_file_set_intact": files_intact, "cases": cases,
            "runtime": {"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.exit_code,
                        "backend": result.backend}}
