#!/usr/bin/env python3
"""Guard loop tests against a fake orca CLI. Run: python3 -X utf8 -B orch_guard_test.py"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import orch_guard as guard_mod

DIALOG = [
    "╭──────────────────────────────────────────────╮",
    "│ Bash command                                 │",
    "│                                              │",
    "│   git status --short                         │",
    "│                                              │",
    "│ Do you want to proceed?                      │",
    "│ ❯ 1. Yes                                     │",
    "│   2. Yes, and don't ask again for this cmd   │",
    "│   3. No, tell CodeBuddy what to do (esc)     │",
    "╰──────────────────────────────────────────────╯",
]
IDLE = ["● Bash(git status)", "  └ clean", "✻ Worked for 2s"]

# Captured from a real CodeBuddy confirm dialog (Orca terminal, v2.169.0).
REAL_DIALOG = [
    "╭──────────────────────────────────────────────────────────────────────────╮",
    "│ Confirm                                                                  │",
    "│                                                                          │",
    "│   REPL                                                                   │",
    '│   code: "const r = await Bash({ command: "ls -la" });                    │',
    "│                                                                          │",
    "│ Are you sure you want to do this?                                        │",
    "│                                                                          │",
    "│ > 1. Yes                                                                 │",
    "│   2. Yes, and don't ask again this session (shift + tab)                 │",
    "│   3. No, and tell CodeBuddy what to do differently (escape)              │",
    "│                                                                          │",
    "╰──────────────────────────────────────────────────────────────────────────╯",
]


def transcript(path, calls):
    """calls: [(callId, name, args, has_result)]"""
    lines, now_ms = [], int(time.time() * 1000)
    for call_id, name, args, has_result in calls:
        lines.append(json.dumps({"type": "function_call", "callId": call_id, "name": name,
                                 "arguments": json.dumps(args, ensure_ascii=False),
                                 "timestamp": now_ms}))
        if has_result:
            lines.append(json.dumps({"type": "function_call_result", "callId": call_id,
                                     "name": name, "timestamp": now_ms + 1}))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


class FakeOrca:
    def __init__(self, guard):
        self.guard = guard
        self.calls = []
        self.sent = []

    def __call__(self, argv, timeout=30):
        self.calls.append(list(argv))
        head = argv[0] if argv else ""
        if head == "terminal" and len(argv) > 1 and argv[1] == "show":
            handle = argv[argv.index("--terminal") + 1]
            state = self.guard["terminals"].get(handle)
            if state is None:
                return {"ok": False, "error": {"code": "not_found"}}
            return {"ok": True, "result": {"terminal": {
                "handle": handle, "tabId": state.get("tabId", ""), "leafId": state.get("leafId", ""),
                "connected": True, "agentWait": state["wait"], "lastOutputAt": state.get("lastOutputAt", 0),
            }}}
        if head == "terminal" and len(argv) > 1 and argv[1] == "read":
            handle = argv[argv.index("--terminal") + 1]
            return {"ok": True, "result": {"terminal": {
                "handle": handle, "tail": self.guard["screens"].get(handle, [])}}}
        if head == "terminal" and len(argv) > 1 and argv[1] == "send":
            handle = argv[argv.index("--terminal") + 1]
            text = argv[argv.index("--text") + 1]
            self.sent.append((handle, text))
            self.guard["terminals"][handle]["wait"] = None
            if self.guard.get("dialog_closes", True):
                self.guard["screens"][handle] = IDLE
            return {"ok": True, "result": {}}
        if head == "orchestration" and argv[1] == "worker-list":
            return {"ok": True, "result": {"workers": self.guard.get("workers", [])}}
        return {"ok": False, "error": {"code": "unexpected", "message": " ".join(argv)}}


class GuardCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "guard"
        self.repo = self.root / "repo"
        (self.repo / ".orchestration" / "run_1").mkdir(parents=True)
        (self.repo / "src").mkdir()
        self.coordinator = "term_coord"
        self.now = 1_700_000_000.0
        self.guard = {
            "terminals": {self.coordinator: {"wait": True, "lastOutputAt": int(self.now * 1000),
                                             "tabId": "tab", "leafId": "leaf"}},
            "screens": {self.coordinator: list(DIALOG)},
            "workers": [],
        }
        self.transcript_path = self.root / "session.jsonl"
        transcript(self.transcript_path, [("call_1", "Bash", {"command": "git status"}, False)])
        self.status_file = self.root / "last-status.json"
        self.status_file.write_text(json.dumps({"entries": {
            "tab:leaf": {"providerSession": {"transcriptPath": str(self.transcript_path)}}}}), encoding="utf-8")
        self.fake = FakeOrca(self.guard)

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, **overrides):
        options = {
            "coordinator": self.coordinator, "run": "run_1", "repo": str(self.repo),
            "workspace": str(self.repo), "branch": "feat/x", "skill_dirs": [],
            "extra_allow": [], "idle_seconds": 300, "max_nudges": 3, "nudge": True,
            "agent_on_hold": False, "agent_cmd": "codebuddy", "verify_wait": 0,
            "status_file": str(self.status_file), "interval": 5,
        }
        options.update(overrides)
        return guard_mod.Guard(self.state, options, request=self.fake,
                               clock=lambda: self.now, log=lambda *a: None, sleep=lambda s: None)

    def audit(self):
        path = self.state / "audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def events(self, name):
        return [row for row in self.audit() if row.get("event") == name]


class TestScreenParsing(unittest.TestCase):
    def test_options_parsed(self):
        options = guard_mod.parse_options(DIALOG)
        self.assertEqual([n for n, _ in options], [1, 2, 3])

    def test_pick_plain_yes(self):
        number, text = guard_mod.pick_allow_option(guard_mod.parse_options(DIALOG))
        self.assertEqual(number, 1)
        self.assertTrue(text.startswith("Yes"))

    def test_pick_does_not_select_no(self):
        screen = ["❯ 1. Yes", "  2. No, tell CodeBuddy what to do"]
        self.assertEqual(guard_mod.pick_allow_option(guard_mod.parse_options(screen))[0], 1)

    def test_no_allow_option(self):
        self.assertIsNone(guard_mod.pick_allow_option(guard_mod.parse_options(["1. No", "2. Cancel"])))

    def test_chinese_allow(self):
        screen = ["1. 允许本次执行", "2. 拒绝"]
        self.assertEqual(guard_mod.pick_allow_option(guard_mod.parse_options(screen))[0], 1)

    def test_blank_screen_is_not_a_dialog(self):
        self.assertFalse(guard_mod.looks_like_dialog(["● Bash(x)", "  └ ok"]))

    def test_real_captured_dialog(self):
        options = guard_mod.parse_options(REAL_DIALOG)
        self.assertEqual([n for n, _ in options], [1, 2, 3])
        self.assertEqual(guard_mod.pick_allow_option(options)[0], 1)
        self.assertTrue(guard_mod.decline_option(options))

    def test_numbered_list_without_decline_is_not_confirm(self):
        screen = ["Do you want to proceed?", "1. Yes", "2. Yes, and don't ask again"]
        options = guard_mod.parse_options(screen)
        self.assertIsNotNone(guard_mod.pick_allow_option(options))
        self.assertFalse(guard_mod.decline_option(options))


class TestTranscriptParsing(GuardCase):
    def test_codebuddy_pending_call(self):
        call = guard_mod.pending_tool_call(str(self.transcript_path))
        self.assertEqual(call["tool"], "Bash")
        self.assertEqual(call["input"]["command"], "git status")

    def test_result_clears_pending(self):
        transcript(self.transcript_path, [("call_1", "Bash", {"command": "git status"}, True)])
        self.assertIsNone(guard_mod.pending_tool_call(str(self.transcript_path)))

    def test_latest_pending_wins(self):
        transcript(self.transcript_path, [("a", "Bash", {"command": "git status"}, False),
                                          ("b", "Bash", {"command": "gh auth token"}, False)])
        self.assertEqual(guard_mod.pending_tool_call(str(self.transcript_path))["input"]["command"],
                         "gh auth token")

    def test_claude_format(self):
        path = self.root / "claude.jsonl"
        path.write_text("\n".join([
            json.dumps({"type": "assistant", "timestamp": int(time.time() * 1000),
                        "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                                 "input": {"command": "git status"}}]}}),
        ]), encoding="utf-8")
        call = guard_mod.pending_tool_call(str(path))
        self.assertEqual(call["tool"], "Bash")

    def test_missing_transcript_is_none(self):
        self.assertIsNone(guard_mod.pending_tool_call(""))


class TestAllowPath(GuardCase):
    def test_allowed_command_is_pressed(self):
        guard = self.make()
        guard.tick()
        self.assertEqual(self.fake.sent, [(self.coordinator, "1")])
        applied = self.events("applied")
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["tool"], "Bash")
        self.assertEqual(applied[0]["option"], 1)

    def test_audit_records_prompt_then_apply(self):
        guard = self.make()
        guard.tick()
        self.assertEqual(len(self.events("prompt")), 1)

    def test_second_tick_does_not_press_again(self):
        guard = self.make()
        guard.tick()
        self.guard["terminals"][self.coordinator]["wait"] = True
        self.guard["screens"][self.coordinator] = list(DIALOG)
        transcript(self.transcript_path, [("call_1", "Bash", {"command": "git status"}, False)])
        guard.tick()
        self.assertEqual(len(self.fake.sent), 1)

    def test_cooldown_expires_after_window(self):
        guard = self.make()
        guard.tick()
        self.now += guard_mod.PROMPT_COOLDOWN + 1
        self.guard["terminals"][self.coordinator]["wait"] = True
        self.guard["screens"][self.coordinator] = list(DIALOG)
        transcript(self.transcript_path, [("call_2", "Bash", {"command": "git status"}, False)])
        guard.tick()
        self.assertEqual(len(self.fake.sent), 2, "a fresh identical prompt after cooldown is pressed again")


class TestHoldPath(GuardCase):
    def test_gh_auth_holds_without_pressing(self):
        transcript(self.transcript_path, [("call_1", "Bash", {"command": "gh auth token"}, False)])
        guard = self.make()
        guard.tick()
        self.assertEqual(self.fake.sent, [])
        holds = list((self.state / "holds").glob("*.json"))
        self.assertEqual(len(holds), 1)
        payload = json.loads(holds[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["rule"], "gh.auth")
        self.assertEqual(payload["state"], "open")

    def test_unknown_tool_holds(self):
        transcript(self.transcript_path, [("call_1", "mcp__evil__do", {}, False)])
        guard = self.make()
        guard.tick()
        self.assertEqual(self.fake.sent, [])
        self.assertEqual(len(list((self.state / "holds").glob("*.json"))), 1)

    def test_no_transcript_holds(self):
        self.status_file.write_text("{}", encoding="utf-8")
        guard = self.make()
        guard.tick()
        self.assertEqual(self.fake.sent, [])
        payload = json.loads(next((self.state / "holds").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(payload["rule"], "unknown-tool")

    def test_unparsable_dialog_holds(self):
        self.guard["screens"][self.coordinator] = ["Do you want to proceed? (y/n)"]
        guard = self.make()
        guard.tick()
        self.assertEqual(self.fake.sent, [])
        self.assertTrue(any("无法确认" in row.get("reason", "") for row in self.audit()))

    def test_verify_failure_holds(self):
        self.guard["dialog_closes"] = False
        guard = self.make()
        guard.tick()
        self.assertEqual(len(self.fake.sent), 1)
        self.assertEqual(len(self.events("verify_failed")), 1)
        payload = json.loads(next((self.state / "holds").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(payload["state"], "open")

    def test_non_dialog_screen_holds(self):
        """agentWait can be momentary; a running tool must not be treated as a prompt."""
        self.guard["screens"][self.coordinator] = IDLE
        guard = self.make()
        guard.tick()
        self.assertEqual(self.fake.sent, [])
        self.assertEqual(list((self.state / "holds").glob("*.json")), [])

    def test_numbered_list_without_decline_holds(self):
        self.guard["screens"][self.coordinator] = [
            "Do you want to proceed?", "1. Yes", "2. Yes, and don't ask again"]
        guard = self.make()
        guard.tick()
        self.assertEqual(self.fake.sent, [])
        self.assertEqual(len(list((self.state / "holds").glob("*.json"))), 1)


class TestPauseAndStop(GuardCase):
    def test_pause_marker_blocks_injection(self):
        (self.state).mkdir(parents=True)
        (self.state / "PAUSED").write_text("", encoding="utf-8")
        guard = self.make()
        guard.tick()
        self.assertEqual(self.fake.sent, [])
        self.assertTrue(all(row.get("paused") for row in self.events("prompt")))

    def test_pause_reason_in_control(self):
        guard = self.make()
        guard.save_control({"desired": "paused", "pause_reason": "用户暂停"})
        guard.tick()
        self.assertEqual(self.fake.sent, [])

    def test_stop_returns_stopped(self):
        guard = self.make()
        guard.save_control({"desired": "stopped"})
        self.assertEqual(guard.tick(), "stopped")

    def test_resume_clears_marker(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "PAUSED").write_text("", encoding="utf-8")
        guard = self.make()
        args = guard_mod.build_parser().parse_args(["--state-dir", str(self.state), "resume"])
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            self.assertEqual(guard_mod.cmd_resume(args, guard), 0)
        self.assertFalse((self.state / "PAUSED").exists())


class TestNudge(GuardCase):
    def setUp(self):
        super().setUp()
        state = {"run": "run_1", "issues": {"1": {"stage": "dev"}, "2": {"stage": "closed"}}}
        (self.repo / ".orchestration" / "run_1" / "state.json").write_text(
            json.dumps(state), encoding="utf-8")
        self.guard["terminals"][self.coordinator].update({"wait": None,
                                                         "lastOutputAt": int((self.now - 400) * 1000)})
        self.guard["screens"][self.coordinator] = IDLE
        transcript(self.transcript_path, [("call_0", "Bash", {"command": "git status"}, True)])

    def test_nudge_when_idle_and_unfinished(self):
        guard = self.make()
        guard.tick()
        self.assertEqual(len(self.fake.sent), 1)
        self.assertIn("继续推进编排", self.fake.sent[0][1])

    def test_no_nudge_with_pending_call(self):
        transcript(self.transcript_path, [("call_1", "Bash", {"command": "sleep 600"}, False)])
        guard = self.make()
        guard.tick()
        self.assertEqual(self.fake.sent, [])

    def test_no_nudge_when_recent_output(self):
        self.guard["terminals"][self.coordinator]["lastOutputAt"] = int((self.now - 10) * 1000)
        guard = self.make()
        guard.tick()
        self.assertEqual(self.fake.sent, [])

    def test_no_nudge_when_all_closed(self):
        (self.repo / ".orchestration" / "run_1" / "state.json").write_text(
            json.dumps({"run": "run_1", "issues": {"1": {"stage": "closed"}}}), encoding="utf-8")
        guard = self.make()
        guard.tick()
        self.assertEqual(self.fake.sent, [])

    def test_no_nudge_while_a_worker_is_running(self):
        """A coordinator parked on worker_done is correct; nudging it fights the wait protocol."""
        self.guard["workers"] = [{"dispatchId": "ctx_1", "dispatchStatus": "running",
                                  "agentTerminalHandle": "term_worker"}]
        self.guard["terminals"]["term_worker"] = {"wait": None, "lastOutputAt": int(self.now * 1000)}
        guard = self.make()
        guard.tick()
        self.assertEqual(self.fake.sent, [])

    def test_no_nudge_when_waiting_on_the_user(self):
        (self.repo / ".orchestration" / "run_1" / "state.json").write_text(json.dumps({
            "run": "run_1",
            "issues": {"1": {"stage": "dev"}, "2": {"stage": "user_gate"}}}), encoding="utf-8")
        guard = self.make()
        guard.tick()
        self.assertEqual(self.fake.sent, [])

    def test_no_nudge_with_an_open_question(self):
        (self.repo / ".orchestration" / "run_1" / "state.json").write_text(json.dumps({
            "run": "run_1", "questions": [{"text": "验收口径?"}],
            "issues": {"1": {"stage": "dev"}}}), encoding="utf-8")
        guard = self.make()
        guard.tick()
        self.assertEqual(self.fake.sent, [])

    def test_no_nudge_after_finish(self):
        (self.repo / ".orchestration" / "run_1" / "state.json").write_text(json.dumps({
            "run": "run_1", "finished_at": "2026-10-20T12:00:00+08:00",
            "issues": {"1": {"stage": "dev"}}}), encoding="utf-8")
        guard = self.make()
        guard.tick()
        self.assertEqual(self.fake.sent, [])

    def test_no_nudge_while_paused(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "PAUSED").write_text("", encoding="utf-8")
        guard = self.make()
        guard.tick()
        self.assertEqual(self.fake.sent, [])

    def test_nudge_capped(self):
        guard = self.make()
        for _ in range(5):
            guard.tick()
        self.assertEqual(len(self.fake.sent), 3)

    def test_no_nudge_flag(self):
        guard = self.make(nudge=False)
        guard.tick()
        self.assertEqual(self.fake.sent, [])


class TestWorkerTargets(GuardCase):
    def test_worker_prompt_is_guarded(self):
        worker = "term_worker"
        self.guard["workers"] = [{"dispatchId": "ctx_1", "dispatchStatus": "running",
                                  "agentTerminalHandle": worker}]
        self.guard["terminals"][worker] = {"wait": True, "lastOutputAt": 0, "tabId": "w", "leafId": "l"}
        self.guard["screens"][worker] = list(DIALOG)
        self.status_file.write_text(json.dumps({"entries": {
            "tab:leaf": {"providerSession": {"transcriptPath": str(self.transcript_path)}},
            "w:l": {"providerSession": {"transcriptPath": str(self.transcript_path)}}}}), encoding="utf-8")
        guard = self.make()
        guard.tick()
        handles = [handle for handle, _ in self.fake.sent]
        self.assertEqual(handles, [self.coordinator, worker])

    def test_settled_worker_skipped(self):
        self.guard["workers"] = [{"dispatchId": "ctx_1", "dispatchStatus": "completed",
                                  "agentTerminalHandle": "term_done"}]
        guard = self.make()
        guard.tick()
        self.assertNotIn("term_done", [handle for handle, _ in self.fake.sent])


class TestVerdict(GuardCase):
    def test_agent_allow_applies_prompt(self):
        transcript(self.transcript_path, [("call_1", "Bash", {"command": "gh auth token"}, False)])
        guard = self.make()
        guard.tick()
        hold = json.loads(next((self.state / "holds").glob("*.json")).read_text(encoding="utf-8"))
        (self.state / "verdicts").mkdir(parents=True, exist_ok=True)
        (self.state / "verdicts" / f"{hold['id']}.json").write_text(
            json.dumps({"id": hold["id"], "decision": "allow", "reason": "本次只用 gh auth status"}),
            encoding="utf-8")
        guard.collect_verdicts()
        self.assertEqual(self.fake.sent, [(self.coordinator, "1")])
        payload = json.loads(next((self.state / "holds").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(payload["state"], "resolved-by-agent")

    def test_user_verdict_is_pressed_on_next_tick(self):
        transcript(self.transcript_path, [("call_1", "Bash", {"command": "gh auth token"}, False)])
        guard = self.make()
        guard.tick()
        self.assertEqual(self.fake.sent, [])
        hold = json.loads(next((self.state / "holds").glob("*.json")).read_text(encoding="utf-8"))
        args = guard_mod.build_parser().parse_args(
            ["--state-dir", str(self.state), "verdict", hold["id"], "allow", "--reason", "流程内推送"])
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            guard_mod.cmd_verdict(args, guard)
        guard.tick()
        self.assertEqual(self.fake.sent, [(self.coordinator, "1")])
        payload = json.loads(next((self.state / "holds").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(payload["state"], "resolved-by-user")

    def test_user_verdict_waits_for_pause(self):
        (self.state).mkdir(parents=True)
        (self.state / "PAUSED").write_text("", encoding="utf-8")
        transcript(self.transcript_path, [("call_1", "Bash", {"command": "gh auth token"}, False)])
        guard = self.make()
        hold = {"id": "h1", "state": "resolved-by-user", "terminal": self.coordinator,
                "label": "coordinator", "tool": "Bash", "input": {"command": "gh auth token"}}
        (self.state / "holds").mkdir(parents=True, exist_ok=True)
        (self.state / "holds" / "h1.json").write_text(json.dumps(hold), encoding="utf-8")
        guard.tick()
        self.assertEqual(self.fake.sent, [])

    def test_agent_deny_keeps_for_human(self):
        transcript(self.transcript_path, [("call_1", "Bash", {"command": "gh auth token"}, False)])
        guard = self.make()
        guard.tick()
        hold = json.loads(next((self.state / "holds").glob("*.json")).read_text(encoding="utf-8"))
        (self.state / "verdicts").mkdir(parents=True, exist_ok=True)
        (self.state / "verdicts" / f"{hold['id']}.json").write_text(
            json.dumps({"id": hold["id"], "decision": "deny", "reason": "涉及凭据"}), encoding="utf-8")
        guard.collect_verdicts()
        self.assertEqual(self.fake.sent, [])
        self.assertEqual(len(guard.open_holds()), 1)

    def test_agent_allow_but_unparsable_dialog(self):
        transcript(self.transcript_path, [("call_1", "Bash", {"command": "gh auth token"}, False)])
        self.guard["screens"][self.coordinator] = ["Do you want to proceed? (y/n)"]
        guard = self.make()
        guard.tick()
        hold = json.loads(next((self.state / "holds").glob("*.json")).read_text(encoding="utf-8"))
        (self.state / "verdicts").mkdir(parents=True, exist_ok=True)
        (self.state / "verdicts" / f"{hold['id']}.json").write_text(
            json.dumps({"id": hold["id"], "decision": "allow"}), encoding="utf-8")
        guard.collect_verdicts()
        self.assertEqual(self.fake.sent, [])
        payload = json.loads(next((self.state / "holds").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(payload["state"], "apply-failed")

    def test_cli_verdict(self):
        transcript(self.transcript_path, [("call_1", "Bash", {"command": "gh auth token"}, False)])
        guard = self.make()
        guard.tick()
        hold = json.loads(next((self.state / "holds").glob("*.json")).read_text(encoding="utf-8"))
        args = guard_mod.build_parser().parse_args(
            ["--state-dir", str(self.state), "verdict", hold["id"], "deny", "--reason", "人工拒绝"])
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            self.assertEqual(guard_mod.cmd_verdict(args, guard), 0)
        payload = json.loads(next((self.state / "holds").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(payload["state"], "denied-by-user")


class TestStatus(GuardCase):
    def test_status_json(self):
        guard = self.make()
        guard.save_control({"desired": "running", "heartbeat_ts": guard_mod.now_iso(), "pid": 1})
        args = guard_mod.build_parser().parse_args(["--state-dir", str(self.state), "status", "--json"])
        import io
        from contextlib import redirect_stdout
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            guard_mod.cmd_status(args, guard)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["desired"], "running")
        self.assertTrue(payload["fresh"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
