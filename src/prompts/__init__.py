from pathlib import Path


def load_prompt(name: str) -> str:
    return (Path(__file__).parent / f"{name}.md").read_text(encoding="utf-8")


SYSTEM = load_prompt("system")
