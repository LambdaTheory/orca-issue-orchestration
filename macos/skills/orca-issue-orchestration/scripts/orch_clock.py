#!/usr/bin/env python3
"""Record when the skill was first used in this repository.

The clock lives at <repo>/.orchestration/clock.json and is local only. It is
advisory: state.json stays authoritative for each run, and orch_state.py copies
started_at from here when the run is initialized.
"""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import orch_time


def path(repo):
    return Path(repo) / ".orchestration" / "clock.json"


def load(repo):
    try:
        return json.loads(path(repo).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def ensure_ignored(repo):
    target = Path(repo) / ".gitignore"
    content = target.read_text(encoding="utf-8") if target.exists() else ""
    if any(line.strip() in (".orchestration/", "/.orchestration/") for line in content.splitlines()):
        return
    with open(target, "a", encoding="utf-8", newline="") as handle:
        handle.write(("\n" if content and not content.endswith("\n") else "")
                     + "# Local orchestration state\n/.orchestration/\n")


def save(repo, data):
    target = path(repo)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=target.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
    os.replace(temp, target)


def cmd_start(args):
    data = load(args.repo)
    active = data.get("started_at") and not data.get("finished_at")
    if active:
        print(data["started_at"])
        return
    ensure_ignored(args.repo)
    data = {"started_at": orch_time.iso(), "run": None, "finished_at": None}
    save(args.repo, data)
    print(data["started_at"])


def cmd_show(args):
    data = load(args.repo)
    if not data.get("started_at"):
        sys.exit("clock.json 不存在或缺少 started_at")
    print(json.dumps(data, ensure_ascii=False) if args.json else
          "开始 %s%s" % (data["started_at"],
                         " · 结束 " + data["finished_at"] if data.get("finished_at") else " · 进行中"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("start", help="记录本次 skill 开始；已有进行中的时钟则保留最早时间")
    show = sub.add_parser("show")
    show.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    {"start": cmd_start, "show": cmd_show}[args.cmd](args)


if __name__ == "__main__":
    main()
