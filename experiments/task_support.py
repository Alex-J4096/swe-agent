"""File-only helpers shared by the small research task environments."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def write_project(destination: Path, sources: dict[str, str]) -> None:
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f"Choose an empty project directory: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    for name, content in sources.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def project_files(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".swe-agent" in relative.parts or "__pycache__" in relative.parts:
            continue
        if path.is_symlink():
            result[relative.as_posix()] = f"[symlink] {path.readlink()}"
        elif path.is_file():
            result[relative.as_posix()] = path.read_text(encoding="utf-8")
    return result


def hashes(sources: dict[str, str]) -> dict[str, str]:
    return {name: hashlib.sha256(content.encode()).hexdigest() for name, content in sorted(sources.items())}


def save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
