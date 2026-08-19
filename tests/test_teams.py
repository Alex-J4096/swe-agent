import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

from src.runtime.session import Session
from src.teams.controller import TeamController
from src.teams.protocol import EventKind, PlanTask, TeamEvent, TeamPlan
from src.teams.task_board import TaskBoard, TeamTask
from src.teams.team_runtime import TeamRuntime
from src.teams.worktree import WorktreeManager
from src.tools.base import ToolContext
from src.tools.team.propose_plan import ProposeTeamPlanArgs, ProposeTeamPlanTool
from src.tools.task.store import atomic_write_json
from src.tools.toolset import ToolSet
from src.utils.slash_commands.base import CommandContext
from src.utils.slash_commands.commands.team import TeamCommand


def make_runtime(tmp_path: Path) -> TeamRuntime:
    return TeamRuntime(tmp_path, "team-test")


def test_message_bus_delivery_is_acknowledgeable_and_idempotent(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    event = runtime.send(
        recipient_id="lead",
        sender_id="worker-a",
        kind=EventKind.task_result,
        payload={"task_id": "t1", "ok": True},
    )
    session = Session(model="test")

    assert runtime.deliver_pending(session, "lead") == 1
    assert runtime.deliver_pending(session, "lead") == 0
    assert event.event_id in session.tool_state["team_event_ids"]
    assert "task_result" in session.history[-1]["content"]


def test_task_board_claims_one_task_once_under_concurrency(tmp_path: Path) -> None:
    board = TaskBoard(tmp_path / ".swe-agent" / "teams" / "team-test")
    task = TeamTask(subject="single claim")
    board.create_tasks([task])
    claims = []
    barrier = threading.Barrier(8)

    def claim(index: int) -> None:
        barrier.wait()
        claimed = board.claim_ready(f"worker-{index}")
        if claimed is not None:
            claims.append(claimed)

    threads = [threading.Thread(target=claim, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(claims) == 1
    assert board.get(task.task_id).owner == claims[0].owner


def test_plan_approval_creates_dependency_ordered_tasks(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    plan = TeamPlan(
        goal="build teams",
        tasks=[
            PlanTask(key="protocol", subject="Implement protocol"),
            PlanTask(
                key="worker",
                subject="Implement worker",
                depends_on=["protocol"],
            ),
        ],
    )
    runtime.propose_plan(plan)
    created = runtime.approve_plan(plan.plan_id)

    assert [task.status for task in created] == ["pending", "pending"]
    assert created[1].depends_on == [created[0].task_id]
    assert [task.task_id for task in runtime.board.ready_tasks()] == [
        created[0].task_id
    ]


def test_lead_plan_tool_stays_in_approval_gate(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    result = ProposeTeamPlanTool(runtime).run(
        ProposeTeamPlanArgs(
            goal="test proposal",
            tasks=[PlanTask(key="one", subject="One")],
        ),
        ToolContext(session=Session(model="test")),
    )

    assert result["ok"] is True
    assert result["status"] == "awaiting_approval"
    assert runtime.state().plan.status.value == "awaiting_approval"


def test_plan_gate_blocks_mutations_until_approval(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    context = ToolContext(
        session=Session(model="test"),
        team_runtime=runtime,
        agent_id="worker-a",
        task_id="task-1",
    )

    assert "approved" in runtime.guard("write_file", {}, context)
    assert runtime.guard("read_file", {}, context) is None


def test_approved_plan_requires_claimed_task_for_teammate_mutation(
    tmp_path: Path,
) -> None:
    runtime = make_runtime(tmp_path)
    plan = TeamPlan(
        goal="modify workspace",
        tasks=[PlanTask(key="change", subject="Make a change")],
    )
    runtime.propose_plan(plan)
    [task] = runtime.approve_plan(plan.plan_id)
    context = ToolContext(
        session=Session(model="test"),
        team_runtime=runtime,
        agent_id="worker-a",
        task_id=task.task_id,
    )
    assert "own" in runtime.guard("write_file", {}, context)

    claimed = runtime.board.claim_ready("worker-a", task.task_id)
    assert claimed is not None
    assert runtime.guard("write_file", {}, context) is None


def test_team_event_payload_is_valid_json_on_disk(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    event = TeamEvent(
        team_id=runtime.team_id,
        sender_id="lead",
        recipient_id="worker-a",
        kind=EventKind.message,
        payload={"text": "hello"},
    )
    runtime.bus.send(event)
    files = list((runtime.root / "inbox" / "worker-a").glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text()) ["event_id"] == event.event_id


def test_teammate_worker_does_not_drop_normal_messages_while_idle(tmp_path: Path) -> None:
    from src.teams.teammate_worker import TeammateWorker

    runtime = make_runtime(tmp_path)
    runtime.send(
        recipient_id="worker-a",
        kind=EventKind.message,
        payload={"text": "context for the next task"},
    )
    worker = TeammateWorker(
        runtime=runtime,
        agent_id="worker-a",
        model="test",
        runner=SimpleNamespace(workdir=tmp_path),
        toolset=SimpleNamespace(),
        system_prompt="system",
    )
    worker._consume_control_events()
    assert len(worker._deferred_deliveries) == 1
    assert runtime.bus.pending_count("worker-a") == 0


def test_message_bus_recovers_unacknowledged_processing_message(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    runtime.send(
        recipient_id="worker-a",
        kind=EventKind.message,
        payload={"text": "recover me"},
    )
    [delivery] = runtime.bus.poll("worker-a")
    assert runtime.bus.pending_count("worker-a") == 0

    restarted = make_runtime(tmp_path)
    assert restarted.bus.pending_count("worker-a") == 1
    [recovered] = restarted.bus.poll("worker-a")
    assert recovered.event.payload["text"] == "recover me"


def test_expired_task_claim_is_requeued(tmp_path: Path) -> None:
    board = TaskBoard(tmp_path / ".swe-agent" / "teams" / "team-test")
    task = TeamTask(subject="retriable")
    board.create_tasks([task])
    claimed = board.claim_ready("worker-a", lease_seconds=10)
    assert claimed is not None
    assert claimed.lease_expires_at is not None

    reclaimed = board.reclaim_expired(now=claimed.lease_expires_at + 1)
    assert [item.task_id for item in reclaimed] == [task.task_id]
    retry = board.claim_ready("worker-b", lease_seconds=10)
    assert retry is not None
    assert retry.owner == "worker-b"


def test_worktree_manager_creates_and_reuses_task_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    manager = WorktreeManager(repo, tmp_path / ".swe-agent" / "teams" / "team-test")

    created = manager.prepare("task-1", "worktree")
    reused = manager.prepare("task-1", "worktree")

    assert created == reused
    assert (created / "README.md").read_text(encoding="utf-8") == "initial\n"
    assert created != repo


def test_runtime_binds_worktree_to_claimed_task(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    runtime = TeamRuntime(repo, "worktree-team")
    plan = TeamPlan(
        goal="isolate a change",
        tasks=[PlanTask(key="change", subject="Change", workspace_mode="worktree")],
    )
    runtime.propose_plan(plan)
    [created] = runtime.approve_plan(plan.plan_id)

    claimed = runtime.claim_task("worker-a")
    assert claimed is not None
    bound = runtime.prepare_task_workspace(claimed)

    assert bound.workspace_root is not None
    assert Path(bound.workspace_root).exists()
    assert Path(bound.workspace_root) != repo.resolve()
    assert runtime.board.get(created.task_id).workspace_root == bound.workspace_root


def test_team_command_starts_and_stops_runtime(tmp_path: Path) -> None:
    toolset = ToolSet()
    controller = TeamController(
        provider=SimpleNamespace(),
        workdir=tmp_path,
        toolset=toolset,
        runner_factory=lambda: SimpleNamespace(workdir=tmp_path),
        model_getter=lambda: "test",
        worker_count=1,
    )
    context = CommandContext(
        session=Session(model="test"),
        toolset=toolset,
        provider=SimpleNamespace(),
        workdir=tmp_path,
        team_controller=controller,
    )

    started = TeamCommand().execute(["start", "cli-team", "1"], context)
    assert started.message is not None
    assert context.team_runtime is controller.runtime
    assert "propose_team_plan" in toolset.tools

    stopped = TeamCommand().execute(["shutdown"], context)
    assert stopped.message == "Team stopped."
    assert context.team_runtime is None
    assert "propose_team_plan" not in toolset.tools
