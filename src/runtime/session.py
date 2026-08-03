from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


@dataclass
class Session:
    model: str
    session_id: str = field(default_factory=lambda: str(uuid4()))
    history: list[dict[str, Any]] = field(default_factory=list)
    tool_state: dict[str, Any] = field(default_factory=dict)
    running: bool = True
