"""Memory-only tools for the background revision subagent."""
from __future__ import annotations
from difflib import SequenceMatcher
from typing import Any, Literal, TYPE_CHECKING
from collections.abc import Callable

from pydantic import BaseModel, Field

from src.infrastructure.experiment_log import record_event
from src.tools.base import BaseTool, ToolContext

if TYPE_CHECKING:
    from src.infrastructure.memory import MemoryStore


class SearchMemoryArgs(BaseModel):
    query: str = Field(description="Words or an approximate description of a project fact. Empty lists available notes.")
    limit: int = Field(default=5, ge=1, le=20)


class ReadMemoryArgs(BaseModel):
    filename: str = Field(description="Exact memory filename returned by search_memory.")


class UpdateMemoryArgs(BaseModel):
    filename: str
    expected_version: str = Field(description="Version returned by the latest read_memory.")
    description: str = Field(min_length=1, description="Complete updated index summary.")
    body: str = Field(min_length=1, description="Complete updated Markdown body, excluding frontmatter. Preserve unrelated facts.")
    status: Literal["active", "suspect"]
    evidence_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1)


class RevisionContext:
    def __init__(self, store: MemoryStore, evidence: dict[str, dict[str, Any]],
                 should_commit: Callable[[], bool] | None, parent_session_id: str | None):
        self.store = store
        self.evidence = evidence
        self.should_commit = should_commit
        self.parent_session_id = parent_session_id
        self.read_versions: dict[str, str] = {}
        self.updated: set[str] = set()

    def project_notes(self):
        return [note for note in self.store.list_visible_memories()
                if note["type"] == "project" and note["scope"] == "project"
                and not (self.store.memory_dir / note["filename"]).is_symlink()]


class SearchMemoryTool(BaseTool[SearchMemoryArgs]):
    name = "search_memory"
    description = "Search project memories by words or approximate wording. Returns filenames and summaries."
    args_model = SearchMemoryArgs

    def __init__(self, revision: RevisionContext):
        self.revision = revision

    def run(self, args: SearchMemoryArgs, context: ToolContext) -> dict[str, Any]:
        store = self.revision.store
        query = " ".join(args.query.lower().split())
        keywords = store._keywords(query)
        ranked = []
        for note in self.revision.project_notes():
            text = " ".join((note["name"], note["description"], note["body"]))
            overlap = len(keywords & store._keywords(text))
            similarity = SequenceMatcher(None, query, note["description"].lower()).ratio()
            ranked.append((overlap, similarity, note["filename"], note["description"]))
        ranked.sort(key=lambda row: (-row[0], -row[1], row[2]))
        return {"ok": True, "total": len(ranked), "memories": [
            {"filename": filename, "description": description}
            for _, _, filename, description in ranked[:args.limit]
        ]}


class ReadMemoryTool(BaseTool[ReadMemoryArgs]):
    name = "read_memory"
    description = "Read a project's complete summary, body and version before revising it."
    args_model = ReadMemoryArgs

    def __init__(self, revision: RevisionContext):
        self.revision = revision

    def run(self, args: ReadMemoryArgs, context: ToolContext) -> dict[str, Any]:
        note = self.revision.store.read_revision_memory(args.filename)
        if note is None:
            return {"ok": False, "error": "unknown_project_memory"}
        self.revision.read_versions[note["filename"]] = note["version"]
        return {"ok": True, **note}


class UpdateMemoryTool(BaseTool[UpdateMemoryArgs]):
    name = "update_memory"
    description = "Submit a complete project note revision with its read version and supporting evidence."
    args_model = UpdateMemoryArgs

    def __init__(self, revision: RevisionContext):
        self.revision = revision

    def run(self, args: UpdateMemoryArgs, context: ToolContext) -> dict[str, Any]:
        revision = self.revision
        if revision.read_versions.get(args.filename) != args.expected_version:
            output = {"ok": False, "error": "read_required", "guidance": "Call read_memory for this filename and use its version."}
        elif any(key not in revision.evidence for key in args.evidence_ids):
            output = {"ok": False, "error": "invalid_evidence",
                      "available_evidence_ids": sorted(revision.evidence),
                      "guidance": "Cite supplied successful tool evidence IDs. Subagent tool-call IDs are not project evidence."}
        elif not any(revision.evidence[key]["tool"] in {"read_file", "run_bash"} for key in args.evidence_ids):
            output = {"ok": False, "error": "missing_observation",
                      "guidance": "Include successful read_file or run_bash evidence. A write alone does not establish application behavior."}
        else:
            try:
                output = revision.store.update_memory(
                    args.filename, args.expected_version, args.description, args.body,
                    status=args.status, reason=args.reason,
                    evidence=[revision.evidence[key] for key in args.evidence_ids],
                    should_commit=revision.should_commit,
                )
            except OSError as exc:
                output = {"ok": False, "error": "write_error", "error_type": type(exc).__name__}
        if output.get("ok"):
            revision.updated.add(args.filename)
            revision.read_versions.pop(args.filename, None)
        record_event("memory_revision_decision", session_id=revision.parent_session_id,
                     subagent_session_id=context.session.session_id,
                     decision=args.model_dump(), mode="revise", implementation="subagent",
                     outcome=output.get("outcome", output.get("error")))
        return output
