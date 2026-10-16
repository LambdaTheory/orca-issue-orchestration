#!/usr/bin/env python3
"""Fake-orca coverage for orch_wait: liveness stays in the shell, real mail exits."""
import json, os, stat, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import orch_wait


def env(result, ok=True):
    return {"ok": ok, "result": result}


def heartbeat(subject="alive", body="", delivery="delivery_hb"):
    return env({"deliveryId": delivery, "messages": [
        {"type": "heartbeat", "subject": subject, "body": body, "from_handle": "term_w"}
    ], "count": 1, "timedOut": False})


class Decide(unittest.TestCase):
    def test_alive_empty_body_is_liveness(self):
        self.assertTrue(orch_wait.is_liveness({"type": "heartbeat", "subject": "alive", "body": ""}))
        self.assertTrue(orch_wait.is_liveness({"type": "heartbeat", "subject": " Alive ", "body": None}))

    def test_progress_stuffed_into_heartbeat_is_not_liveness(self):
        self.assertFalse(orch_wait.is_liveness({"type": "heartbeat", "subject": "实现口径与索引", "body": "要加索引"}))
        self.assertFalse(orch_wait.is_liveness({"type": "heartbeat", "subject": "alive", "body": "卡在迁移"}))

    def test_batch_of_only_liveness_is_swallowed(self):
        self.assertEqual(orch_wait.batch_action(heartbeat()["result"]), "swallow")

    def test_mixed_batch_is_surfaced_without_being_swallowable(self):
        result = {"deliveryId": "d1", "messages": [
            {"type": "heartbeat", "subject": "alive", "body": ""},
            {"type": "question", "subject": "口径", "body": "算不算完成"},
        ]}
        self.assertEqual(orch_wait.batch_action(result), "surface")

    def test_liveness_without_delivery_id_is_surfaced(self):
        result = {"deliveryId": None, "messages": [{"type": "heartbeat", "subject": "alive", "body": ""}]}
        self.assertEqual(orch_wait.batch_action(result), "surface")


class Loop(unittest.TestCase):
    def test_three_alive_heartbeats_then_worker_done(self):
        script = [
            heartbeat(delivery="d1"),
            heartbeat(delivery="d2"),
            heartbeat(delivery="d3"),
            env({"deliveryId": "d4", "messages": [
                {"type": "worker_done", "subject": "done", "body": "pr open"}
            ], "timedOut": False}),
        ]
        acks = []

        def check(ack):
            acks.append(ack)
            return script.pop(0)

        envelope, swallowed, code = orch_wait.run_loop(check)
        self.assertEqual(code, 0)
        self.assertEqual(swallowed, 3)
        self.assertEqual(acks, [None, "d1", "d2", "d3"])
        self.assertEqual(envelope["result"]["messages"][0]["type"], "worker_done")
        self.assertEqual(script, [])

    def test_progress_heartbeat_exits_without_ack(self):
        calls = []

        def check(ack):
            calls.append(ack)
            return heartbeat(subject="实现口径与索引", body="补索引", delivery="d9")

        envelope, swallowed, code = orch_wait.run_loop(check)
        self.assertEqual((swallowed, code, calls), (0, 0, [None]))
        self.assertEqual(envelope["result"]["deliveryId"], "d9")

    def test_many_timeouts_keep_waiting_until_worker_done(self):
        done = env({"deliveryId": "done", "messages": [{"type": "worker_done"}]})
        seq = [env({"messages": [], "timedOut": True})] * 8 + [done]
        acks = []

        def check(ack):
            acks.append(ack)
            return seq.pop(0)

        envelope, swallowed, code = orch_wait.run_loop(check)
        self.assertEqual((envelope, swallowed, code, seq), (done, 0, 0, []))
        self.assertEqual(acks, [None] * 9)

    def test_timeouts_and_heartbeat_keep_waiting_until_real_message(self):
        seq = [
            env({"deliveryId": None, "messages": [], "timedOut": True}),
            env({"deliveryId": None, "messages": [], "timedOut": True}),
            heartbeat(delivery="d1"),
            env({"deliveryId": None, "messages": [], "timedOut": True}),
            env({"deliveryId": None, "messages": [], "timedOut": True}),
            env({"deliveryId": None, "messages": [], "timedOut": True}),
            env({"deliveryId": "done", "messages": [{"type": "worker_done"}]}),
        ]
        acks = []

        def check(ack):
            acks.append(ack)
            return seq.pop(0)

        result, swallowed, code = orch_wait.run_loop(check)
        self.assertEqual((swallowed, code, seq), (1, 0, []))
        self.assertEqual(result["result"]["deliveryId"], "done")
        self.assertEqual(acks, [None, None, None, "d1", None, None, None])

    def test_same_delivery_twice_is_surfaced(self):
        def check(ack):
            return heartbeat(delivery="same")

        envelope, swallowed, code = orch_wait.run_loop(check)
        self.assertEqual(code, 0)
        self.assertEqual(swallowed, 1)
        self.assertEqual(envelope["result"]["deliveryId"], "same")

    def test_waiter_exists_exits_immediately(self):
        def check(ack):
            return {"ok": False, "error": {"code": "waiter_exists", "message": "already waiting"}}

        envelope, swallowed, code = orch_wait.run_loop(check)
        self.assertEqual(code, 1)
        self.assertEqual(envelope["error"]["code"], "waiter_exists")
        self.assertEqual(swallowed, 0)


