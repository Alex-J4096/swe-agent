from copy import deepcopy
from types import SimpleNamespace

from experiments.c1.run import Transcript, summarize, usage
from src.runtime.agent_runner import AgentRunner
from src.runtime.session import Session


def test_replay_retains_original_tool_results_across_compaction():
    history = Transcript()
    history.append({"role": "user", "content": "inspect"})
    for index in range(4):
        call_id = f"call-{index}"
        history.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": "run_bash", "arguments": "{}"}},
        ]})
        history.append({"role": "tool", "tool_call_id": call_id, "content": f"output {index}"})
    history.append({"role": "assistant", "content": "done"})
    prior = deepcopy(history.original)
    sent = []

    def create(**kwargs):
        sent.append(kwargs["messages"])
        message = SimpleNamespace(content="done", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    provider = SimpleNamespace(client=SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    ))
    runner = AgentRunner(provider, max_history_messages=6, prior_messages=history.original)
    runner._compact_after_round(Session(model="test", history=history))
    assert history != prior
    assert history.original == prior
    session = Session(model="test", history=[{"role": "user", "content": "change"}])
    result = runner.run(session, SimpleNamespace(schemas=lambda: []), "system")
    assert result.ok
    assert sent[0][1:-1] == prior
    assert sent[0][-1] == {"role": "user", "content": "change"}


def test_first_shell_delta_survives_rework_and_costs_keep_missing_usage():
    events = [
        {"event": "run_started", "run_id": "r"},
        {"event": "tool_result", "sequence": 2, "tool": "read_file", "output": {"ok": True}},
        {"event": "workspace_delta", "sequence": 3, "changes": {}},
        {"event": "tool_result", "sequence": 4, "tool": "run_bash", "output": {"ok": False}},
        {"event": "workspace_delta", "sequence": 5, "changes": {"settings.toml": ["patch"]}},
        {"event": "tool_result", "sequence": 6, "tool": "edit_file", "output": {"ok": True}},
        {"event": "workspace_delta", "sequence": 7, "changes": {"app.toml": ["patch"]}},
        {"event": "model_request", "request_id": "m", "component": "memory"},
        {"event": "turn_finished", "ok": True},
    ]
    summary = summarize(events, "app.toml")
    assert summary["first_action"] == "inactive"
    assert summary["delta_sequence"] == 5
    assert summary["reads_before"] == 1
    assert summary["tools"] == 3
    assert summary["agent"] == {"prompt_tokens": 0, "completion_tokens": 0}
    assert summary["memory"] == {"prompt_tokens": None, "completion_tokens": None}
    events[4]["changes"]["app.toml"] = ["patch"]
    assert summarize(events, "app.toml")["first_action"] == "both"
    events.append({"event": "model_response", "component": "memory", "request_id": "m",
                   "response": {"usage": {"prompt_tokens": 17, "completion_tokens": 8}}})
    assert usage(events, "memory") == {"prompt_tokens": 17, "completion_tokens": 8}
