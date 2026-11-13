"""Aggregate the fixed revision batch and export blinded maintenance snapshots."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

from .fixture import save


def load(path):
    return json.loads(path.read_text())


def events(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("lab", type=Path)
    args = parser.parse_args()
    lab = args.lab.resolve()
    jobs = load(lab / "manifest.json")["order"]
    blind, mapping, results = [], {}, []
    failures = []
    for job in jobs:
        directory = lab / job
        alias = hashlib.sha256(("audit-1133-" + job).encode()).hexdigest()[:10]
        mapping[alias] = job
        if (directory / "visible-before-task.json").exists():
            direction, scene, _, _ = job.split("-")
            old = "settings.toml" if direction == "settings" else "app.toml"
            target = old if scene == "stable" else ("app.toml" if old == "settings.toml" else "settings.toml")
            visible = load(directory / "visible-before-task.json")
            blind.append(dict(id=alias, scene=scene, actual_target=target,
                              notes=[{k: n[k] for k in ("description", "body")} for n in visible["notes"]]))
        if (directory / "failure.json").exists():
            failures.append(load(directory / "failure.json"))
        if (directory / "result.json").exists():
            row = load(directory / "result.json")
            ev = events(directory / "maintenance.jsonl") + events(directory / "follow.jsonl")
            row["model_errors"] = sum(e["event"] == "model_error" for e in ev)
            row["models"] = sorted({e["response"]["model"] for e in ev if e["event"] == "model_response"})
            row["decisions"] = [e for e in events(directory / "maintenance.jsonl") if e["event"] == "memory_revision_decision"]
            results.append(row)
    save(lab / "blind-maintenance.json", sorted(blind, key=lambda r: r["id"]))
    save(lab / "audit-mapping.json", mapping)
    save(lab / "all-results.json", results)
    save(lab / "failures.json", failures)
    summaries = []
    for scene in ("stable", "conflict", "located"):
        for mode in ("default", "evidence", "revise"):
            group = [r for r in results if r["scene"] == scene and r["mode"] == mode]
            def total_tokens(key):
                values = [r[key][field] for r in group for field in ("prompt_tokens", "completion_tokens")]
                return sum(values) if all(isinstance(v, int) for v in values) else None
            summaries.append(dict(scene=scene, mode=mode, completed=len(group),
                                  first_correct=sum(r["first_action"] == "correct" for r in group),
                                  strict_success=sum(r["strict_success"] for r in group),
                                  runtime_success=sum(r["success"] for r in group),
                                  idle_written=sum(r["idle_written"] for r in group),
                                  rework=sum(r["rework"] for r in group),
                                  reads=sum(r["reads_before"] for r in group),
                                  tools=sum(r["tools"] for r in group),
                                  initial_maintenance_tokens=total_tokens("initial_maintenance"),
                                  agent_tokens=total_tokens("agent"),
                                  post_maintenance_tokens=total_tokens("memory"),
                                  errors=sum(r["model_errors"] for r in group)))
    save(lab / "summary.json", summaries)
    with (lab / "results.csv").open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(summaries[0]))
        writer.writeheader()
        writer.writerows(summaries)
    print("Completed", len(results), "failed", len(failures), "maintenance snapshots", len(blind))
    for row in summaries:
        print(row["scene"], row["mode"], row["first_correct"], row["strict_success"], row["completed"])


if __name__ == "__main__":
    main()