FAKE_ORCA = r'''#!/usr/bin/env python3
import json, os, sys
log = os.environ["ORCH_FAKE_LOG"]
plan = json.loads(os.environ["ORCH_FAKE_PLAN"])
if "worker-list" in sys.argv:
    print(json.dumps({"ok": True, "result": {"workers": [{"runId": "run_test", "dispatchId": "ctx_test", "taskId": "task_test", "dispatchStatus": "dispatched", "terminalState": "active"}]}}))
    sys.exit(0)
if "worker-show" in sys.argv:
    print(json.dumps({"ok": True, "result": {"dispatch": {"id": "ctx_test", "runId": "run_test", "status": "dispatched"}, "projection": {"stage": {"activity": "working"}, "liveness": {"verdict": "live"}}, "observation": {"agentWait": None}}}))
    sys.exit(0)
with open(log, "r", encoding="utf-8") as f:
    n = sum(1 for line in f if line.strip())
with open(log, "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv) + "\n")
item = plan[min(n, len(plan) - 1)]
sys.stderr.write('{"_keepalive":true}\n')
sys.stdout.write(json.dumps(item["stdout"], ensure_ascii=False))
sys.exit(item.get("exit", 0))
'''


def make_fake_orca(d, name="orca"):
    """Create a fake orca launcher, using a .cmd wrapper on Windows."""
    d = Path(d)
    if os.name == "nt":
        script = d / (name + ".py")
        script.write_text(FAKE_ORCA, encoding="utf-8")
        wrapper = d / (name + ".cmd")
        wrapper.write_text('@"%ORCH_FAKE_PYTHON%" -X utf8 "%~dp0' + name + '.py" %*\n',
                           encoding="ascii")
        return wrapper
    exe = d / name
    exe.write_text(FAKE_ORCA, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


def run_orch_wait(d, extra_args, plan):
    d = Path(d)
    log = d / "log"
    log.write_text("", encoding="utf-8")
    child_env = os.environ.copy()
    child_env["ORCH_FAKE_PYTHON"] = sys.executable
    child_env["ORCH_FAKE_LOG"] = str(log)
    child_env["ORCH_FAKE_PLAN"] = json.dumps([{"stdout": p} for p in plan],
                                             ensure_ascii=False)
    return log, subprocess.run(
        [sys.executable, "-X", "utf8", "-B",
         str(Path(__file__).parent / "orch_wait.py"),
         "--run", "run_test", "--timeout-ms", "1000"] + extra_args,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", env=child_env)


class Cli(unittest.TestCase):
    def test_cli_acks_liveness_and_prints_only_the_real_batch(self):
        plan = [
            heartbeat(delivery="d1"),
            heartbeat(delivery="d2"),
            env({"deliveryId": "d3", "messages": [
                {"type": "escalation", "subject": "Blocked: ci", "body": "root pin"}
            ], "timedOut": False}),
        ]
        with tempfile.TemporaryDirectory(prefix="中文 orch_wait_") as d:
            fake = make_fake_orca(d)
            log, proc = run_orch_wait(d, ["--orca", str(fake)], plan)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["result"]["messages"][0]["type"], "escalation")
            self.assertIn("swallowed 2 liveness heartbeat(s)", proc.stderr)
            self.assertNotIn("_keepalive", proc.stdout)
            argv = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(argv), 3)
            self.assertIn("heartbeat", argv[0][argv[0].index("--types") + 1])
            self.assertNotIn("--ack", argv[0])
            self.assertEqual(argv[1][argv[1].index("--ack") + 1], "d1")
            self.assertEqual(argv[2][argv[2].index("--ack") + 1], "d2")

    def test_cli_passes_initial_ack_on_the_first_call(self):
        plan = [env({"messages": [], "timedOut": True}),
                heartbeat(delivery="hb"),
                env({"messages": [], "timedOut": True}),
                env({"deliveryId": "done", "messages": [{"type": "worker_done"}]})]
        with tempfile.TemporaryDirectory(prefix="中文 orch_wait_") as d:
            fake = make_fake_orca(d)
            log, proc = run_orch_wait(
                d, ["--orca", str(fake), "--ack", "delivery_done"], plan)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            acks = [argv[argv.index("--ack") + 1] if "--ack" in argv else None for argv in calls]
            self.assertEqual(acks, ["delivery_done", None, "hb", None])
            self.assertEqual(json.loads(proc.stdout)["result"]["deliveryId"], "done")

    def test_cli_roundtrips_utf8_chinese_payload(self):
        plan = [env({"deliveryId": "dz", "messages": [
            {"type": "worker_done", "subject": "完成：口径与索引", "body": "交付说明，含中文标点。【】"}
        ], "timedOut": False})]
        with tempfile.TemporaryDirectory(prefix="中文 orch_wait_") as d:
            fake = make_fake_orca(d)
            _, proc = run_orch_wait(d, ["--orca", str(fake)], plan)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["result"]["messages"][0]["body"],
                             "交付说明，含中文标点。【】")

    def test_nonzero_exit_with_json_stays_a_failure(self):
        for payload in (heartbeat(), {"ok": False, "error": {"code": "unavailable"}}):
            with self.subTest(payload=payload):
                process = subprocess.CompletedProcess([], 1, json.dumps(payload), "service failed")
                with patch.object(orch_wait.subprocess, "run", return_value=process):
                    result = orch_wait.orca_check("orca", "r", 100, "", None)
                self.assertIs(result["ok"], False)
                self.assertEqual(result["error"]["code"],
                                 "unavailable" if payload.get("ok") is False else "check_failed")

    def test_missing_orca_returns_check_failed_without_traceback(self):
        check_failed = orch_wait.orca_check("no_such_orca_不存在", "run_x", 100, "", None)
        self.assertIs(check_failed["ok"], False)
        self.assertEqual(check_failed["error"]["code"], "check_failed")
        self.assertTrue(check_failed["error"]["message"])
        with tempfile.TemporaryDirectory(prefix="中文 orch_wait_") as d:
            log, proc = run_orch_wait(
                d, ["--orca", str(Path(d) / "也没有这个orca")],
                [env({"deliveryId": None, "messages": [], "timedOut": True})])
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(json.loads(proc.stdout)["error"]["code"], "check_failed")
            self.assertNotIn("Traceback", proc.stderr)


