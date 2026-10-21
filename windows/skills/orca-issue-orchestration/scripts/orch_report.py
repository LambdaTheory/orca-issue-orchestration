#!/usr/bin/env python3
"""Read-only hourly progress report for the orchestration coordinator.

Prints the current stages, the issues being developed or reviewed, PRs waiting
to merge, user gates and blockers from state.json. The coordinator relays the
output verbatim to its terminal; this script never changes state or dispatches.
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import orch_time

DEV = ("dev", "fix")
ORDER = ("blocked", "ready", "dev", "fix", "review", "verify", "user_gate", "merged", "closed")


def load(repo, run):
    path = Path(repo) / ".orchestration" / run / "state.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print("%s 不存在：这个 Run 还没初始化，先执行 init" % path, file=sys.stderr)
        sys.exit(2)
    except ValueError as exc:
        print("state.json invalid: %s" % exc, file=sys.stderr)
        sys.exit(2)


def number(key):
    return int(key) if str(key).isdigit() else 10 ** 9


def label(key, item):
    text = str(item.get("title") or "").strip()
    return text if text.startswith("#") else ("#%s %s" % (key, text)).strip()


def short(value, size=8):
    return str(value or "")[:size]


def head(item):
    return short(item.get("review_head") or item.get("delivery_sha") or item.get("review_base"))


def detail(item):
    parts = []
    agent = item.get("dev_agent") or item.get("fix_agent") or item.get("review_agent")
    if agent:
        parts.append(str(agent))
    worktree = item.get("dev_worktree") or item.get("worktree")
    if worktree:
        parts.append(Path(str(worktree)).name)
    if item.get("pr"):
        parts.append("PR #%s" % item["pr"])
    if item.get("stage") == "review":
        base, review = short(item.get("review_base")), short(item.get("review_head"))
        if base and review:
            parts.append("%s→%s" % (base, review))
    elif head(item):
        parts.append("HEAD %s" % head(item))
    if item.get("fix_round"):
        parts.append("修复第 %s 轮" % item["fix_round"])
    return "（%s）" % "，".join(parts) if parts else ""


def section(lines, name, items, extra):
    if not items:
        return
    lines.append("%s：" % name)
    shown = items[:5]
    for key, item in shown:
        lines.append("  - " + label(key, item) + extra(item))
    if len(items) > len(shown):
        lines.append("  - …另有 %d 个" % (len(items) - len(shown)))


def render(state):
    issues = state.get("issues") or {}
    items = sorted(((str(k), v) for k, v in issues.items()), key=lambda x: (ORDER.index(x[1].get("stage", "blocked")) if x[1].get("stage") in ORDER else 99, number(x[0])))

    def pick(*stages):
        return [(k, v) for k, v in items if v.get("stage") in stages]

    dev = pick("dev", "fix")
    review = pick("review")
    merging = [(k, v) for k, v in items if v.get("stage") == "verify" and v.get("delivery") == "pr_delivered"]
    verifying = [(k, v) for k, v in items if v.get("stage") == "verify" and v.get("delivery") != "pr_delivered"]
    ready = pick("ready")
    waiting = pick("user_gate")
    blocked = pick("blocked")
    done = pick("merged", "closed")

    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    lines = ["编排小时报 %s · %s · %s" % (now, state.get("run", "-"), state.get("objective", "-"))]
    counts = [("开发", len(dev)), ("评审", len(review)), ("待合并", len(merging)),
              ("验收中", len(verifying)), ("待派发", len(ready)), ("等用户", len(waiting)),
              ("阻塞", len(blocked)), ("完成", len(done))]
    lines.append("进度：" + " · ".join("%s %d" % (name, count) for name, count in counts if count))
    timing = orch_time.compute(state)
    if timing.get("started_at"):
        lines.append("已运行 %s（有效 %s / 等待 %s）" % (
            orch_time.fmt(timing["elapsed"]), orch_time.fmt(timing["effective"]), orch_time.fmt(timing["waiting"])))

    section(lines, "正在开发", dev, detail)
    section(lines, "评审中", review, detail)
    section(lines, "待合并", merging, detail)
    section(lines, "验收中", verifying, detail)
    section(lines, "待派发", ready, detail)
    section(lines, "等用户", waiting, lambda item: "（%s）" % item["open_question"] if item.get("open_question") else "")
    section(lines, "阻塞", blocked, lambda item: "（%s）" % item["blocker"] if item.get("blocker") else "")

    actions = [label(k, v) + "：" + str(v["next_action"]) for k, v in items if v.get("next_action")]
    if actions:
        lines.append("下一步：" + "；".join(actions[:3]))
    if len(lines) == 2:
        lines.append("当前没有在开发、评审或待合并的任务。")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--run", required=True)
    parser.add_argument("--wait", type=float, default=0, help="seconds to sleep before printing (hourly timer)")
    args = parser.parse_args(argv)
    if args.wait < 0:
        parser.error("--wait must be >= 0")
    if args.wait:
        time.sleep(args.wait)
    print(render(load(args.repo, args.run)))


if __name__ == "__main__":
    main()
