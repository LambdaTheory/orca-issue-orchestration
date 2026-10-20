#!/usr/bin/env python3
"""Stdlib-only unittest for the skill start clock."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CLI = Path(__file__).with_name("orch_clock.py")


class OrchClockTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="orch clock 中文 ")
        self.repo = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def run_cli(self, *args):
        env = dict(os.environ, PYTHONUTF8="1")
        return subprocess.run([sys.executable, str(CLI), "--repo", str(self.repo), *args],
                              capture_output=True, text=True, encoding="utf-8", env=env)

    def clock(self):
        return json.loads((self.repo / ".orchestration" / "clock.json").read_text(encoding="utf-8"))

    def test_start_is_idempotent_while_active(self):
        first = self.run_cli("start")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_cli("start")
        self.assertEqual(first.stdout.strip(), second.stdout.strip())
        self.assertEqual(self.clock()["started_at"], first.stdout.strip())
        self.assertIn("/.orchestration/", (self.repo / ".gitignore").read_text(encoding="utf-8"))

    def test_start_after_finish_begins_new_clock(self):
        self.run_cli("start")
        path = self.repo / ".orchestration" / "clock.json"
        data = self.clock()
        data["started_at"] = "2026-01-01T10:00:00+00:00"
        data["finished_at"] = "2026-01-01T12:00:00+00:00"
        path.write_text(json.dumps(data), encoding="utf-8")
        result = self.run_cli("start")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(result.stdout.strip(), "2026-01-01T10:00:00+00:00")
        self.assertIsNone(self.clock()["finished_at"])

    def test_show_requires_clock(self):
        self.assertNotEqual(self.run_cli("show").returncode, 0)
        self.run_cli("start")
        shown = self.run_cli("show", "--json")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("started_at", json.loads(shown.stdout))


if __name__ == "__main__":
    unittest.main()
