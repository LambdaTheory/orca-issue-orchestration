#!/usr/bin/env python3
"""Stdlib-only unittest for stage-duration accounting."""
import unittest

import orch_time


class OrchTimeTest(unittest.TestCase):
    def state(self):
        return {
            "started_at": "2026-01-01T10:00:00+00:00",
            "finished_at": "2026-01-01T12:00:00+00:00",
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
                },
                "4": {"stage": "ready", "title": "未开始记录"},
            },
        }

    def test_attributes_time_to_the_stage_entered(self):
        data = orch_time.compute(self.state())
        self.assertEqual(data["elapsed"], 7200)
        self.assertEqual(data["stages"]["ready"], 1800)
        self.assertEqual(data["stages"]["dev"], 3600)
        self.assertEqual(data["stages"]["review"], 1200)
        self.assertEqual(data["effective"], 4800)
        self.assertEqual(data["waiting"], 1800)
        self.assertEqual(data["issues"]["3"]["seconds"], 6600)
        self.assertNotIn("4", data["issues"])

    def test_text_reports_total_effective_and_cycle(self):
        output = orch_time.text(orch_time.compute(self.state()))
        self.assertIn("本次编排 2h（2026-01-01 10:00 → 2026-01-01 12:00）", output)
        self.assertIn("有效工作 1h20m · 等待 30m", output)
        self.assertIn("阶段：待派发 30m · 开发 1h · 评审 20m", output)
        self.assertIn("#3 示例任务 1h50m（待派发 30m / 开发 1h / 评审 20m）", output)

    def test_missing_start_is_reported_not_guessed(self):
        data = orch_time.compute({"issues": {}})
        self.assertEqual(data["elapsed"], 0)
        self.assertIn("未记录开始时间", orch_time.text(data))

    def test_duration_format(self):
        self.assertEqual(orch_time.fmt(0), "0s")
        self.assertEqual(orch_time.fmt(59), "59s")
        self.assertEqual(orch_time.fmt(60), "1m")
        self.assertEqual(orch_time.fmt(3660), "1h01m")
        self.assertEqual(orch_time.fmt(7200), "2h")


if __name__ == "__main__":
    unittest.main()
