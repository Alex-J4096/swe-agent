import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import src.infrastructure.compact as compact_module
from src.infrastructure.compact import (
    CompactHistoryResult,
    compact_history,
    snip_compact,
)
from src.runtime.agent_runner import AgentRunner
from src.runtime.session import Session
from src.utils.slash_commands.commands.compact import CompactCommand


def tool_group(call_id: str, content: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "function": {"name": "shell"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": content,
        },
    ]


class SnipCompactTests(unittest.TestCase):
    def test_does_not_split_tool_group_or_exceed_limit(self) -> None:
        messages = [
            {"role": "user", "content": "h0"},
            {"role": "assistant", "content": "h1"},
            {"role": "user", "content": "h2"},
            {"role": "assistant", "content": "middle"},
            *tool_group("call_1", "result"),
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "tail"},
        ]

        result = snip_compact(messages, max_messages=6)

        self.assertLessEqual(len(result), 6)
        for index, message in enumerate(result):
            if message.get("role") != "tool":
                continue
            self.assertGreater(index, 0)
            previous = result[index - 1]
            self.assertTrue(
                previous.get("role") == "assistant"
                and previous.get("tool_calls")
            )


class AgentRunnerCompactTests(unittest.TestCase):
    def setUp(self) -> None:
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="done",
                        tool_calls=None,
                    )
                )
            ]
        )
        self.requests: list[dict] = []
        completions = SimpleNamespace(
            create=lambda **kwargs: (
                self.requests.append(kwargs) or response
            )
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )
        provider = SimpleNamespace(client=client)
        self.runner = AgentRunner(
            provider=provider,
            max_history_messages=6,
        )

    def test_applies_l1_before_and_after_a_round(self) -> None:
        session = Session(
            model="test-model",
            history=[
                {"role": "user", "content": str(index)}
                for index in range(8)
            ],
        )
        toolset = SimpleNamespace(schemas=lambda: [])

        result = self.runner.run(session, toolset, "system")

        self.assertTrue(result.ok)
        self.assertLessEqual(len(session.history), 6)
        self.assertEqual(len(self.requests[0]["messages"]), 7)

    def test_applies_l3_before_l2_at_round_end(self) -> None:
        history: list[dict] = []
        for index in range(4):
            content = "x" * 5_000 if index == 0 else f"result-{index}"
            history.extend(tool_group(f"call_{index}", content))
        session = Session(model="test-model", history=history)

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(
                compact_module,
                "TOOL_RESULT_ARCHIVE_DIR",
                Path(temp_dir),
            ):
                self.runner._compact_after_round(session)
            archives = list(Path(temp_dir).glob("*.json"))

        self.assertEqual(len(archives), 1)
        self.assertEqual(
            session.history[1]["content"],
            "[Earlier tool result compacted. Re-run if needed.]",
        )


class CompactCommandTests(unittest.TestCase):
    def test_stops_after_three_failures_without_persisting_state(self) -> None:
        session = Session(
            model="test-model",
            history=[{"role": "user", "content": "task"}],
        )
        context = SimpleNamespace(
            session=session,
            provider=SimpleNamespace(client=object()),
        )
        command = CompactCommand()
        failure = CompactHistoryResult(ok=False, error="failed")

        with patch(
            "src.utils.slash_commands.commands.compact.compact_history",
            return_value=failure,
        ) as compact_history:
            result = command.execute([], context)

        self.assertEqual(compact_history.call_count, 3)
        self.assertIn("failed 3 consecutive times", result.message or "")
        self.assertIn("Circuit breaker stopped", result.message or "")

    def test_success_stops_retrying(self) -> None:
        session = Session(
            model="test-model",
            history=[{"role": "user", "content": "task"}],
        )
        context = SimpleNamespace(
            session=session,
            provider=SimpleNamespace(client=object()),
        )
        command = CompactCommand()
        failure = CompactHistoryResult(ok=False, error="failed")
        success = CompactHistoryResult(
            ok=True,
            transcript_path=Path(".swe-agent/transcripts/test.jsonl"),
        )

        with patch(
            "src.utils.slash_commands.commands.compact.compact_history",
            side_effect=[failure, success],
        ) as compact_history:
            result = command.execute([], context)

        self.assertEqual(compact_history.call_count, 2)
        self.assertIn("Compacted 1 messages", result.message or "")


class CompactHistoryTests(unittest.TestCase):
    def test_success_replaces_history_and_empty_summary_preserves_it(self) -> None:
        success_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="Current task summary.")
                )
            ]
        )
        empty_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=None)
                )
            ]
        )
        responses = iter([success_response, empty_response])
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **_: next(responses)
                )
            )
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(
                compact_module,
                "TRANSCRIPT_DIR",
                Path(temp_dir),
            ):
                history = [{"role": "user", "content": "task"}]
                success = compact_history(history, client, "test-model")
                preserved = [{"role": "user", "content": "next task"}]
                failure = compact_history(
                    preserved,
                    client,
                    "test-model",
                )
            transcripts = list(Path(temp_dir).glob("*.jsonl"))

        self.assertTrue(success.ok)
        self.assertEqual(len(history), 1)
        self.assertIn("Current task summary.", history[0]["content"])
        self.assertFalse(failure.ok)
        self.assertEqual(
            preserved,
            [{"role": "user", "content": "next task"}],
        )
        self.assertEqual(len(transcripts), 2)


if __name__ == "__main__":
    unittest.main()
