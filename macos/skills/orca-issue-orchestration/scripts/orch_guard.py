#!/usr/bin/env python3
"""Keep an orchestration run moving when an agent stops on an approval prompt.

The guard owns no secrets and makes no judgement beyond the orch_policy table.
Each tick it looks at the coordinator terminal and every live worker terminal:

  waiting on a known tool call -> policy says allow -> press the matching option
  waiting on anything else     -> write a hold, never press a key
  agent gone / silent too long -> nudge once, then hold and report

It never restarts, kills or re-dispatches a worker. Those stay coordinator calls.
Every decision is appended to audit.jsonl so an auto-approval can be reviewed.

Layout under --state-dir:
  guard.json     control (desired, generation, pid, heartbeat) and options
  audit.jsonl    one line per observation and per applied decision
  holds/         open questions for a human or the standby agent
  verdicts/      answers written by the standby agent
  seen.json      prompt signatures already handled, to avoid double pressing
"""
import argparse, hashlib, json, os, re, subprocess, sys, tempfile, time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import orch_policy as policy

ORCA = os.environ.get("ORCA_CLI_BIN") or "orca"
TICK = 5
RUNTIME_FRESH = 180
PROMPT_COOLDOWN = 60
MAX_ATTEMPTS = 3

ALLOW_START = re.compile(r"^(yes|y\b|allow|approve|ok\b|允许|同意|批准)", re.I)
REPEAT_HINTS = ("don't ask", "do not ask", "always allow", "不再询问", "始终允许")
DIALOG_HINTS = ("permission", "allow", "approve", "do you want to", "would you like to",
                "需要你的许可", "是否允许", "允许执行", "批准", "confirm")
OPTION_LINE = re.compile(r"^(?:[❯>»●○]\s*)?(\d{1,2})[.)、]\s+(\S.*?)$")
BOX_CHARS = "│┃|║ \t"


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def orca_json(argv, timeout=30):
    """Same envelope handling as orch_wait: never raise, always return a dict."""
    try:
        proc = subprocess.run([ORCA, *argv], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, encoding="utf-8", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": {"code": "cli_timeout", "message": f"orca {' '.join(argv[:2])} timed out"}}
    except OSError as exc:
        return {"ok": False, "error": {"code": "cli_missing", "message": str(exc)}}
    raw = proc.stdout.strip()
    if not raw:
        return {"ok": False, "error": {"code": "empty", "message": proc.stderr.strip() or f"exit {proc.returncode}"}}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "error": {"code": "bad_json", "message": raw[:300]}}
    if not isinstance(data, dict):
        return {"ok": False, "error": {"code": "bad_response", "message": "expected object"}}
    if data.get("ok") is False and "error" not in data:
        data["error"] = {"code": "orca_error", "message": ""}
    return data


# ---------------------------------------------------------------- screen parsing

def parse_options(screen):
    """Numbered options with the selected marker; tolerant of TUI decoration."""
    options = []
    for line in screen:
        clean = line.strip().strip(BOX_CHARS).strip()
        match = OPTION_LINE.match(clean)
        if not match:
            continue
        number = int(match.group(1))
        text = match.group(2).strip().rstrip(BOX_CHARS).strip()
        if not text or len(text) > 120:
            continue
        options.append((number, text))
    return options


def pick_allow_option(options):
    """Prefer the plain Yes over 'yes, and don't ask again' (least privilege)."""
    plain, repeat = [], []
    for number, text in options:
        if not ALLOW_START.match(text):
            continue
        bucket = repeat if any(hint in text.lower() for hint in REPEAT_HINTS) else plain
        bucket.append((number, text))
    chosen = (plain or repeat or [None])[0]
    return chosen


def decline_option(options):
    """A real confirm dialog always offers a way out; its absence means we misread the screen."""
    return any(re.match(r"^(no\b|cancel|deny|reject|拒绝|取消|不允许)", text, re.I) for _, text in options)


