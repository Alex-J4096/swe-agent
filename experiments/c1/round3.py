"""Cross old-entry direction with verified learning demonstrations."""
import argparse
from contextlib import chdir
from concurrent.futures import ProcessPoolExecutor
import json
from pathlib import Path
import random
import shutil
import tempfile

from . import run as c

LAB = c.ROOT / ".swe-agent/experiments/c1-round3"
PILOT = LAB.parent / "c1-pair-01"
NAMES = {"settings": c.OLD, "app": c.NEW}
VERIFY = "请实际运行 python3 service.py 验证。"


def setup():
    db = c.default_database_path().expanduser().resolve()
    sandbox = c.ExperimentSandbox([c.ROOT, LAB, db, db.parent / (db.name+"-wal"), db.parent / (db.name+"-shm")])
    profile = c.BUILTIN_PROVIDERS[0]
    database = c.AppDatabase(db)
    try:
        credential = c.CredentialService(database).resolve(profile)
    finally:
        database.close()
    return sandbox, c.Provider(profile.name, credential.token, profile.base_url)


def prepare(label):
    direction, style = label.split("-")
    target = NAMES[direction]
    prompt = c.LEARN + ("只修改当前生效的配置文件。" if style == "single" else "请同步两份配置文件。") + VERIFY
    sandbox, provider = setup()
    for attempt in range(1, 4):
        stage = f"learn-{label}-{attempt}"
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp).resolve()
            c.fixture(repo, target, 10)
            with chdir(repo):
                events = c.run_turn(LAB, stage, repo, sandbox, provider, prompt, True, [])
            grade = c.grade(repo, sandbox, target, 30)
            touched = {name for e in events if e["event"] == "workspace_delta" for name in e["changes"]}
            verified = any(e["event"] == "tool_result" and e["tool"] == "run_bash" and
                           json.loads(e["arguments"]).get("command","").strip() in c.SERVICE_COMMANDS and
                           e["output"].get("ok") and e["output"].get("stdout","").strip() == "30" for e in events)
            accepted = (grade["success"] and grade["loader_intact"] and grade["unrelated_fields_intact"]
                        and verified and touched == ({target} if style == "single" else set(NAMES.values())))
            c.save(LAB / (stage+".grade.json"), dict(grade, accepted=accepted, touched=sorted(touched), verified=verified))
            if accepted:
                c.snapshot(repo, LAB / label)
                shutil.copyfile(LAB / (stage+".history.json"), LAB / (label+".history.json"))
                return
    raise RuntimeError(label+" preparation failed")


def test(job):
    label, condition, rep = job
    direction = label.split("-")[0]
    target = c.NEW if direction == "settings" else c.OLD
    stage = f"{label}-{condition}-{rep}"
    sandbox, provider = setup()
    prior = []
    with tempfile.TemporaryDirectory() as temp:
        repo = Path(temp).resolve()
        c.fixture(repo, target, 30)
        if condition == "default":
            c.copy_memory(LAB / label, repo)
        if condition == "full":
            prior = json.loads((LAB / (label+".history.json")).read_text())
        with chdir(repo):
            events = c.run_turn(LAB, stage, repo, sandbox, provider, c.TASK, condition=="default", prior)
        row = c.summarize(events, target)
        first = row["delta_sequence"] or float("inf")
        seen = c.observe_evidence([e for e in events if e["sequence"] < first], target)
        changes = [e for e in events if e["event"]=="workspace_delta" and e["changes"]]
        wrong = [e for e in events if e["event"]=="tool_result" and e["tool"]=="run_bash"
                 and json.loads(e["arguments"]).get("command","").strip() in c.SERVICE_COMMANDS
                 and e["output"].get("ok") and e["output"].get("stdout","").strip()=="30" and e["sequence"]>first]
        touched = {k for e in changes for k in e["changes"]}
        row.update(c.grade(repo,sandbox,target,60), stage=stage, target=target, loader_seen=bool(seen),
                   both=set(NAMES.values())<=touched, rework=any(w["sequence"]<e["sequence"] for w in wrong for e in changes))
        c.save(LAB / (stage+".result.json"), row)
        print(stage,row["first_action"],flush=True)
        return row


def main():
    LAB.mkdir()
    sandbox,_ = setup()
    with tempfile.TemporaryDirectory() as temp:
        c.preflight(LAB,Path(temp).resolve(),sandbox,c.default_database_path().expanduser().resolve())
    labels = [f"{d}-{s}" for d in NAMES for s in ("single","double")]
    # Keep the prior natural double-write as an additional, separate replication.
    c.snapshot(PILOT / "seed",LAB / "settings-natural")
    shutil.copyfile(PILOT / "learn.history.json",LAB / "settings-natural.history.json")
    jobs = [(label,cond,r) for label in labels+["settings-natural"] for cond in ("full","default") for r in (1,2)]
    jobs += [(d+"-control","none",r) for d in NAMES for r in (1,2)]
    random.Random(4098).shuffle(jobs)
    c.save(LAB/"manifest.json",dict(jobs=jobs,model=c.MODEL,task=c.TASK,seed=4098))
    shutil.copyfile(__file__,LAB/"round3.py")
    with ProcessPoolExecutor(3) as pool:
        list(pool.map(prepare,labels))
        c.save(LAB/"results.json",list(pool.map(test,jobs)))


if __name__ == "__main__":
    main()
