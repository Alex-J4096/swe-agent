"""Local execution is restricted to test-authored fixtures in temporary projects."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from src.runtime.sandbox import CommandExecutionResult


class LocalExecutor:
    """Only run the test-authored, stdlib-only fixtures in temporary directories."""
    def execute(self, command, *, cwd, timeout_seconds, policy):
        env = {**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
               "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run(command, shell=True, cwd=cwd, env=env, capture_output=True,
                                text=True, timeout=timeout_seconds)
        return CommandExecutionResult(result.stdout, result.stderr, result.returncode, False, "test-local")


@pytest.fixture
def local_executor():
    return LocalExecutor()
