from dataclasses import dataclass, field
from typing import Any


@dataclass
class Session:
    model: str
    history: list[dict[str, Any]] = field(default_factory=list)
    tool_state: dict[str, Any] = field(default_factory=dict)
    running: bool = True
