#!/usr/bin/env python3
"""File-based orchestration state: the coordinator keeps no memory between events.

State lives in <repo>/.orchestration/<run_id>/ (git-ignored, local only):
  state.json  — current truth: run, settings, issues, open questions, rules
  events.md   — append-only log of what happened and why
Every coordinator step: `show` → act on one event → `set`/`event` → done.
"""
import argparse, json, os, sys, tempfile
from datetime import datetime
from pathlib import Path

import orch_time

STAGES = ["blocked", "ready", "dev", "review", "fix", "verify", "merged", "user_gate", "closed"]


def state_dir(repo, run):
    return Path(repo) / ".orchestration" / run


def load(d):
    path = d / "state.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(f"{path} 不存在：这个 Run 还没初始化，先执行 init --objective \"...\"")
    except json.JSONDecodeError as exc:
        sys.exit(f"{path} 无法解析（{exc}）；修复或从备份恢复，不要直接覆盖")


def save(d, s):
    s["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    fd, tmp = tempfile.mkstemp(dir=d)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
    os.replace(tmp, d / "state.json")


def log(d, text):
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    with open(d / "events.md", "a", encoding="utf-8") as f:
        f.write(f"- {stamp} {text}\n")


def clock_path(repo):
    return Path(repo) / ".orchestration" / "clock.json"


def load_clock(repo):
    try:
        return json.loads(clock_path(repo).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def save_clock(repo, data):
    target = clock_path(repo)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=target.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
    os.replace(temp, target)


def cmd_init(a):
    d = state_dir(a.repo, a.run)
    if (d / "state.json").exists():
        sys.exit(f"{d}/state.json exists")
    gi = Path(a.repo) / ".gitignore"
    content = gi.read_text(encoding="utf-8") if gi.exists() else ""
    lines = content.splitlines()
    # Append after any negations; comments mentioning the path are not rules.
    if not lines or lines[-1] not in (".orchestration/", "/.orchestration/"):
        with open(gi, "a", encoding="utf-8", newline="") as f:
            f.write(("\n" if content and not content.endswith("\n") else "")
                    + "# Local orchestration state\n/.orchestration/\n")
    d.mkdir(parents=True, exist_ok=True)
    started = orch_time.iso()
    clock = load_clock(a.repo)
    if clock.get("started_at") and not clock.get("finished_at") and (
            not clock.get("run") or str(clock.get("run")) == str(a.run)):
        started = clock["started_at"]
        clock["run"] = a.run
        save_clock(a.repo, clock)
    elif not clock.get("started_at") or clock.get("finished_at"):
        save_clock(a.repo, {"started_at": started, "run": a.run, "finished_at": None})
    save(d, {"run": a.run, "repo": a.repo, "objective": a.objective,
             "started_at": started, "initialized_at": orch_time.iso(),
             "finished_at": None, "end_reason": None,
             "settings": {"max_parallel": a.max_parallel, "reviewers": a.reviewers, "next_reviewer": 0},
             "issues": {}, "questions": [], "rules": [], "notes": []})
    log(d, f"init run {a.run}")
    print(d)


def cmd_set(a):
    d = state_dir(a.repo, a.run); s = load(d)
    it = s["issues"].get(str(a.issue))
    if it is None:
        it = s["issues"][str(a.issue)] = {"stage": "blocked"}
        previous = None
    else:
        previous = it.get("stage")
    for kv in a.kv:
        k, _, v = kv.partition("=")
        if k == "stage" and v not in STAGES:
            sys.exit(f"stage must be one of {STAGES}")
        it[k] = None if v == "" else v
    if it.get("stage") != previous:
        moment = orch_time.iso()
        it["stage_since"] = moment
        entry = {"at": moment, "from": previous, "to": it.get("stage")}
        if a.why:
            entry["why"] = a.why
        it.setdefault("history", []).append(entry)
    save(d, s)
    if a.why:
        log(d, f"#{a.issue} " + ", ".join(a.kv) + f" — {a.why}")


def cmd_list_add(a, key):
    d = state_dir(a.repo, a.run); s = load(d)
    s[key].append({"text": a.text, "at": datetime.now().astimezone().isoformat(timespec="minutes")})
    save(d, s); log(d, f"{key}: {a.text}")


def cmd_resolve(a):
    d = state_dir(a.repo, a.run); s = load(d)
    q = s["questions"].pop(a.index)
    save(d, s); log(d, f"answered: {q['text']} → {a.answer}")


def cmd_event(a):
    log(state_dir(a.repo, a.run), a.text)


def cmd_reviewer(a):
    d = state_dir(a.repo, a.run); s = load(d); st = s["settings"]
    r = st["reviewers"][st["next_reviewer"] % len(st["reviewers"])]
    st["next_reviewer"] += 1; save(d, s); print(r)


def cmd_show(a):
    d = state_dir(a.repo, a.run); s = load(d)
    if a.issue is not None and a.issue not in s["issues"]:
        sys.exit(f"issue {a.issue} not found")
    active = [k for k, v in s["issues"].items()
              if v.get("stage") in ("dev", "review", "fix", "verify")
              and not (v.get("stage") == "verify" and v.get("delivery") == "pr_delivered")]
    timing = orch_time.compute(s)
    elapsed = f" · 已运行 {orch_time.fmt(timing['elapsed'])}" if timing.get("started_at") else ""
    print(f"run {s['run']} · {s['objective']} · max_parallel={s['settings']['max_parallel']} · active_issues={len(active)}{elapsed} · updated {s.get('updated_at')}")
    summary_fields = ("pr", "deps", "review_head", "delivery", "delivery_sha",
                      "worker_state", "next_action", "blocker", "checkpoint")
    for k, v in sorted(s["issues"].items(), key=lambda x: (STAGES.index(x[1].get("stage", "blocked")), int(x[0]) if x[0].isdigit() else 10**9, x[0])):
        if a.issue is not None:
            if k != a.issue:
                continue
        elif not a.full and v.get("stage") in ("merged", "closed"):
            continue
        fields = sorted(v) if a.full or a.issue is not None else summary_fields
        extra = " ".join(f"{f}={v[f]}" for f in fields if f not in ("stage", "title") and v.get(f))
        pending = " · 待合并" if v.get("stage") == "verify" and v.get("delivery") == "pr_delivered" else ""
        print(f"  #{k} [{v.get('stage')}{pending}] {v.get('title', '')} {extra}")
    for i, q in enumerate(s["questions"]):
        print(f"  ? [{i}] {q['text']}")
    for r in s["rules"]:
        print(f"  ! {r['text']}")
    for n in (s["notes"] if a.full else s["notes"][-3:]):
        print(f"  - {n['text']}")
    if a.events:
        lines = (d / "events.md").read_text(encoding="utf-8").splitlines()
        print("\n".join(lines[-a.events:]))


def cmd_finish(a):
    d = state_dir(a.repo, a.run); s = load(d)
    if not s.get("finished_at"):
        s["finished_at"] = orch_time.iso()
        s["end_reason"] = a.reason
        save(d, s)
        clock = load_clock(a.repo)
        if clock.get("started_at") and (not clock.get("run") or str(clock.get("run")) == str(a.run)):
            clock["finished_at"] = s["finished_at"]
            clock["run"] = a.run
            save_clock(a.repo, clock)
        log(d, f"finish run {a.run}: {a.reason}")
    data = orch_time.compute(s)
    (d / "timing.md").write_text(orch_time.text(data) + "\n", encoding="utf-8")
    print(orch_time.text(data))
    print("timing.md: %s" % (d / "timing.md"))


def cmd_timing(a):
    d = state_dir(a.repo, a.run); s = load(d)
    data = orch_time.compute(s)
    print(json.dumps(data, ensure_ascii=False, indent=1) if a.json else orch_time.text(data))


def nonempty_reviewer(v):
    r = v.strip()
    if not r:
        raise argparse.ArgumentTypeError("reviewer must be a non-blank identifier")
    return r


p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--repo", default=".")
p.add_argument("--run", required=True)
sub = p.add_subparsers(dest="cmd", required=True)
x = sub.add_parser("init"); x.add_argument("--objective", required=True); x.add_argument("--max-parallel", type=int, default=1); x.add_argument("--reviewers", nargs="+", required=True, type=nonempty_reviewer); x.set_defaults(f=cmd_init)
x = sub.add_parser("set", help="set fields on an issue: stage=review pr=34 dispatch=ctx_.. terminal=term_.. worktree=.."); x.add_argument("issue"); x.add_argument("kv", nargs="+"); x.add_argument("--why"); x.set_defaults(f=cmd_set)
x = sub.add_parser("ask", help="record a question waiting for the user"); x.add_argument("text"); x.set_defaults(f=lambda a: cmd_list_add(a, "questions"))
x = sub.add_parser("answer"); x.add_argument("index", type=int); x.add_argument("answer"); x.set_defaults(f=cmd_resolve)
x = sub.add_parser("rule", help="record a decision every coordinator must honor"); x.add_argument("text"); x.set_defaults(f=lambda a: cmd_list_add(a, "rules"))
x = sub.add_parser("note"); x.add_argument("text"); x.set_defaults(f=lambda a: cmd_list_add(a, "notes"))
x = sub.add_parser("event"); x.add_argument("text"); x.set_defaults(f=cmd_event)
x = sub.add_parser("reviewer", help="print and rotate the next reviewer"); x.set_defaults(f=cmd_reviewer)
x = sub.add_parser("show"); x.add_argument("--events", type=int, default=0); x.set_defaults(f=cmd_show)
view = x.add_mutually_exclusive_group()
view.add_argument("--issue", help="show all fields for one issue, including completed issues")
view.add_argument("--full", action="store_true", help="show all issues, fields and notes")
x = sub.add_parser("finish", help="record the end time, write timing.md and print the summary"); x.add_argument("--reason", choices=["completed", "paused", "cancelled"], default="completed"); x.set_defaults(f=cmd_finish)
x = sub.add_parser("timing", help="print elapsed, effective and waiting time by stage"); x.add_argument("--json", action="store_true"); x.set_defaults(f=cmd_timing)
a = p.parse_args(); a.f(a)