def looks_like_dialog(screen):
    blob = "\n".join(screen).lower()
    return any(hint in blob for hint in DIALOG_HINTS)


def dialog_signature(screen):
    return hashlib.sha1("\n".join(screen[-40:]).encode("utf-8", "replace")).hexdigest()[:16]


# ---------------------------------------------------------------- transcript

def pending_tool_call(path, max_age_seconds=900):
    """Last tool call with no result, for codebuddy and claude transcript formats."""
    if not path:
        return None
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[-800:]
    except OSError:
        return None
    calls, results = {}, set()
    for index, line in enumerate(lines):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = record.get("type")
        if kind == "function_call" and record.get("callId"):
            calls[record["callId"]] = {
                "id": record["callId"],
                "tool": record.get("name") or "",
                "input": _json_object(record.get("arguments")),
                "ts": record.get("timestamp") or 0,
                "seq": index,
            }
        elif kind == "function_call_result" and record.get("callId"):
            results.add(record["callId"])
        elif kind == "assistant":
            for item in (record.get("message") or {}).get("content") or []:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    calls[item.get("id")] = {"id": item.get("id"), "tool": item.get("name") or "",
                                             "input": item.get("input") or {},
                                             "ts": record.get("timestamp") or 0, "seq": index}
        elif kind == "user":
            for item in (record.get("message") or {}).get("content") or []:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    results.add(item.get("tool_use_id"))
    pending = [call for call_id, call in calls.items() if call_id not in results and call_id]
    if not pending:
        return None
    latest = max(pending, key=lambda call: (call.get("seq") or 0, call.get("ts") or 0))
    stamp = latest.get("ts") or 0
    if stamp > 10_000_000_000:  # milliseconds
        stamp /= 1000
    if stamp and time.time() - stamp > max_age_seconds:
        return None
    return latest


