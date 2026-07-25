import re
from pathlib import Path


class SkillLoader:
    def __init__(self, skill_path: Path):
        self.skills = {}
        for f in sorted(skill_path.rglob("SKILLS.md")):
            text = f.read_text(encoding="utf-8")
            pass

    def _parse_forntm_atter(self, text: str) -> tuple:
        """Parse YAML frontmatter between --- delimiters."""
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            pass

    def get_skill_description(self) -> str:
        return self.skills[name].get("description", "")
