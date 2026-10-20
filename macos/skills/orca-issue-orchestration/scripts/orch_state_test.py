#!/usr/bin/env python3
"""Stdlib-only unittest for orch_state.py init --reviewers and reviewer rotation."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CLI = Path(__file__).with_name("orch_state.py")


class OrchStateTest(unittest.TestCase):
    def run_cli(self, repo, *args, env_extra=None):
        env = dict(os.environ, PYTHONUTF8="1")
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable, str(CLI), "--repo", str(repo), "--run", "r1", *args],
            capture_output=True, text=True, encoding="utf-8", env=env,
        )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="orch 中文 path ")
        self.repo = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def orch_dir(self):
        return self.repo / ".orchestration" / "r1"

    def state(self):
        return json.loads((self.orch_dir() / "state.json").read_text(encoding="utf-8"))

    def reviewer(self):
        return self.run_cli(self.repo, "reviewer").stdout.strip()

    def test_init_ignores_state_with_missing_or_existing_gitignore(self):
        for initial in (None, b"# existing", b"# .orchestration/ is local\n",
                        b"/.orchestration/\n!/.orchestration/\n",
                        b"/.orchestration/\n", b"# keep CRLF\r\n*.log\r\n"):
            with self.subTest(initial=initial), tempfile.TemporaryDirectory() as directory:
                repo = Path(directory)
                subprocess.run(["git", "init", "--quiet", str(repo)], check=True,
                               capture_output=True)
                ignore = repo / ".gitignore"
                if initial is not None:
                    ignore.write_bytes(initial)
                result = self.run_cli(repo, "init", "--objective", "obj", "--reviewers", "a")
                self.assertEqual(result.returncode, 0, result.stderr)
                if initial is not None:
                    self.assertTrue(ignore.read_bytes().startswith(initial))
                check = subprocess.run(
                    ["git", "-C", str(repo), "check-ignore", ".orchestration/r1/state.json",
                     ".orchestration/r1/events.md"], capture_output=True, text=True)
                self.assertEqual(check.returncode, 0, check.stderr)
                self.assertEqual(len(check.stdout.splitlines()), 2)
                before = ignore.read_bytes()
                result = subprocess.run(
                    [sys.executable, str(CLI), "--repo", str(repo), "--run", "r2", "init",
                     "--objective", "obj", "--reviewers", "a"], capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(ignore.read_bytes(), before)

    def test_custom_reviewers_saved_verbatim(self):
        r = self.run_cli(self.repo, "init", "--objective", "obj", "--reviewers", "reviewer-a", "reviewer-b")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.state()["settings"]["reviewers"], ["reviewer-a", "reviewer-b"])

    def test_no_implicit_model_values(self):
        r = self.run_cli(self.repo, "init", "--objective", "obj", "--reviewers", "custom-reviewer")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.state()["settings"]["reviewers"], ["custom-reviewer"])
        self.assertEqual(self.state()["settings"]["max_parallel"], 1)

    def test_explicit_parallel_limit_is_preserved(self):
        r = self.run_cli(self.repo, "init", "--objective", "obj", "--reviewers", "a", "--max-parallel", "2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.state()["settings"]["max_parallel"], 2)

    def test_summary_details_and_full_keep_history_intact(self):
        commands = [
            ("init", "--objective", "obj", "--reviewers", "a"),
            ("set", "1", "stage=review", "title=当前任务", "review_head=abc123", "worker_state=ready", "next_action=等待评审", "receipt=long-receipt"),
            ("set", "2", "stage=merged", "title=已合并", "checkpoint=交付完成"),
            ("set", "3", "stage=closed", "title=已关闭"),
            ("set", "4", "stage=verify", "delivery=pr_delivered", "delivery_sha=def456", "pr=9"),
            ("ask", "请验收"), ("rule", "既有边界"),
        ] + [("note", f"历史备注-{i}") for i in range(8)]
        for command in commands:
            result = self.run_cli(self.repo, *command)
            self.assertEqual(result.returncode, 0, result.stderr)
        paths = [self.orch_dir() / name for name in ("state.json", "events.md")]
        before = [path.read_bytes() for path in paths]
        summary = self.run_cli(self.repo, "show")
        self.assertEqual(summary.returncode, 0, summary.stderr)
        for text in ("active_issues=1", "#1", "abc123", "等待评审", "请验收", "既有边界", "待合并", "def456"):
            self.assertIn(text, summary.stdout)
        for text in ("#2", "#3", "long-receipt", "历史备注-0", "历史备注-4"):
            self.assertNotIn(text, summary.stdout)
        for i in range(5, 8):
            self.assertIn(f"历史备注-{i}", summary.stdout)
        detail = self.run_cli(self.repo, "show", "--issue", "1")
        self.assertEqual(detail.returncode, 0, detail.stderr)
        self.assertIn("long-receipt", detail.stdout)
        self.assertNotIn("#4", detail.stdout)
        completed = self.run_cli(self.repo, "show", "--issue", "2")
        self.assertIn("checkpoint=交付完成", completed.stdout)
        full = self.run_cli(self.repo, "show", "--full")
        self.assertEqual(full.returncode, 0, full.stderr)
        for text in ("#2", "#3", "long-receipt", "历史备注-0"):
            self.assertIn(text, full.stdout)
        missing = self.run_cli(self.repo, "show", "--issue", "missing")
        self.assertNotEqual(missing.returncode, 0)
        self.assertEqual([path.read_bytes() for path in paths], before)

    def test_single_reviewer_rotation(self):
        self.run_cli(self.repo, "init", "--objective", "obj", "--reviewers", "solo")
        self.assertEqual(self.reviewer(), "solo")
        self.assertEqual(self.reviewer(), "solo")

    def test_multi_reviewer_rotation(self):
        self.run_cli(self.repo, "init", "--objective", "obj", "--reviewers", "a", "b", "c")
        self.assertEqual([self.reviewer() for _ in range(3)], ["a", "b", "c"])
        self.assertEqual(self.reviewer(), "a")  # wraps around

    def test_chinese_full_state_flow(self):
        (self.repo / ".gitignore").write_text("# 中文忽略项\n", encoding="utf-8")
        commands = [
            ("init", "--objective", "中文目标", "--reviewers", "reviewer-a"),
            ("set", "1", "stage=dev", "title=中文任务", "--why", "开始开发"),
            ("ask", "是否通过验收？"),
            ("answer", "0", "确认通过"),
            ("rule", "保留原始流程"),
            ("note", "交接记录"),
            ("event", "中文事件 → 验收"),
            ("set", "1", "stage=merged"),
            ("show", "--events", "20"),
        ]
        for command in commands:
            result = self.run_cli(self.repo, *command)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("中文目标", result.stdout)
        self.assertIn("中文事件 → 验收", result.stdout)
        self.assertEqual(self.state()["issues"]["1"]["stage"], "merged")
        self.assertEqual(self.state()["questions"], [])
        self.assertIn(".orchestration/", (self.repo / ".gitignore").read_text(encoding="utf-8"))

    def assert_not_created(self):
        self.assertFalse(self.repo.joinpath(".orchestration").exists())

    def test_missing_reviewers_fails_before_side_effects(self):
        r = self.run_cli(self.repo, "init", "--objective", "obj")
        self.assertNotEqual(r.returncode, 0)
        self.assert_not_created()

    def test_empty_list_fails_before_side_effects(self):
        # nargs="+" rejects a zero-argument --reviewers at parse time.
        r = self.run_cli(self.repo, "init", "--objective", "obj", "--reviewers")
        self.assertNotEqual(r.returncode, 0)
        self.assert_not_created()

    def test_blank_identifier_fails_before_side_effects(self):
        r = self.run_cli(self.repo, "init", "--objective", "obj", "--reviewers", "ok", "   ")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("non-blank", r.stderr)
        self.assert_not_created()


    def test_init_uses_clock_started_at(self):
        clock = self.repo / ".orchestration" / "clock.json"
        clock.parent.mkdir(parents=True)
        clock.write_text(json.dumps({"started_at": "2026-01-01T09:00:00+00:00",
                                     "run": None, "finished_at": None}), encoding="utf-8")
        result = self.run_cli(self.repo, "init", "--objective", "obj", "--reviewers", "a")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.state()
        self.assertEqual(state["started_at"], "2026-01-01T09:00:00+00:00")
        self.assertTrue(state["initialized_at"])
        self.assertEqual(json.loads(clock.read_text(encoding="utf-8"))["run"], "r1")

    def test_stage_change_appends_history_once(self):
        self.run_cli(self.repo, "init", "--objective", "obj", "--reviewers", "a")
        self.run_cli(self.repo, "set", "3", "stage=ready", "title=任务")
        self.run_cli(self.repo, "set", "3", "stage=dev", "--why", "开始开发")
        self.run_cli(self.repo, "set", "3", "stage=dev", "title=新标题")
        issue = self.state()["issues"]["3"]
        self.assertEqual([(h["from"], h["to"]) for h in issue["history"]],
                         [(None, "ready"), ("ready", "dev")])
        self.assertEqual(issue["history"][1]["why"], "开始开发")
        self.assertTrue(issue["stage_since"])

    def test_finish_writes_timing_and_is_idempotent(self):
        self.run_cli(self.repo, "init", "--objective", "obj", "--reviewers", "a")
        self.run_cli(self.repo, "set", "3", "stage=dev", "title=任务")
        first = self.run_cli(self.repo, "finish", "--reason", "completed")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("本次编排", first.stdout)
        state = self.state()
        self.assertEqual(state["end_reason"], "completed")
        self.assertTrue(state["finished_at"])
        self.assertIn("本次编排", (self.orch_dir() / "timing.md").read_text(encoding="utf-8"))
        finished = state["finished_at"]
        self.run_cli(self.repo, "finish", "--reason", "paused")
        self.assertEqual(self.state()["finished_at"], finished)
        self.assertEqual(self.state()["end_reason"], "completed")

    def test_timing_json_and_show_elapsed(self):
        self.run_cli(self.repo, "init", "--objective", "obj", "--reviewers", "a")
        self.run_cli(self.repo, "set", "3", "stage=dev", "title=任务")
        result = self.run_cli(self.repo, "timing", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertIn("3", data["issues"])
        self.assertGreaterEqual(data["elapsed"], 0)
        shown = self.run_cli(self.repo, "show")
        self.assertIn("已运行", shown.stdout)


if __name__ == "__main__":
    unittest.main()