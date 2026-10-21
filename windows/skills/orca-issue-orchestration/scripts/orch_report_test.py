#!/usr/bin/env python3
"""Stdlib-only unittest for the read-only hourly report."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CLI = Path(__file__).with_name("orch_report.py")


class OrchReportTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="orch report 中文 ")
        self.repo = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = self.repo / ".orchestration" / "r1"
        self.run_dir.mkdir(parents=True)

    def write_state(self, issues):
        state = {"run": "r1", "objective": "演示目标", "issues": issues}
        (self.run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    def report(self, *args):
        env = dict(os.environ, PYTHONUTF8="1")
        return subprocess.run(
            [sys.executable, str(CLI), "--repo", str(self.repo), "--run", "r1", *args],
            capture_output=True, text=True, encoding="utf-8", env=env,
        )

    def test_reports_active_review_merge_wait_and_block(self):
        self.write_state({
            "3": {"stage": "dev", "title": "放开只读模型查询通道", "dev_agent": "codex",
                  "dev_worktree": "/tmp/workspaces/issue-3-api-proxy"},
            "4": {"stage": "review", "title": "模型下拉改为动态获取", "review_agent": "claude",
                  "review_base": "a8ddbea59b3408eefb2791fc82b07a593665b0c7",
                  "review_head": "7ed0b3b2a69c86bc0285732f48a6fea3abaf8aa2"},
            "5": {"stage": "verify", "title": "修复 CSRF", "delivery": "pr_delivered",
                  "delivery_sha": "7ae052138e4847778005eb3d47ebaf958a341ec2", "pr": "8"},
            "6": {"stage": "user_gate", "title": "切换上游模型", "open_question": "是否上线"},
            "7": {"stage": "blocked", "title": "依赖任务", "blocker": "等待 #3 合并"},
            "8": {"stage": "merged", "title": "已完成任务"},
        })
        result = self.report()
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn("进度：开发 1 · 评审 1 · 待合并 1 · 等用户 1 · 阻塞 1 · 完成 1", out)
        self.assertIn("#3 放开只读模型查询通道（codex，issue-3-api-proxy）", out)
        self.assertIn("#4 模型下拉改为动态获取（claude，a8ddbea5→7ed0b3b2）", out)
        self.assertIn("#5 修复 CSRF（PR #8，HEAD 7ae05213）", out)
        self.assertIn("等用户：", out)
        self.assertIn("是否上线", out)
        self.assertIn("等待 #3 合并", out)

    def test_empty_state_says_nothing_active(self):
        self.write_state({"9": {"stage": "closed", "title": "只完成"}})
        result = self.report()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("进度：完成 1", result.stdout)
        self.assertIn("当前没有在开发、评审或待合并的任务", result.stdout)

    def test_missing_state_fails_loudly(self):
        result = self.report()
        self.assertEqual(result.returncode, 2)
        self.assertIn("还没初始化", result.stderr)

    def test_zero_wait_prints_immediately(self):
        self.write_state({})
        result = self.report("--wait", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("编排小时报", result.stdout)


if __name__ == "__main__":
    unittest.main()