def _json_object(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def transcript_for(terminal, status_path):
    """Orca records the provider session per pane; only trust a pane-level match."""
    status = read_json(status_path, {})
    key = f"{terminal.get('tabId')}:{terminal.get('leafId')}"
    entry = (status.get("entries") or {}).get(key) or {}
    session = entry.get("providerSession") or {}
    path = session.get("transcriptPath") or (entry.get("payload") or {}).get("transcript_path")
    return path if isinstance(path, str) and path else ""


# ---------------------------------------------------------------- guard

class Guard:
    def __init__(self, state_dir, options, request=orca_json, clock=time.time, log=print, sleep=time.sleep):
        self.dir = Path(state_dir)
        self.holds_dir = self.dir / "holds"
        self.verdicts_dir = self.dir / "verdicts"
        self.audit_path = self.dir / "audit.jsonl"
        self.opts = options
        self.request = request
        self.clock = clock
        self.log = log
        self.sleep = sleep
        self.seen_path = self.dir / "seen.json"
        self.seen = read_json(self.seen_path, {})

    # -- state ------------------------------------------------------------
    def control(self):
        return read_json(self.dir / "guard.json", {})

    def save_control(self, data):
        write_json(self.dir / "guard.json", data)

    def heartbeat(self, extra=None):
        control = self.control()
        control["heartbeat_ts"] = now_iso()
        control["pid"] = os.getpid()
        if extra:
            control.update(extra)
        self.save_control(control)
        return control

    def audit(self, record):
        record = {"ts": now_iso(), **record}
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.audit_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def paused(self):
        if (self.dir / "PAUSED").exists():
            return "PAUSED 标记存在"
        control = self.control()
        if control.get("desired") == "paused":
            return control.get("pause_reason") or "desired=paused"
        return ""

    def stopping(self):
        return self.control().get("desired") in ("stopped",)

    # -- targets ----------------------------------------------------------
    def targets(self):
        rows = []
        handle = self.opts.get("coordinator") or os.environ.get("ORCA_TERMINAL_HANDLE", "")
        if handle:
            rows.append({"label": "coordinator", "handle": handle})
        run = self.opts.get("run") or ""
        if run:
            result = self.request(["orchestration", "worker-list", "--run", run, "--include-remote", "--json"])
            if result.get("ok") is True:
                for worker in (result.get("result") or {}).get("workers") or []:
                    if not isinstance(worker, dict):
                        continue
                    if worker.get("dispatchStatus") in ("completed", "failed", "stopped", "cancelled"):
                        continue
                    terminal = worker.get("agentTerminalHandle")
                    if terminal:
                        rows.append({"label": worker.get("dispatchId") or "worker", "handle": terminal})
        return rows

    def observe(self, target):
        result = self.request(["terminal", "show", "--terminal", target["handle"], "--json"], timeout=15)
        if result.get("ok") is not True:
            return {"label": target["label"], "handle": target["handle"], "state": "unreadable",
                    "error": result.get("error")}
        terminal = (result.get("result") or {}).get("terminal") or {}
        return {
            "label": target["label"],
            "handle": target["handle"],
            "state": "waiting" if terminal.get("agentWait") else "running",
            "connected": bool(terminal.get("connected")),
            "terminal": terminal,
        }

    def screen(self, handle):
        result = self.request(["terminal", "read", "--terminal", handle, "--screen", "--json"], timeout=15)
        if result.get("ok") is not True:
            return []
        return list((result.get("result") or {}).get("terminal", {}).get("tail") or [])

    def send(self, handle, text):
        return self.request(["terminal", "send", "--terminal", handle, "--text", text, "--enter", "--json"],
                            timeout=20)

    # -- decisions --------------------------------------------------------
    def signature(self, label, call):
        blob = json.dumps({"label": label, "tool": call.get("tool"), "input": call.get("input")},
                          ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]

    def cool(self, signature):
        entry = self.seen.get(signature) or {}
        last = entry.get("last") or 0
        if entry.get("applied", 0) >= MAX_ATTEMPTS:
            return "attempts", entry
        if self.clock() - last < PROMPT_COOLDOWN:
            return "cooldown", entry
        return "", entry

    def remember(self, signature, outcome):
        entry = self.seen.setdefault(signature, {"applied": 0, "last": 0})
        entry["last"] = self.clock()
        entry["outcome"] = outcome
        if outcome == "applied":
            entry["applied"] = entry.get("applied", 0) + 1
        write_json(self.seen_path, self.seen)

    def hold(self, observation, call, decision, reason):
        base = hashlib.sha1(f"{observation['handle']}:{decision.get('rule')}:{self.clock()}".encode()).hexdigest()[:12]
        hold_id, suffix = base, 1
        while (self.holds_dir / f"{hold_id}.json").exists():
            suffix += 1
            hold_id = f"{base}-{suffix}"
        payload = {
            "id": hold_id,
            "ts": now_iso(),
            "state": "open",
            "terminal": observation["handle"],
            "label": observation["label"],
            "tool": (call or {}).get("tool"),
            "input": (call or {}).get("input"),
            "rule": decision.get("rule"),
            "reason": reason or decision.get("reason"),
        }
        write_json(self.holds_dir / f"{hold_id}.json", payload)
        self.audit({"event": "hold", **{k: payload[k] for k in ("id", "terminal", "label", "tool", "rule", "reason")}})
        self.log(f"[guard] HOLD {hold_id} {observation['label']} rule={payload['rule']} {payload['reason']}")
        return payload

    def apply(self, observation, call, decision, create_hold=True, screen=None):
        handle = observation["handle"]
        screen = screen if screen is not None else self.screen(handle)
        options = parse_options(screen)
        chosen = pick_allow_option(options)
        if not looks_like_dialog(screen) or chosen is None or not decline_option(options):
            reason = "弹窗结构无法确认，拒绝盲发按键" if options or screen else "读不到屏幕"
            return self.hold(observation, call, decision, reason) if create_hold \
                else {"state": "unverified", "reason": reason}
        before = dialog_signature(screen)
        number, text = chosen
        sent = self.send(handle, str(number))
        if sent.get("ok") is not True:
            self.audit({"event": "inject_failed", "terminal": handle, "option": number,
                        "error": sent.get("error")})
            return self.hold(observation, call, decision, "按键发送失败") if create_hold \
                else {"state": "inject_failed"}
        self.sleep(self.opts.get("verify_wait", 2.0))
        after_screen = self.screen(handle)
        if dialog_signature(after_screen) == before:
            self.audit({"event": "verify_failed", "terminal": handle, "option": number})
            return self.hold(observation, call, decision, "按键已发送但弹窗仍在，需人工确认结果") \
                if create_hold else {"state": "verify_failed"}
        self.audit({"event": "applied", "terminal": handle, "label": observation["label"],
                    "tool": call.get("tool"), "option": number, "option_text": text,
                    "rule": decision.get("rule")})
        self.remember(self.signature(observation["label"], call), "applied")
        self.log(f"[guard] allow {observation['label']} {call.get('tool')} via option {number} ({decision.get('rule')})")
        return {"state": "applied", "option": number}

    # -- stall nudge ------------------------------------------------------
    def unfinished(self):
        """True only when work is left AND nobody is legitimately parked waiting."""
        repo, run = self.opts.get("repo") or "", self.opts.get("run") or ""
        if not repo or not run:
            return False
        state = read_json(Path(repo) / ".orchestration" / run / "state.json", {})
        if state.get("finished_at"):
            return False
        if state.get("questions"):
            return False
        issues = state.get("issues") or {}
        if not issues:
            return False
        pending = [issue or {} for issue in issues.values()]
        if any(issue.get("stage") == "user_gate" for issue in pending):
            return False
        return any(issue.get("stage") not in ("merged", "closed") for issue in pending)

    def maybe_nudge(self, observation, active_workers=0):
        if not self.opts.get("nudge", True) or not self.unfinished():
            return None
        if active_workers:
            return None  # a worker is still running; a parked coordinator is correct
        call = pending_tool_call(self.transcript_path(observation), self.opts.get("transcript_max_age", 900))
        if call is not None:
            return None
        control = self.control()
        nudges = int(control.get("nudges") or 0)
        if nudges >= self.opts.get("max_nudges", 3):
            return None
        last = (observation.get("terminal") or {}).get("lastOutputAt") or 0
        if last > 10_000_000_000:
            last /= 1000
        idle = self.clock() - last if last else 0
        if idle < self.opts.get("idle_seconds", 300):
            return None
        sent = self.send(observation["handle"],
                         "继续推进编排：读取当前状态，处理可执行项；若已无待办则按 Skill 收口。")
        if sent.get("ok") is not True:
            return None
        control["nudges"] = nudges + 1
        control["last_nudge_at"] = now_iso()
        self.save_control(control)
        self.audit({"event": "nudge", "terminal": observation["handle"], "count": nudges + 1, "idle": round(idle)})
        self.log(f"[guard] nudge {observation['label']} (idle {int(idle)}s, 第 {nudges + 1} 次)")
        return nudges + 1

    def transcript_path(self, observation):
        return transcript_for(observation.get("terminal") or {}, self.opts.get("status_file")
                              or os.path.expanduser("~/.orca/agent-hooks/last-status.json"))

    # -- one pass ---------------------------------------------------------
    def tick(self):
        control = self.heartbeat()
        if control.get("desired") == "stopped":
            return "stopped"
        paused = self.paused()
        if not paused:
            self.apply_user_verdicts()
        seen_handles = set()
        targets = self.targets()
        active_workers = len([t for t in targets if t["label"] != "coordinator"])
        for target in targets:
            if target["handle"] in seen_handles:
                continue
            seen_handles.add(target["handle"])
            observation = self.observe(target)
            if observation["state"] != "waiting":
                if observation["state"] == "running" and observation["label"] == "coordinator" and not paused:
                    self.maybe_nudge(observation, active_workers)
                continue
            # agentWait also fires for momentary states, so the rendered screen decides
            # whether a prompt is really on screen. No dialog, no action.
            screen = self.screen(observation["handle"])
            if not looks_like_dialog(screen):
                continue
            call = pending_tool_call(self.transcript_path(observation),
                                     self.opts.get("transcript_max_age", 900))
            signature = self.signature(observation["label"], call or {"tool": None, "input": None})
            state, _ = self.cool(signature)
            if state:
                continue
            self.audit({"event": "prompt", "terminal": observation["handle"], "label": observation["label"],
                        "tool": (call or {}).get("tool"), "paused": bool(paused)})
            if paused:
                self.remember(signature, "paused")
                continue
            if call is None:
                self.hold(observation, None, {"rule": "unknown-tool", "reason": "读不到待批的工具调用"},
                          "读不到待批的工具调用")
                self.remember(signature, "hold")
                continue
            decision = policy.decide(call.get("tool"), call.get("input") or {}, self.policy_ctx())
            if decision["decision"] != "allow":
                self.hold(observation, call, decision, decision.get("reason"))
                self.remember(signature, "hold")
                continue
            outcome = self.apply(observation, call, decision, screen=screen)
            if outcome.get("state") != "applied":
                self.remember(signature, "hold")
        return "ran"

    def policy_ctx(self):
        return policy.Ctx(
            workspace=self.opts.get("workspace") or self.opts.get("repo") or os.getcwd(),
            branch=self.opts.get("branch") or "",
            extra_allow=self.opts.get("extra_allow") or [],
            run_dirs=[str(Path(self.opts["repo"]) / ".orchestration" / self.opts["run"])]
            if self.opts.get("repo") and self.opts.get("run") else [],
            skill_dirs=self.opts.get("skill_dirs") or [],
        )

    # -- standby agent ----------------------------------------------------
    def pending_holds(self):
        rows = []
        for path in sorted(self.holds_dir.glob("*.json")):
            data = read_json(path, {})
            if data.get("state") == "open" and not data.get("agent_requested"):
                rows.append((path, data))
        return rows

    def open_holds(self):
        """Everything that still needs a human, including holds the agent declined."""
        rows = []
        for path in sorted(self.holds_dir.glob("*.json")):
            data = read_json(path, {})
            if data.get("state") in ("open", "denied-by-agent", "apply-failed"):
                rows.append(data)
        return rows

    def apply_verdict(self, hold_path, payload, by="agent"):
        observation = {"handle": payload.get("terminal"), "label": payload.get("label") or "worker"}
        call = {"tool": payload.get("tool"), "input": payload.get("input") or {}}
        outcome = self.apply(observation, call,
                             {"rule": f"{by}-verdict", "reason": "判定放行"},
                             create_hold=False)
        applied = outcome.get("state") == "applied"
        payload["state"] = f"resolved-by-{by}" if applied else "apply-failed"
        payload["apply_outcome"] = outcome.get("state")
        write_json(hold_path, payload)
        self.audit({"event": "verdict_applied", "id": payload["id"], "by": by,
                    "outcome": outcome.get("state")})
        self.log(f"[guard] {by} 放行 {payload['id']} -> {outcome.get('state')}")

    def apply_user_verdicts(self):
        """A hold the coordinator approved by CLI still has to be pressed here."""
        for path in sorted(self.holds_dir.glob("*.json")):
            payload = read_json(path, {})
            if payload.get("state") == "resolved-by-user":
                self.apply_verdict(path, payload, by="user")

    def collect_verdicts(self):
        for path in sorted(self.verdicts_dir.glob("*.json")):
            data = read_json(path, {})
            hold_id, decision = data.get("id"), data.get("decision")
            if not hold_id or decision not in ("allow", "deny"):
                continue
            target = self.holds_dir / f"{hold_id}.json"
            payload = read_json(target, {})
            path.unlink(missing_ok=True)
            if not payload:
                continue
            payload["agent_reason"] = data.get("reason")
            self.audit({"event": "verdict", "id": hold_id, "decision": decision,
                        "reason": data.get("reason")})
            if decision == "allow":
                self.apply_verdict(target, payload)
                continue
            payload["state"] = "denied-by-agent"
            write_json(target, payload)
            self.log(f"[guard] agent 拒绝 {hold_id}：{data.get('reason')}")

    def request_agent(self, path, payload):
        prompt = (
            "你是编排守护的待命判定者，只做一件事：判断这条被挂起的授权请求是否属于本次编排的正常流程。\n"
            f"读取 {path}，其中 tool/input 是待批的调用，rule/reason 是策略拒绝自动放行的原因。\n"
            "判定标准：只放行与当前 issue 交付直接相关、可回滚、不涉及凭据/远端强改/系统目录的操作；"
            "任何说不清的都写 deny。\n"
            f"然后把 {{\"id\":\"{payload['id']}\",\"decision\":\"allow|deny\",\"reason\":\"...\"}} "
            f"写入 {self.verdicts_dir / (payload['id'] + '.json')}。只写这一个文件，不要执行被挂起的命令。"
        )
        payload["agent_requested"] = True
        payload["agent_requested_at"] = now_iso()
        write_json(path, payload)
        agent = self.opts.get("agent_cmd") or "codebuddy"
        log_path = self.dir / "agent.log"
        try:
            with open(log_path, "a", encoding="utf-8") as log:
                subprocess.Popen([agent, "-p", prompt, "--permission-mode", "acceptEdits"],
                                 stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                 start_new_session=True)
        except OSError as exc:
            self.audit({"event": "agent_unavailable", "error": str(exc)})
            return False
        self.audit({"event": "agent_requested", "id": payload["id"]})
        self.log(f"[guard] 已唤起待命 Agent 判定 {payload['id']}")
        return True

    def handle_holds(self):
        self.collect_verdicts()
        if not self.opts.get("agent_on_hold", True):
            return
        for path, payload in self.pending_holds():
            self.request_agent(path, payload)


# ---------------------------------------------------------------- CLI

def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state-dir", required=True)
    p.add_argument("--coordinator", default="", help="terminal handle of the coordinator")
    p.add_argument("--run", default="")
    p.add_argument("--repo", default="")
    p.add_argument("--workspace", default="")
    p.add_argument("--branch", default="")
    p.add_argument("--skill-dir", action="append", default=[])
    p.add_argument("--extra-allow", default="", help="JSON file with extra allow regexes")
    p.add_argument("--idle-seconds", type=int, default=300)
    p.add_argument("--max-nudges", type=int, default=3)
    p.add_argument("--no-nudge", action="store_true")
    p.add_argument("--no-agent", action="store_true")
    p.add_argument("--agent-cmd", default="codebuddy")
    p.add_argument("--interval", type=float, default=TICK)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pane", help="run in the split terminal, with the standby agent")
    sub.add_parser("serve", help="run the loop without the standby agent")
    sub.add_parser("start", help="spawn serve detached (no split terminal)")
    sub.add_parser("tick", help="run a single pass and exit")
    s = sub.add_parser("status"); s.add_argument("--json", action="store_true")
    sub.add_parser("stop")
    pause = sub.add_parser("pause"); pause.add_argument("--reason", required=True)
    sub.add_parser("resume")
    audit = sub.add_parser("audit"); audit.add_argument("--tail", type=int, default=20)
    verdict = sub.add_parser("verdict"); verdict.add_argument("id")
    verdict.add_argument("decision", choices=("allow", "deny")); verdict.add_argument("--reason", default="")
    return p


def options_from(a):
    extra = []
    if a.extra_allow:
        extra = policy.load_extra(a.extra_allow)
    return {
        "coordinator": a.coordinator or os.environ.get("ORCA_TERMINAL_HANDLE", ""),
        "run": a.run,
        "repo": a.repo,
        "workspace": a.workspace or a.repo,
        "branch": a.branch,
        "skill_dirs": a.skill_dir,
        "extra_allow": extra,
        "extra_allow_file": a.extra_allow,
        "idle_seconds": a.idle_seconds,
        "max_nudges": a.max_nudges,
        "nudge": not a.no_nudge,
        "agent_on_hold": not a.no_agent,
        "agent_cmd": a.agent_cmd,
        "interval": a.interval,
    }


def heartbeat_fresh(control, window=RUNTIME_FRESH):
    stamp = control.get("heartbeat_ts")
    if not stamp:
        return False
    try:
        beat = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    return (datetime.now().astimezone() - beat).total_seconds() < window


def cmd_status(a, guard):
    control = guard.control()
    heartbeat = control.get("heartbeat_ts")
    fresh = heartbeat_fresh(control)
    holds = [data for _, data in guard.pending_holds()]
    open_holds = guard.open_holds()
    payload = {"desired": control.get("desired", "unknown"), "heartbeat": heartbeat, "fresh": fresh,
               "pid": control.get("pid"), "nudges": control.get("nudges", 0),
               "open_holds": len(open_holds), "awaiting_agent": len(holds), "holds": open_holds,
               "audit": str(guard.audit_path)}
    if a.json:
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    print(f"guard {payload['desired']} · heartbeat {heartbeat or '-'} · {'fresh' if fresh else 'stale'}")
    print(f"nudges={payload['nudges']} open_holds={payload['open_holds']} "
          f"awaiting_agent={payload['awaiting_agent']} audit={payload['audit']}")
    for hold in open_holds:
        print(f"  ? {hold['id']} [{hold.get('state')}] {hold.get('label')} {hold.get('tool')} {hold.get('reason')}")
    return 0


def cmd_start(a, guard):
    guard.dir.mkdir(parents=True, exist_ok=True)
    guard.holds_dir.mkdir(parents=True, exist_ok=True)
    guard.verdicts_dir.mkdir(parents=True, exist_ok=True)
    control = guard.control()
    if control.get("desired") == "running" and heartbeat_fresh(control):
        print(json.dumps({"ok": True, "already": True, "pid": control.get("pid"),
                          "state_dir": str(guard.dir)}, ensure_ascii=False))
        return 0
    options = dict(guard.opts)
    options["coordinator"] = options["coordinator"] or os.environ.get("ORCA_TERMINAL_HANDLE", "")
    write_json(guard.dir / "guard.json", {
        "desired": "running", "generation": int(control.get("generation") or 0) + 1,
        "started_at": now_iso(), "options": options, "nudges": 0,
    })
    argv = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--state-dir", str(guard.dir),
            "--coordinator", options["coordinator"], "--run", options["run"], "--repo", options["repo"],
            "--workspace", options["workspace"], "--branch", options["branch"],
            "--idle-seconds", str(options["idle_seconds"]), "--max-nudges", str(options["max_nudges"])]
    if options.get("extra_allow_file"):
        argv.extend(["--extra-allow", options["extra_allow_file"]])
    for skill_dir in options["skill_dirs"]:
        argv.extend(["--skill-dir", skill_dir])
    if not options["nudge"]:
        argv.append("--no-nudge")
    if not options["agent_on_hold"]:
        argv.append("--no-agent")
    argv.append("serve")
    log_path = guard.dir / "guard.log"
    with open(log_path, "a", encoding="utf-8") as log:
        kwargs = {"stdout": log, "stderr": log, "stdin": subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(argv, **kwargs)
    print(json.dumps({"ok": True, "started": True, "pid": proc.pid, "state_dir": str(guard.dir),
                      "log": str(log_path)}, ensure_ascii=False))
    return 0


def cmd_pause(a, guard):
    control = guard.control()
    control["desired"] = "paused"
    control["pause_reason"] = a.reason
    control["paused_at"] = now_iso()
    guard.save_control(control)
    print(f"guard paused：{a.reason}")
    return 0


def cmd_resume(a, guard):
    control = guard.control()
    control["desired"] = "running"
    control.pop("pause_reason", None)
    guard.save_control(control)
    marker = guard.dir / "PAUSED"
    if marker.exists():
        marker.unlink()
    print("guard resumed")
    return 0


def cmd_stop(a, guard):
    control = guard.control()
    control["desired"] = "stopped"
    control["stopped_at"] = now_iso()
    guard.save_control(control)
    print("guard stopped：后台进程在下一轮退出，hold 保留待查")
    return 0


def cmd_audit(a, guard):
    try:
        lines = guard.audit_path.read_text(encoding="utf-8").splitlines()[-a.tail:]
    except OSError:
        print("no audit yet")
        return 0
    print("\n".join(lines))
    return 0


def cmd_verdict(a, guard):
    target = guard.holds_dir / f"{a.id}.json"
    payload = read_json(target, {})
    if not payload:
        print(f"hold {a.id} not found", file=sys.stderr)
        return 1
    payload["state"] = "resolved-by-user" if a.decision == "allow" else "denied-by-user"
    payload["agent_reason"] = a.reason
    write_json(target, payload)
    guard.audit({"event": "verdict", "id": a.id, "decision": a.decision, "by": "cli", "reason": a.reason})
    print(f"{a.id} -> {a.decision}")
    return 0


def loop(guard, interval, with_agent):
    while True:
        try:
            state = guard.tick()
            if with_agent:
                guard.handle_holds()
        except KeyboardInterrupt:
            return 0
        except Exception as exc:  # keep the guard alive; a crash would defeat the purpose
            guard.audit({"event": "error", "error": repr(exc)})
            guard.log(f"[guard] error {exc!r}")
            state = "ran"
        if state == "stopped":
            guard.log("[guard] desired=stopped, exiting")
            return 0
        time.sleep(interval)


def main(argv=None):
    a = build_parser().parse_args(argv)
    guard = Guard(a.state_dir, options_from(a))
    if a.cmd == "status":
        return cmd_status(a, guard)
    if a.cmd == "start":
        return cmd_start(a, guard)
    if a.cmd == "pause":
        return cmd_pause(a, guard)
    if a.cmd == "resume":
        return cmd_resume(a, guard)
    if a.cmd == "stop":
        return cmd_stop(a, guard)
    if a.cmd == "audit":
        return cmd_audit(a, guard)
    if a.cmd == "verdict":
        return cmd_verdict(a, guard)
    if a.cmd == "tick":
        if not (guard.dir / "guard.json").exists():
            guard.save_control({"desired": "running", "started_at": now_iso(),
                                "generation": 1, "options": guard.opts, "nudges": 0})
        guard.dir.mkdir(parents=True, exist_ok=True)
        guard.holds_dir.mkdir(parents=True, exist_ok=True)
        guard.tick()
        return 0
    guard.dir.mkdir(parents=True, exist_ok=True)
    guard.holds_dir.mkdir(parents=True, exist_ok=True)
    guard.verdicts_dir.mkdir(parents=True, exist_ok=True)
    if not guard.opts.get("coordinator") and not guard.opts.get("run"):
        # A guard with nothing to watch is a silent no-op; say so instead.
        print("guard 需要 --coordinator 或 --run 至少一项：没有可观察的终端，守护不会做任何事",
              file=sys.stderr)
        return 2
    if a.cmd == "pane":
        guard.log(f"[guard] pane started · coordinator={guard.opts['coordinator'] or '-'} "
                  f"run={guard.opts['run'] or '-'} state={guard.dir}")
        return loop(guard, a.interval, with_agent=True)
    guard.log(f"[guard] serve started · state={guard.dir}")
    return loop(guard, a.interval, with_agent=False)


if __name__ == "__main__":
    sys.exit(main())
