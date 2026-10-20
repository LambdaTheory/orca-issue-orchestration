#!/usr/bin/env python3
"""Stage-duration accounting for one orchestration run (read-only).

Time is attributed to the stage an issue entered. Durations are wall-clock
seconds from stage transitions, not worker CPU or model time. Missing history
is reported as untracked rather than guessed.
"""
from datetime import datetime

STAGES = ("ready", "blocked", "dev", "fix", "review", "verify", "user_gate", "merged", "closed")
LABELS = {
    "ready": "待派发", "blocked": "阻塞", "dev": "开发", "fix": "修复",
    "review": "评审", "verify": "验证", "user_gate": "等用户",
    "merged": "已合并", "closed": "已关闭", "unknown": "未记录",
}
EFFECTIVE = ("dev", "fix", "review", "verify")
WAITING = ("ready", "blocked", "user_gate")
TERMINAL = ("merged", "closed")


def now():
    return datetime.now().astimezone()


def iso(moment=None):
    return (moment or now()).isoformat(timespec="seconds")


def parse(value):
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.astimezone()


def fmt(seconds):
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return "%ds" % seconds
    minutes = seconds // 60
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return "%dh%02dm" % (hours, minutes)
    if hours:
        return "%dh" % hours
    return "%dm" % minutes


def _segments(item, started, end):
    history = [h for h in (item.get("history") or []) if parse(h.get("at"))]
    spans = []
    if history:
        for index, entry in enumerate(history):
            begin = parse(entry["at"])
            stop = parse(history[index + 1]["at"]) if index + 1 < len(history) else end
            spans.append((begin, stop, entry.get("to") or "unknown"))
    elif item.get("stage_since"):
        spans.append((parse(item["stage_since"]), end, item.get("stage") or "unknown"))
    return [(max(begin, started), min(stop, end), stage)
            for begin, stop, stage in spans if begin and stop and stop > begin]


def compute(state, end=None):
    started = parse(state.get("started_at"))
    finished = parse(state.get("finished_at"))
    end = finished or end or now()
    if not started:
        return {"started_at": None, "finished_at": None, "elapsed": 0, "effective": 0,
                "waiting": 0, "stages": {}, "issues": {}}

    elapsed = max(0.0, (end - started).total_seconds())
    stages = {stage: 0.0 for stage in STAGES}
    issues = {}
    for key, item in (state.get("issues") or {}).items():
        close_at = parse(item.get("stage_since")) if item.get("stage") in TERMINAL else None
        issue_end = min(close_at, end) if close_at else end
        spans = _segments(item, started, issue_end)
        if not spans:
            continue
        per_issue = {}
        for begin, stop, stage in spans:
            if stage in TERMINAL:
                continue
            seconds = max(0.0, (stop - begin).total_seconds())
            stages[stage] = stages.get(stage, 0.0) + seconds
            per_issue[stage] = per_issue.get(stage, 0.0) + seconds
        cycle_start = min(begin for begin, _, _ in spans)
        issues[str(key)] = {
            "title": item.get("title") or "",
            "seconds": max(0.0, (issue_end - max(cycle_start, started)).total_seconds()),
            "stages": per_issue,
        }

    return {
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "end": iso(end),
        "elapsed": elapsed,
        "effective": sum(stages.get(stage, 0.0) for stage in EFFECTIVE),
        "waiting": sum(stages.get(stage, 0.0) for stage in WAITING),
        "stages": stages,
        "issues": issues,
    }


def text(data):
    if not data.get("started_at"):
        return "未记录开始时间，无法统计时长。"

    def stamp(value):
        moment = parse(value)
        return moment.strftime("%Y-%m-%d %H:%M") if moment else "-"

    lines = ["本次编排 %s（%s → %s）" % (fmt(data["elapsed"]), stamp(data["started_at"]), stamp(data.get("finished_at") or data.get("end")))]
    lines.append("有效工作 %s · 等待 %s" % (fmt(data["effective"]), fmt(data["waiting"])))
    detail = [LABELS.get(stage, stage) + " " + fmt(seconds)
              for stage, seconds in data["stages"].items() if seconds >= 1]
    if detail:
        lines.append("阶段：" + " · ".join(detail))
    if data["issues"]:
        lines.append("Issue 周期：")
        for key in sorted(data["issues"], key=lambda value: int(value) if value.isdigit() else 10 ** 9):
            issue = data["issues"][key]
            parts = [LABELS.get(stage, stage) + " " + fmt(seconds)
                     for stage, seconds in issue["stages"].items() if seconds >= 1]
            suffix = "（%s）" % " / ".join(parts) if parts else ""
            title = str(issue.get("title") or "").strip()
            name = title if title.startswith("#") else ("#%s %s" % (key, title)).strip()
            lines.append("  %s %s%s" % (name, fmt(issue["seconds"]), suffix))
    return "\n".join(lines)
