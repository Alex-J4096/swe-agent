"""Optional git worktree lifecycle for task-scoped workspace isolation."""

from __future__ import annotations

import subprocess
from pathlib import Path


class WorktreeError(RuntimeError):
    """Raised when a task worktree cannot be created or validated."""


class WorktreeManager:
    def __init__(self, repo_root: Path, team_root: Path) -> None:
        self.repo_root = repo_root.resolve()
        self.root = (team_root / "worktrees").resolve()

    def path_for(self, task_id: str) -> Path:
        if not task_id or Path(task_id).name != task_id:
            raise ValueError("Invalid task ID for worktree")
        return self.root / task_id

    def prepare(self, task_id: str, mode: str) -> Path:
        if mode == "repo":
            return self.repo_root
        if mode != "worktree":
            raise WorktreeError(f"Unsupported workspace mode: {mode}")

        path = self.path_for(task_id)
        if path.exists():
            self._validate_existing(path)
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.repo_root),
                    "worktree",
                    "add",
                    "--detach",
                    str(path),
                    "HEAD",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            raise WorktreeError(f"Failed to create worktree: {detail.strip()}") from exc
        self._validate_existing(path)
        return path

    def remove(self, task_id: str, *, force: bool = False) -> None:
        """Remove a task worktree when its changes have been integrated."""
        path = self.path_for(task_id)
        if not path.exists():
            return
        command = ["git", "-C", str(self.repo_root), "worktree", "remove"]
        if force:
            command.append("--force")
        command.append(str(path))
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            raise WorktreeError(f"Failed to remove worktree: {detail.strip()}") from exc

    def _validate_existing(self, path: Path) -> None:
        try:
            result = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise WorktreeError(f"Invalid task worktree: {path}") from exc
        resolved = Path(result.stdout.strip()).resolve()
        if resolved != path.resolve():
            raise WorktreeError(f"Worktree points to an unexpected directory: {path}")
