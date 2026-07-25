import re
import yaml
from pathlib import Path


class SkillLoader:
    def __init__(self, skill_path: Path):
        self.skills = {}
        self.skill_path = skill_path
        self._load_all()

    def _load_all(self):
        if not self.skill_path.exists():
            return
        for f in sorted(self.skill_path.rglob("SKILL.md")):
            text = f.read_text()
            meta, body = self._parse_front_matter(text)
            name = meta.get("name", f.parent.name)
            self.skills[name] = {
                "meta": meta,
                "body": body,
                "path": str(f)
            }

    def _parse_front_matter(self, text: str) -> tuple:
        """Parse YAML frontmatter between --- delimiters."""
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text
        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        return meta, match.group(2).strip()

    def get_skill_description(self) -> str:
        """Layer 1: short descriptions for the system prompt."""
        if not self.skills:
            return "(not skills available)"
        lines = []
        for name, skill in self.skills.items():
            desc = skill["meta"].get("description", "No description")
            tags = skill["meta"].get("tags", "")
            line = f"  - {name}: {desc}"
            if tags:
                line += f" [{tags}]"
            lines.append(line)
        return "\n".join(lines)

    def get_content(self, name: str) -> str:
        """Layer 2: full skill body returned in tool_result."""
        skill = self.skills.get(name)
        if not skill:
            return f"Error: Unknow skill '{name}'. Available: {', '.join(self.skills.keys())}"
        return f"<skill name=\"{name}\">\n{skill['body']}</skill>"



