"""Model-facing request for a checkpointed context rewind."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from src.tools.base import BaseTool, ToolContext


CarryText = Annotated[str, Field(min_length=1, max_length=2_000)]
CarryPath = Annotated[str, Field(min_length=1, max_length=1_000)]


class CarryForward(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(
        min_length=1,
        max_length=4_000,
        description="The complete user goal that must survive the rewind.",
    )
    completed: list[CarryText] = Field(
        default_factory=list,
        max_length=20,
        description="Work already completed in the external world.",
    )
    files_changed: list[CarryPath] = Field(
        default_factory=list,
        max_length=50,
        description="Files created, edited, deleted, or otherwise changed.",
    )
    external_effects: list[CarryText] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "Commands, network calls, approvals, messages, tasks, or other effects "
            "that the context rewind will not undo."
        ),
    )
    discoveries: list[CarryText] = Field(
        default_factory=list,
        max_length=30,
        description="Important verified findings and evidence.",
    )
    failed_approaches: list[CarryText] = Field(
        default_factory=list,
        max_length=20,
        description="Approaches that should not be repeated and why they failed.",
    )
    next_steps: list[CarryText] = Field(
        default_factory=list,
        max_length=20,
        description="Concrete actions the agent should perform next.",
    )
    long_term_candidates: list[CarryText] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "Durable user preferences, constraints, project facts, or references. "
            "These are only candidates and will be revalidated by the Memory pipeline."
        ),
    )


class RewindContextArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checkpoint_id: str = Field(
        description="An exact checkpoint ID listed in the current system context."
    )
    carry_forward: CarryForward = Field(
        description=(
            "Structured knowledge required to continue without repeating work or "
            "forgetting external side effects."
        )
    )


class RewindContextTool(BaseTool[RewindContextArgs]):
    name = "rewind_context"
    description = (
        "Rewind this agent's working conversation to an earlier checkpoint while "
        "preserving filesystem and other external side effects. Use only when the "
        "intervening exploration is no longer useful. This must be the sole tool "
        "call in the current model step. Include the complete goal, completed work, "
        "changed files, external effects, discoveries, failed approaches, next "
        "steps, and any durable Memory candidates. The request is applied only "
        "after the current tool result is safely recorded."
    )
    args_model = RewindContextArgs

    def run(self, args: RewindContextArgs, context: ToolContext) -> dict:
        checkpoint_ids = {
            record.get("checkpoint_id")
            for record in context.session.context_checkpoints
            if isinstance(record, dict)
        }
        if args.checkpoint_id not in checkpoint_ids:
            return {
                "ok": False,
                "error": f"Unknown context checkpoint: {args.checkpoint_id}",
            }
        if context.session.pending_context_rewind is not None:
            return {
                "ok": False,
                "error": "A context rewind is already pending for this model step.",
            }

        context.session.pending_context_rewind = {
            "checkpoint_id": args.checkpoint_id,
            "carry_forward": args.carry_forward.model_dump(mode="json"),
        }
        return {
            "ok": True,
            "status": "rewind_scheduled",
            "checkpoint_id": args.checkpoint_id,
            "content": (
                "Context rewind scheduled. This result will disappear if the "
                "rewind is applied successfully."
            ),
        }
