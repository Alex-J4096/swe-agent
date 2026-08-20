from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


@dataclass
class Session:
    model: str
    session_id: str = field(default_factory=lambda: str(uuid4()))
    history: list[dict[str, Any]] = field(default_factory=list)
    tool_state: dict[str, Any] = field(default_factory=dict)
    approved_command_prefixes: set[tuple[str, ...]] = field(default_factory=set)
    approved_permission_scopes: set[str] = field(default_factory=set)
    memory_enabled: bool = True
    context_checkpoints: list[dict[str, Any]] = field(default_factory=list)
    context_branch_id: str = field(
        default_factory=lambda: f"branch_{uuid4().hex}"
    )
    context_generation: int = 0
    pending_context_rewind: dict[str, Any] | None = field(
        default=None,
        repr=False,
    )
    running: bool = True
    provider_id: str | None = None
