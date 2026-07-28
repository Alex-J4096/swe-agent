from dataclasses import dataclass, field
from typing import Any


@dataclass
class Session:
    model: str
    history: list[dict[str, Any]] = field(default_factory=list)
    running: bool = True
