"""Build and score the paired timeout repositories independently of model output."""
import difflib
import json
import tomllib

from src.runtime.sandbox import SandboxPolicy

OLD = "settings.toml"
NEW = "app.toml"
FILES = (OLD, NEW, "service.py")
LEARN = "请把服务的请求超时调到 30 秒，并验证修改生效。"
OBSERVE = "请读取服务启动代码，说明启动时如何加载配置，并确认当前生效的超时值。只需检查。"
TASK = "请把服务的请求超时调到 60 秒，并验证修改生效。"


def service(target):
    return (
        "from pathlib import Path\nimport tomllib\n\n"
        f'config = Path(__file__).with_name("{target}")\n'
        'print(tomllib.loads(config.read_text())["timeout"])\n'
    )


def fixture(repo, target, timeout):
    repo.mkdir(parents=True, exist_ok=True)
    for name in (OLD, NEW):
        (repo / name).write_text(f"timeout = {timeout}\nport = 8080\n")
    (repo / "service.py").write_text(service(target))


def files(repo):
    """Keep exact task-file contents for the tool-boundary diff and final audit."""
    return {
        p.relative_to(repo).as_posix(): (
            f"[symlink] {p.readlink()}" if p.is_symlink()
            else p.read_bytes().decode("utf-8", errors="backslashreplace")
        )
        for p in repo.rglob("*")
        if p.relative_to(repo).parts[0] not in {".swe-agent", "__pycache__"}
        and (p.is_symlink() or p.is_file())
    }


def diff(before, after):
    return {
        name: list(difflib.unified_diff(
            before.get(name, "").splitlines(keepends=True),
            after.get(name, "").splitlines(keepends=True), fromfile=name, tofile=name,
        ))
        for name in sorted(before.keys() | after.keys()) if before.get(name) != after.get(name)
    }


def runtime(repo, sandbox):
    result = sandbox.execute("python3 -I -B service.py", cwd=repo, timeout_seconds=10,
                             policy=SandboxPolicy(repo))
    return {"stdout": result.stdout.strip(), "stderr": result.stderr.strip(),
            "exit_code": result.exit_code, "backend": result.backend}


def grade(repo, sandbox, target, expected):
    run = runtime(repo, sandbox)
    state = files(repo)
    ports_ok = True
    values = {}
    for name in (OLD, NEW):
        try:
            config = tomllib.loads(state[name])
            values[name] = config.pop("timeout")
            ports_ok &= config == {"port": 8080}
        except (ValueError, KeyError):
            ports_ok = False
    return {"runtime": run, "success": run["exit_code"] == 0 and run["stdout"] == str(expected),
            "config_values": values, "unrelated_fields_intact": bool(ports_ok),
            "loader_intact": state.get("service.py") == service(target)}


def save(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
