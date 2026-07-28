from pathlib import Path

SYSTEM = (Path(__file__).parent / "system.md").read_text(encoding="utf-8")
COMPACT = (Path(__file__).parent / "compact.md").read_text(encoding="utf-8")