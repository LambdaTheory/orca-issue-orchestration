#!/usr/bin/env python3
"""Write the final duration section into the run's last PR (idempotent).

The coordinator runs this only after `S finish`, so the numbers are final. The
existing PR body is preserved: an old `## 工作时长` section is replaced, never
duplicated.
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import orch_time

MARKER = "## 工作时长"
REASONS = {"completed": "完成", "paused": "暂停", "cancelled": "取消"}


def load(repo, run):
    path = Path(repo) / ".orchestration" / run / "state.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit("state.json not found: %s" % path)
    except ValueError as exc:
        sys.exit("state.json invalid: %s" % exc)


def detail(stages, names):
    parts = ["%s %s" % (orch_time.LABELS[name], orch_time.fmt(stages.get(name, 0)))
             for name in names if stages.get(name, 0) >= 1]
    return " · ".join(parts) if parts else "无"


def render(state):
    data = orch_time.compute(state)
    if not data.get("started_at"):
        return MARKER + "\n\n未记录开始时间，无法统计时长。\n"
    start = orch_time.parse(data["started_at"])
    end = orch_time.parse(state.get("finished_at")) or orch_time.parse(data["end"])
    stamp = lambda moment: moment.strftime("%Y-%m-%d %H:%M") if moment else "-"
    lines = [
        MARKER,
        "",
        "- 总历时：%s（%s → %s）" % (orch_time.fmt(data["elapsed"]), stamp(start), stamp(end)),
        "- 有效工作：%s（%s）" % (orch_time.fmt(data["effective"]), detail(data["stages"], orch_time.EFFECTIVE)),
        "- 等待：%s（%s）" % (orch_time.fmt(data["waiting"]), detail(data["stages"], orch_time.WAITING)),
    ]
    reason = REASONS.get(str(state.get("end_reason") or ""), "")
    if reason:
        lines.append("- 结束状态：%s" % reason)
    lines.append("- 统计口径：从 skill 启动（C start）到收口（S finish）的墙钟时间；"
                 "并发任务按 Issue 累加，明细见 `.orchestration/%s/timing.md`" % state.get("run", ""))
    return "\n".join(lines) + "\n"


def upsert(body, section):
    lines = body.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == MARKER), None)
    if start is None:
        return body.rstrip() + ("\n\n" if body.strip() else "") + section.strip() + "\n"
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    prefix = "\n".join(lines[:start]).rstrip()
    suffix = "\n".join(lines[end:]).strip()
    parts = [part for part in (prefix, section.strip(), suffix) if part]
    return "\n\n".join(parts) + "\n"


def gh(repo, *args):
    try:
        return subprocess.run(["gh", *args], cwd=repo, capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        sys.exit("gh 调用失败: %s" % exc)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--run", required=True)
    parser.add_argument("--pr", required=True, help="final PR number or URL")
    parser.add_argument("--dry-run", action="store_true", help="print the merged body without writing")
    args = parser.parse_args(argv)

    state = load(args.repo, args.run)
    if not state.get("finished_at"):
        sys.exit("run 尚未收口，请先执行 S finish")

    viewed = gh(args.repo, "pr", "view", str(args.pr), "--json", "body,url,state")
    if viewed.returncode:
        sys.exit("读取 PR 失败: %s" % (viewed.stderr or viewed.stdout).strip())
    info = json.loads(viewed.stdout)
    body = upsert(info.get("body") or "", render(state))
    if args.dry_run:
        print(body)
        return

    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False)
    try:
        handle.write(body)
        handle.close()
        edited = gh(args.repo, "pr", "edit", str(args.pr), "--body-file", handle.name)
        if edited.returncode:
            sys.exit("更新 PR 正文失败: %s" % (edited.stderr or edited.stdout).strip())
    finally:
        Path(handle.name).unlink(missing_ok=True)

    checked = gh(args.repo, "pr", "view", str(args.pr), "--json", "body,url")
    if checked.returncode:
        sys.exit("回读 PR 失败: %s" % (checked.stderr or checked.stdout).strip())
    result = json.loads(checked.stdout)
    if (result.get("body") or "").count(MARKER) != 1:
        sys.exit("回读正文缺少或重复 工作时长 章节，未确认成功")
    print("已回填 %s" % result.get("url", args.pr))


if __name__ == "__main__":
    main()
