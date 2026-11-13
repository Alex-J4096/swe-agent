"""C1 shell view: system tools, the selected Python runtime and one task checkout."""
import os
from pathlib import Path
import sys

from src.runtime.sandbox import SandboxedCommandExecutor


class ExperimentSandbox(SandboxedCommandExecutor):
    runtime = Path(sys.base_prefix).resolve()

    def __init__(self, hidden_paths):
        super().__init__()
        self.hidden_paths = tuple(Path(p).resolve() for p in hidden_paths)

    def _environment(self, private_home):
        env = super()._environment(private_home)
        env["PATH"] = os.pathsep.join([str(self.runtime / "bin"), "/usr/bin", "/bin"])
        return env

    def _macos_profile(self, policy, private_home):
        profile = super()._macos_profile(policy, private_home)
        return "\n".join([
            profile, "(deny network*)",
            *(f"(deny file-read* (subpath {self._seatbelt_string(p)}))" for p in self.hidden_paths),
        ])

    def _bubblewrap_command(self, command, *, cwd, policy, private_home):
        args = ["bwrap", "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-net"]
        for entry in ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", self.runtime]:
            path = Path(entry)
            if path.is_symlink():
                args.extend(["--symlink", os.readlink(path), str(path)])
            elif path.exists() and not (entry == self.runtime and path.is_relative_to("/usr")):
                args.extend(["--ro-bind", str(path), str(path)])
        return [
            *args, "--dev", "/dev", "--proc", "/proc",
            "--bind", str(private_home), str(private_home),
            "--bind", str(policy.workspace), str(policy.workspace),
            "--chdir", str(cwd), "/bin/sh", "-c", command,
        ]

    def _command(self, command, *, cwd, policy, private_home):
        argv = super()._command(command, cwd=cwd, policy=policy, private_home=private_home)
        if self.backend_name == "sandbox-exec":
            argv[-2] = "-c"
        return argv
