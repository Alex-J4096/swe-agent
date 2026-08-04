import json
import re
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Any

import yaml
from openai import OpenAIError

from src.infrastructure.retry import request_with_retry

MEMORY_TYPES = ("user", "feedback", "project", "reference")
MEMORY_INDEX_NAME = "MEMORY.md"


@dataclass(frozen=True)
class Memory:
    name: str
    description: str
    type: str
    content: str
    path: Path | None = None


class MemoryStore:
    """Persist project memories under ``<workdir>/.swe-agent/memory``."""

    def __init__(
        self,
        workdir: Path,
        client: Any | None = None,
        model: str | None = None,
        model_getter: Callable[[], str | None] | None = None,
    ) -> None:
        if model is not None and model_getter is not None:
            raise ValueError("Pass either model or model_getter, not both.")

        self.workdir = workdir.expanduser().resolve()
        self.memory_dir = self.workdir / ".swe-agent" / "memory"
        self.memory_index = self.memory_dir / MEMORY_INDEX_NAME
        self.client = client
        self.model = model
        self.model_getter = model_getter

    def _current_model(self) -> str | None:
        """Return the model to use for the next memory LLM request."""
        if self.model_getter is not None:
            return self.model_getter()
        return self.model

    def write(self, memory: Memory) -> Path:
        """Write a memory file and rebuild the memory index."""
        if memory.type not in MEMORY_TYPES:
            allowed_types = ", ".join(MEMORY_TYPES)
            raise ValueError(
                f"Unknown memory type '{memory.type}'. "
                f"Expected one of: {allowed_types}."
            )

        file_path = self.memory_dir / f"{self._slugify(memory.name)}.md"
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        metadata = {
            "name": memory.name,
            "description": memory.description,
            "type": memory.type,
        }
        frontmatter = yaml.safe_dump(
            metadata,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ).strip()
        body = memory.content.rstrip()
        self._atomic_write(
            file_path,
            f"---\n{frontmatter}\n---\n\n{body}\n",
        )
        self._rebuild_index()
        return file_path

    def write_memory_file(
        self,
        name: str,
        mem_type: str,
        description: str,
        body: str,
    ) -> Path:
        """Write a memory using the individual metadata fields."""
        return self.write(
            Memory(
                name=name,
                description=description,
                type=mem_type,
                content=body,
            )
        )

    def read_memory_index(self) -> str:
        """Read the Markdown index used for memory discovery."""
        if not self.memory_index.exists():
            return ""
        return self.memory_index.read_text(encoding="utf-8").strip()

    def read_memory_file(self, name: str) -> str | None:
        """Read a single memory file's complete Markdown content."""
        path = self._resolve_memory_path(name)
        if not path.exists() or not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    def list_memory_files(self) -> list[dict[str, str]]:
        """List all memory files with their metadata and body."""
        if not self.memory_dir.exists():
            return []

        result: list[dict[str, str]] = []
        for path in sorted(self.memory_dir.glob("*.md")):
            if path.name.lower() == MEMORY_INDEX_NAME.lower():
                continue

            raw = path.read_text(encoding="utf-8")
            metadata, body = self._parse_frontmatter(raw)
            result.append(
                {
                    "filename": path.name,
                    "name": str(metadata.get("name", path.stem)),
                    "description": str(metadata.get("description", "")),
                    "type": str(metadata.get("type", "user")),
                    "body": body,
                }
            )
        return result

    def select_relevant_memories(
        self,
        messages: list[dict[str, Any]],
        max_items: int = 5,
        use_llm: bool = True,
    ) -> list[str]:
        """Select relevant memory filenames for the current dialogue.

        An LLM is used when a client and model were injected. If that call is
        unavailable or returns invalid filenames, keyword matching is used.
        """
        if max_items <= 0:
            return []

        memories = self.list_memory_files()
        if not memories:
            return []

        dialogue = self._format_messages(messages, max_chars=4_000)
        candidates = "\n".join(
            f"- {memory['filename']}: {memory['name']} — "
            f"{memory['description']}"
            for memory in memories
        )
        prompt = (
            "Select the memories relevant to this dialogue. Return only a "
            "JSON array of exact filenames, with at most "
            f"{max_items} items. Do not return explanations.\n\n"
            f"Available memories:\n{candidates}\n\n"
            f"Dialogue:\n{dialogue}"
        )

        if use_llm:
            response = self._call_llm(prompt)
            if response is not None:
                selected = self._parse_json_array(response)
                valid_filenames = {
                    memory["filename"] for memory in memories
                }
                result = []
                for filename in selected:
                    if filename in valid_filenames and filename not in result:
                        result.append(filename)
                if result:
                    return result[:max_items]

        return self._select_by_keywords(messages, memories, max_items)

    def load_memories(
        self,
        messages: list[dict[str, Any]],
        use_llm: bool = True,
    ) -> str:
        """Load relevant memory content for injection into context."""
        memories_by_filename = {
            memory["filename"]: memory
            for memory in self.list_memory_files()
        }
        sections = []
        for filename in self.select_relevant_memories(
            messages,
            use_llm=use_llm,
        ):
            memory = memories_by_filename.get(filename)
            if memory is None:
                continue

            sections.append(
                f"<memory name=\"{memory['name']}\" "
                f"type=\"{memory['type']}\">\n"
                f"{memory['body']}\n"
                "</memory>"
            )
        return "\n\n".join(sections)

    def extract_memories(
        self,
        messages: list[dict[str, Any]],
    ) -> list[Memory]:
        """Extract new memories from recent dialogue. Runs after each turn."""
        if self.client is None or not self._current_model():
            return []

        memories = self.list_memory_files()
        existing_desc = "\n".join(
            f"- {memory['filename']}: {memory['name']} — "
            f"{memory['description']}"
            for memory in memories
        ) or "(none)"
        dialogue = self._format_messages(messages, max_chars=4_000)
        prompt = (
            "Extract user preferences, constraints, or project facts from this dialogue.\n"
            "Return a JSON array. Each item: {name, type, description, body}.\n"
            "- name: short kebab-case identifier (e.g. 'user-preference-tabs')\n"
            "- type: one of 'user' (user preference), 'feedback' (guidance), "
            "'project' (project fact), 'reference' (external pointer)\n"
            "- description: one-line summary for index lookup\n"
            "- body: full detail in markdown\n"
            "If nothing new or already covered by existing memories, return [].\n\n"
            f"Existing memories:\n{existing_desc}\n\n"
            f"Dialogue:\n{dialogue}"
        )

        response = self._call_llm(prompt)
        if response is None:
            return []

        extracted = self._parse_json_array(response)
        existing_names = {
            memory["name"].lower()
            for memory in memories
        }
        created: list[Memory] = []
        for item in extracted:
            if not isinstance(item, dict):
                continue

            name = self._string_value(item.get("name"))
            memory_type = self._string_value(item.get("type"), "user")
            description = self._string_value(item.get("description"))
            body = self._string_value(item.get("body"))
            if not name or not description or not body:
                continue
            if name.lower() in existing_names:
                continue
            if memory_type not in MEMORY_TYPES:
                continue

            memory = Memory(
                name=self._slugify(name),
                description=description,
                type=memory_type,
                content=body,
            )
            path = self.write(memory)
            created.append(
                Memory(
                    name=memory.name,
                    description=memory.description,
                    type=memory.type,
                    content=memory.content,
                    path=path,
                )
            )
            existing_names.add(name.lower())

        return created

    CONSOLIDATE_THRESHOLD = 10

    def consolidate_memories(self) -> list[Memory]:
        """Merge duplicate or stale memories when the threshold is reached.

        The model must return a JSON object with ``memories`` containing the
        replacement records and ``remove`` containing exact source filenames.
        Only files in the current memory directory can be removed.
        """
        memories = self.list_memory_files()
        if len(memories) < self.CONSOLIDATE_THRESHOLD:
            return []
        if self.client is None or not self._current_model():
            return []

        documents = "\n\n".join(
            f"Filename: {memory['filename']}\n"
            f"Name: {memory['name']}\n"
            f"Description: {memory['description']}\n"
            f"Type: {memory['type']}\n"
            f"Body:\n{memory['body']}"
            for memory in memories
        )
        prompt = (
            "Review these agent memory files for duplicates, contradictions, "
            "or stale entries. Merge only when useful.\n"
            "Return one JSON object with this exact shape:\n"
            '{"memories": [{"name": "...", "type": "...", '
            '"description": "...", "body": "..."}], '
            '"remove": ["filename.md"]}\n'
            "- memories: replacement or updated memories only; omit files "
            "that do not need changes.\n"
            "- remove: exact filenames of source memories replaced by the "
            "returned memories. Do not include memory.md.\n"
            "- Keep important details and do not invent facts.\n\n"
            f"Memory files:\n{documents[:12_000]}"
        )

        response = self._call_llm(prompt, max_tokens=4_096)
        if response is None:
            return []

        payload = self._parse_json_value(response)
        if isinstance(payload, list):
            replacement_items = payload
            remove_items: list[Any] = []
        elif isinstance(payload, dict):
            replacement_items = payload.get("memories", [])
            remove_items = payload.get("remove", payload.get("delete", []))
        else:
            return []

        if not isinstance(replacement_items, list):
            return []
        if not isinstance(remove_items, list):
            remove_items = []

        created: list[Memory] = []
        target_filenames: set[str] = set()
        for item in replacement_items:
            if not isinstance(item, dict):
                continue

            name = self._string_value(item.get("name"))
            memory_type = self._string_value(item.get("type"), "user")
            description = self._string_value(item.get("description"))
            body = self._string_value(item.get("body"))
            if not name or not description or not body:
                continue
            if memory_type not in MEMORY_TYPES:
                continue

            slug = self._slugify(name)
            filename = f"{slug}.md"
            if filename in target_filenames:
                continue

            memory = Memory(
                name=slug,
                description=description,
                type=memory_type,
                content=body,
            )
            path = self.write(memory)
            created.append(
                Memory(
                    name=memory.name,
                    description=memory.description,
                    type=memory.type,
                    content=memory.content,
                    path=path,
                )
            )
            target_filenames.add(path.name)

        existing_filenames = {
            memory["filename"]
            for memory in memories
        }
        files_to_remove = set()
        for item in remove_items:
            if not isinstance(item, str):
                continue
            filename = item.strip()
            if filename in existing_filenames:
                files_to_remove.add(filename)

        files_to_remove.difference_update(target_filenames)
        for filename in files_to_remove:
            path = self._resolve_memory_path(filename)
            if path.exists() and path.is_file():
                path.unlink()

        if files_to_remove:
            self._rebuild_index()
        return created

    def _call_llm(
        self,
        prompt: str,
        max_tokens: int = 1_024,
    ) -> str | None:
        model = self._current_model()
        if self.client is None or not model:
            return None

        try:
            response = request_with_retry(
                lambda: self.client.chat.completions.create(
                    model=model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are a memory management component. "
                                "Follow the requested JSON output format exactly."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    max_tokens=max_tokens,
                )
            )
        except (OpenAIError, AttributeError, IndexError, TypeError):
            return None

        if not response.choices:
            return None
        content = response.choices[0].message.content
        return content if isinstance(content, str) and content.strip() else None

    def _select_by_keywords(
        self,
        messages: list[dict[str, Any]],
        memories: list[dict[str, str]],
        max_items: int,
    ) -> list[str]:
        query_keywords = self._keywords(
            self._format_messages(messages, max_chars=4_000)
        )
        if not query_keywords:
            return []

        ranked: list[tuple[int, int, str]] = []
        for index, memory in enumerate(memories):
            name_keywords = self._keywords(memory["name"])
            description_keywords = self._keywords(memory["description"])
            score = (
                3 * len(query_keywords & name_keywords)
                + 2 * len(query_keywords & description_keywords)
            )
            if score:
                ranked.append((-score, index, memory["filename"]))

        ranked.sort()
        return [filename for _, _, filename in ranked[:max_items]]

    @staticmethod
    def _parse_json_value(text: str) -> Any:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            candidates = (
                (cleaned.find("{"), cleaned.rfind("}")),
                (cleaned.find("["), cleaned.rfind("]")),
            )
            for start, end in candidates:
                if start < 0 or end <= start:
                    continue
                try:
                    return json.loads(cleaned[start:end + 1])
                except json.JSONDecodeError:
                    continue
            return None

    @staticmethod
    def _parse_json_array(text: str) -> list[Any]:
        value = MemoryStore._parse_json_value(text)

        if isinstance(value, dict) and isinstance(value.get("memories"), list):
            return value["memories"]
        return value if isinstance(value, list) else []

    @staticmethod
    def _format_messages(
        messages: list[dict[str, Any]],
        max_chars: int,
    ) -> str:
        chunks = []
        for message in messages:
            role = str(message.get("role", "unknown"))
            content = message.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, default=str)
            chunks.append(f"{role}: {content}")

        dialogue = "\n".join(chunks)
        return dialogue[-max_chars:]

    @staticmethod
    def _keywords(text: str) -> set[str]:
        stopwords = {
            "a", "an", "and", "are", "for", "from", "has", "have",
            "into", "is", "it", "of", "on", "or", "that", "the",
            "this", "to", "use", "with", "you",
        }
        return {
            token
            for token in re.findall(r"[\w-]+", text.lower())
            if len(token) > 1 and token not in stopwords
        }

    @staticmethod
    def _string_value(value: Any, default: str = "") -> str:
        if value is None:
            return default
        return str(value).strip()

    def _rebuild_index(self) -> None:
        lines = []
        for memory in self.list_memory_files():
            description = " ".join(memory["description"].split())
            lines.append(
                f"- [{memory['name']}]({memory['filename']}) — {description}"
            )

        self._atomic_write(
            self.memory_index,
            "\n".join(lines) + "\n" if lines else "",
        )

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temporary_path = path.with_name(f".{path.name}.tmp")
        temporary_path.write_text(content, encoding="utf-8")
        temporary_path.replace(path)

    def _resolve_memory_path(self, name: str) -> Path:
        file_name = name if name.endswith(".md") else f"{self._slugify(name)}.md"
        if Path(file_name).name != file_name:
            raise ValueError("Memory name must refer to a file in the memory directory.")
        return self.memory_dir / file_name

    @staticmethod
    def _slugify(name: str) -> str:
        slug = re.sub(r"[^\w-]+", "-", name.strip().lower())
        slug = slug.strip("-")
        if not slug:
            raise ValueError("Memory name cannot be empty.")
        return slug

    @staticmethod
    def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            return {}, text.strip()

        try:
            closing_index = next(
                index
                for index, line in enumerate(lines[1:], start=1)
                if line.strip() == "---"
            )
        except StopIteration:
            return {}, text.strip()

        raw_metadata = "\n".join(lines[1:closing_index])
        metadata = yaml.safe_load(raw_metadata) or {}
        if not isinstance(metadata, dict):
            return {}, text.strip()

        body = "\n".join(lines[closing_index + 1:]).strip()
        return metadata, body
