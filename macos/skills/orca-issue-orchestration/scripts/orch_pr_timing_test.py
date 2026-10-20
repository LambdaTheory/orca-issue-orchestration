#!/usr/bin/env python3
"""Stdlib-only unittest for the final PR duration section."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import orch_pr_timing

CLI = Path(__file__).with_name("orch_pr_timing.py")


def state():
    return {
        "run": "r1",
        "started_at": "2026-01-01T10:00:00+00:00",
        "finished_at": "2026-01-01T12:00:00+00:00",
        "end_reason": "completed",
        "issues": {
            "3": {
                "stage": "closed",
                "stage_since": "2026-01-01T11:50:00+00:00",
                "title": "示例任务",
                "history": [
                    {"at": "2026-01-01T10:00:00+00:00", "from": None, "to": "ready"},
                    {"at": "2026-01-01T10:30:00+00:00", "from": "ready", "to": "dev"},
                    {"at": "2026-01-01T11:30:00+00:00", "from": "dev", "to": "review"},
                    {"at": "2026-01-01T11:50:00+00:00", "from": "review", "to": "closed"},
                ],
            }
        },
    }


class OrchPrTimingTest(unittest.TestCase):
    def test_render_has_final_numbers_and_scope(self):
        section = orch_pr_timing.render(state())
        self.assertIn("## 工作时长", section)
        self.assertIn("- 总历时：2h（2026-01-01 10:00 → 2026-01-01 12:00）", section)
        self.assertIn("- 有效工作：1h20m（开发 1h · 评审 20m）", section)
        self.assertIn("- 等待：30m（待派发 30m）", section)
        self.assertIn("- 结束状态：完成", section)
        self.assertIn(".orchestration/r1/timing.md", section)

    def test_upsert_appends_without_touching_body(self):
        body = "## PR 概述\n\n现有正文\n"
        merged = orch_pr_timing.upsert(body, orch_pr_timing.render(state()))
        self.assertTrue(merged.startswith("## PR 概述\n\n现有正文"))
        self.assertEqual(merged.count("## 工作时长"), 1)
        self.assertIn("## 工作时长", merged)

    def test_upsert_replaces_old_section_once(self):
        body = ("## PR 概述\n\n前文\n\n## 工作时长\n\n- 旧数据\n\n"
                "## 测试与验证说明\n\n后文\n")
        merged = orch_pr_timing.upsert(body, orch_pr_timing.render(state()))
        self.assertEqual(merged.count("## 工作时长"), 1)
        self.assertNotIn("旧数据", merged)
        self.assertIn("## 测试与验证说明\n\n后文", merged)
        self.assertIn("- 总历时：2h", merged)

    def test_missing_start_is_not_fabricated(self):
        data = state()
        data.pop("started_at")
        self.assertIn("未记录开始时间", orch_pr_timing.render(data))

    def test_cli_refuses_before_finish(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / ".orchestration" / "r1"
            run.mkdir(parents=True)
            data = state()
            data.pop("finished_at")
            (run / "state.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            env = dict(os.environ, PYTHONUTF8="1")
            result = subprocess.run(
                [sys.executable, str(CLI), "--repo", directory, "--run", "r1", "--pr", "1"],
                capture_output=True, text=True, encoding="utf-8", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("尚未收口", result.stderr)


if __name__ == "__main__":
    unittest.main()