class Health(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.row = {"runId": "r", "dispatchId": "d", "taskId": "t",
                    "dispatchStatus": "dispatched", "terminalState": "active"}
        self.detail = {"dispatch": {"id": "d", "runId": "r", "taskId": "t", "status": "dispatched"},
                       "projection": {"stage": {"activity": "working", "worker": "ready"},
                                      "liveness": {"verdict": "live"}},
                       "observation": {"agentWait": None}, "terminal": {"lastOutputAt": 1}}
        self.calls = []
        self.health = orch_wait.WorkerHealth("orca", "r", clock=lambda: self.now,
                                             request=self.request)

    def request(self, cmd, timeout):
        self.calls.append(cmd)
        self.assertGreater(timeout, 0)
        if "worker-list" in cmd:
            self.assertIn("--include-remote", cmd)
            self.assertEqual(cmd[cmd.index("--run") + 1], "r")
            return env({"workers": [self.row], "scope": {"run": "r"}})
        self.assertIn("worker-show", cmd)
        return env(self.detail)

    def reason(self):
        result = self.health.probe()
        self.assertEqual(result["kind"], "health_alert")
        self.assertNotIn("deliveryId", result)
        return result["alerts"][0]["reason"]

    def test_done_live_requires_grace_not_heartbeat_refresh(self):
        self.detail["projection"]["stage"]["activity"] = "done"
        self.assertIsNone(self.health.probe())
        self.now = 60
        self.detail["dispatch"]["lastHeartbeatAt"] = "new"
        self.detail["projection"]["liveness"]["observedAt"] = 60
        self.assertIsNone(self.health.probe())
        self.now = 120
        self.assertEqual(self.reason(), "turn_ended_without_report")

    def test_real_progress_resets_stale_deadline(self):
        self.assertIsNone(self.health.probe())
        self.now = 899
        self.assertIsNone(self.health.probe())
        self.detail["terminal"]["lastOutputAt"] = 2
        self.now = 900
        self.assertIsNone(self.health.probe())
        self.now = 1799
        self.assertIsNone(self.health.probe())
        self.now = 1800
        self.assertEqual(self.reason(), "suspected_stall")

    def test_wait_unknown_exit_and_settlement_are_distinct(self):
        self.detail["observation"]["agentWait"] = {"reason": "permission"}
        self.assertEqual(self.reason(), "needs_input")
        del self.detail["observation"]["agentWait"]
        self.detail["projection"]["liveness"]["verdict"] = "unverifiable"
        self.assertEqual(self.reason(), "worker_unverifiable")
        self.detail["projection"]["liveness"]["verdict"] = "exited"
        self.assertEqual(self.reason(), "worker_exited")
        self.detail["dispatch"]["status"] = "completed"
        self.assertEqual(self.reason(), "settlement_pending")

    def test_released_history_does_not_get_inspected(self):
        self.row.update(dispatchStatus="completed", terminalState="released")
        self.assertEqual(self.reason(), "no_active_workers")
        self.assertEqual(len(self.calls), 1)

    def test_missing_worker_is_unknown_not_dead(self):
        self.assertIsNone(self.health.probe())
        self.health.request = lambda cmd, timeout: env({"workers": []})
        self.now = 60
        self.assertEqual(self.reason(), "worker_missing")

    def test_pagination_finds_active_worker_and_rejects_cycle(self):
        def pages(cmd, timeout):
            if "worker-show" in cmd:
                return env(self.detail)
            if "--cursor" not in cmd:
                return env({"workers": [], "page": {"hasMore": True, "nextCursor": "p2"}})
            return env({"workers": [self.row], "page": {"hasMore": False}})
        self.health.request = pages
        self.assertIsNone(self.health.probe())
        self.assertIn("d", self.health.observed)
        self.health.request = lambda cmd, timeout: env({"workers": [], "page": {"hasMore": True, "nextCursor": "p2"}})
        self.assertEqual(self.reason(), "health_probe_failed")

    def test_wrong_run_detail_is_not_used(self):
        self.detail["dispatch"]["runId"] = "another_run"
        self.assertEqual(self.reason(), "health_probe_failed")

    def test_slow_query_budget_is_reported_not_silent(self):
        def slow_list(cmd, timeout):
            self.now += 61
            return env({"workers": [self.row]})
        self.health.request = slow_list
        self.assertEqual(self.reason(), "health_probe_failed")

    def test_probe_error_is_surfaced(self):
        self.health.request = lambda cmd, timeout: {"ok": False, "error": {"code": "offline"}}
        result = self.health.probe()
        self.assertEqual(result["alerts"][0]["reason"], "health_probe_failed")
        self.assertEqual(result["alerts"][0]["evidence"]["response"]["error"]["code"], "offline")

    def test_heartbeat_stream_does_not_starve_health_and_preserves_ack(self):
        self.detail["projection"]["stage"]["activity"] = "done"
        acks, drains = [], []
        def check(ack):
            acks.append(ack)
            self.now += 30
            return heartbeat(delivery=f"hb{self.now}")
        def drain(ack):
            drains.append(ack)
            return heartbeat(delivery="last_hb")
        result, swallowed, code = orch_wait.run_loop(check, "initial", self.health, drain)
        self.assertEqual(code, 0)
        self.assertEqual(result["kind"], "health_alert")
        self.assertEqual(result["ack_pending"], "last_hb")
        self.assertEqual(acks, ["initial", "hb30", "hb60", "hb90"])
        self.assertEqual(drains, ["hb120"])
        self.assertEqual(swallowed, 5)

    def test_real_delivery_wins_alert_race(self):
        self.health.request = lambda cmd, timeout: env({"workers": []})
        mail = env({"deliveryId": "complete", "messages": [{"type": "worker_done"}]})
        def no_wait(ack):
            self.assertEqual(ack, "initial")
            return mail
        result, _, code = orch_wait.run_loop(lambda ack: self.fail("must not long wait"),
                                             "initial", self.health, no_wait)
        self.assertEqual((result, code), (mail, 0))

    def test_failed_ack_check_keeps_pending_id(self):
        self.health.request = lambda cmd, timeout: env({"workers": []})
        result, _, code = orch_wait.run_loop(lambda ack: None, "initial", self.health,
            lambda ack: {"ok": False, "error": {"code": "cli_timeout"}})
        self.assertEqual(code, 1)
        self.assertEqual(result["ack_pending"], "initial")

    def test_wait_budget_and_cli_timeout(self):
        self.assertIsNone(self.health.probe())
        self.now = 30
        self.assertEqual(self.health.wait_ms(1800000), 30000)
        with patch.object(orch_wait.subprocess, "run", side_effect=subprocess.TimeoutExpired("orca", 30)):
            result = orch_wait.orca_check("orca", "r", 1000, "", None)
        self.assertEqual(result["error"]["code"], "cli_timeout")


if __name__ == "__main__":
    unittest.main()
