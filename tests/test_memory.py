import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.infrastructure.memory import Memory, MemoryStore
from src.runtime.memory_worker import MemoryWorker


def fake_client(*contents: str) -> SimpleNamespace:
    responses = iter(
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=content),
                )
            ]
        )
        for content in contents
    )
    completions = SimpleNamespace(
        create=lambda **_: next(responses),
    )
    return SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
    )


class MemoryStoreTests(unittest.TestCase):
    def test_writes_reads_and_indexes_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir))

            path = store.write(
                Memory(
                    name="user-preference-tabs",
                    description="User prefers tabs for indentation",
                    type="user",
                    content="Always use tabs when editing files.",
                )
            )

            self.assertEqual(
                path,
                (
                    Path(temp_dir)
                    / ".swe-agent"
                    / "memory"
                    / "user-preference-tabs.md"
                ).resolve(),
            )
            self.assertIn("name: user-preference-tabs", path.read_text())
            self.assertEqual(
                store.read_memory_file("user-preference-tabs"),
                path.read_text(),
            )
            self.assertIn(
                "- [user-preference-tabs](user-preference-tabs.md)",
                store.read_memory_index(),
            )

            memories = store.list_memory_files()
            self.assertEqual(len(memories), 1)
            self.assertEqual(memories[0]["type"], "user")
            self.assertEqual(
                memories[0]["body"],
                "Always use tabs when editing files.",
            )

    def test_rejects_unknown_type(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir))

            with self.assertRaises(ValueError):
                store.write_memory_file(
                    "bad-memory",
                    "unknown",
                    "Description",
                    "Content",
                )

    def test_does_not_read_outside_memory_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir))

            with self.assertRaises(ValueError):
                store.read_memory_file("../outside.md")

    def test_selects_and_loads_relevant_memories_by_keywords(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir))
            store.write_memory_file(
                "user-preference-tabs",
                "user",
                "User prefers tabs for indentation",
                "Always use tabs when editing files.",
            )
            store.write_memory_file(
                "project-language",
                "project",
                "The project uses Python",
                "Use Python 3.13.",
            )

            messages = [
                {"role": "user", "content": "Please preserve tab indentation."}
            ]
            self.assertEqual(
                store.select_relevant_memories(messages),
                ["user-preference-tabs.md"],
            )
            self.assertIn(
                "Always use tabs when editing files.",
                store.load_memories(messages),
            )

    def test_selects_memories_from_llm_json_response(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(
                Path(temp_dir),
                client=fake_client('["project-language.md"]'),
                model="test-model",
            )
            store.write_memory_file(
                "project-language",
                "project",
                "The project uses Python",
                "Use Python 3.13.",
            )

            selected = store.select_relevant_memories(
                [{"role": "user", "content": "What language is this?"}]
            )

            self.assertEqual(selected, ["project-language.md"])

    def test_model_getter_is_evaluated_for_each_llm_request(self) -> None:
        requested_models: list[str] = []

        class RecordingClient:
            class Chat:
                class Completions:
                    def create(self, **kwargs: object) -> SimpleNamespace:
                        requested_models.append(str(kwargs["model"]))
                        return SimpleNamespace(
                            choices=[
                                SimpleNamespace(
                                    message=SimpleNamespace(
                                        content='["project-language.md"]',
                                    )
                                )
                            ]
                        )

                completions = Completions()

            chat = Chat()

        current_model = ["model-a"]
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(
                Path(temp_dir),
                client=RecordingClient(),
                model_getter=lambda: current_model[0],
            )
            store.write_memory_file(
                "project-language",
                "project",
                "The project uses Python",
                "Use Python 3.13.",
            )

            store.select_relevant_memories(
                [{"role": "user", "content": "What language is this?"}]
            )
            current_model[0] = "model-b"
            store.select_relevant_memories(
                [{"role": "user", "content": "What language is this?"}]
            )

        self.assertEqual(requested_models, ["model-a", "model-b"])

    def test_extracts_and_persists_new_memories(self) -> None:
        response = (
            '[{"name":"user-preference-tabs","type":"user",'
            '"description":"User prefers tabs",'
            '"body":"Always use tabs when editing files."}]'
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(
                Path(temp_dir),
                client=fake_client(response),
                model="test-model",
            )

            created = store.extract_memories(
                [{"role": "user", "content": "Use tabs in files."}]
            )

            self.assertEqual(len(created), 1)
            self.assertEqual(created[0].name, "user-preference-tabs")
            self.assertTrue(created[0].path is not None)
            self.assertIn(
                "user-preference-tabs",
                store.read_memory_index(),
            )

    def test_consolidates_memories_and_removes_replaced_files(self) -> None:
        response = (
            '{"memories":[{"name":"coding-style","type":"user",'
            '"description":"Preferred coding style",'
            '"body":"Use tabs consistently."}],'
            '"remove":["style-tabs.md","style-spaces.md"]}'
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(
                Path(temp_dir),
                client=fake_client(response),
                model="test-model",
            )
            store.CONSOLIDATE_THRESHOLD = 3
            store.write_memory_file(
                "style-tabs",
                "user",
                "Tabs are preferred",
                "Use tabs.",
            )
            store.write_memory_file(
                "style-spaces",
                "user",
                "Spaces are preferred",
                "Use spaces.",
            )
            store.write_memory_file(
                "project-language",
                "project",
                "The project uses Python",
                "Use Python.",
            )

            created = store.consolidate_memories()

            self.assertEqual([memory.name for memory in created], ["coding-style"])
            self.assertIsNotNone(store.read_memory_file("coding-style"))
            self.assertIsNone(store.read_memory_file("style-tabs"))
            self.assertIsNone(store.read_memory_file("style-spaces"))
            self.assertIsNotNone(store.read_memory_file("project-language"))
            index = store.read_memory_index()
            self.assertIn("coding-style", index)
            self.assertNotIn("style-tabs", index)

    def test_memory_worker_processes_a_snapshot_in_order(self) -> None:
        class FakeStore:
            def __init__(self) -> None:
                self.received: list[list[dict[str, str]]] = []
                self.steps: list[str] = []

            def extract_memories(
                self,
                messages: list[dict[str, str]],
            ) -> list[Memory]:
                self.received.append(messages)
                self.steps.append("extract")
                return []

            def consolidate_memories(self) -> list[Memory]:
                self.steps.append("consolidate")
                return []

        store = FakeStore()
        worker = MemoryWorker(store)  # type: ignore[arg-type]
        messages = [{"role": "user", "content": "original"}]

        future = worker.submit(messages)
        messages[0]["content"] = "mutated after submit"
        future.result(timeout=2)
        worker.shutdown(wait=True)

        self.assertEqual(store.received[0][0]["content"], "original")
        self.assertEqual(store.steps, ["extract", "consolidate"])


if __name__ == "__main__":
    unittest.main()
