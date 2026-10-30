"""Regression checks for the suite's default requirements clarification flow."""

import unittest
from pathlib import Path


SKILLS = Path(__file__).resolve().parents[2]
SUITE = SKILLS.parents[1]


def read(relative):
    return (SKILLS / relative).read_text(encoding="utf-8")


class ClarificationFlow(unittest.TestCase):
    def test_orchestration_routes_to_wayfinder(self):
        entry = read("orca-issue-orchestration/SKILL.md")
        self.assertIn("需要澄清时默认调用同套件 `wayfinder`", entry)
        self.assertIn("不要求用户另行输入 `/wayfinder`", entry)
        self.assertIn("已有确认且范围未变就直接继续", entry)
        self.assertIn("首批实现 Issue", entry)

    def test_wayfinder_can_be_invoked_automatically(self):
        skill = read("wayfinder/SKILL.md")
        self.assertNotIn("disable-model-invocation: true", skill)
        self.assertNotIn("Use Wayfinder only when", skill)
        self.assertIn("invokes Wayfinder by default", skill)
        config = read("wayfinder/agents/openai.yaml")
        self.assertIn("allow_implicit_invocation: true", config)
        self.assertNotIn("allow_implicit_invocation: false", config)

    def test_preparation_has_one_default_entry(self):
        preparation = read("orca-issue-orchestration/references/preparation.md")
        self.assertIn("## 需求澄清与确认：wayfinder", preparation)
        self.assertIn("**默认 wayfinder**", preparation)
        self.assertNotIn("**默认 html-plan**", preparation)
        self.assertNotIn("wayfinder 仅显式启用", preparation)
        self.assertIn("每 session 一张 HITL 票", preparation)
        self.assertIn("答题不自动等于批准", preparation)

    def test_pending_remote_does_not_block_clarification(self):
        preparation = read("orca-issue-orchestration/references/preparation.md")
        self.assertIn("待新建计划及授权已明确", preparation)
        self.assertIn("在下述本地澄清记录目录暂存 Map / 决策记录", preparation)
        self.assertIn("不提前建库、不强行写 GitHub", preparation)
        self.assertIn("需求澄清期间不启动 watcher、不创建开发 Run、不派开发 worker", preparation)
        wayfinder = read("wayfinder/SKILL.md")
        self.assertIn("**Orca preparation override:**", wayfinder)
        self.assertIn("keep the map and decision records in the agreed local clarification directory", wayfinder)

    def test_small_task_returns_without_an_extra_start_question(self):
        wayfinder = read("wayfinder/SKILL.md")
        self.assertIn("you don't need a map", wayfinder)
        self.assertIn("continue without an extra proceed question", wayfinder)
        self.assertIn("return the clarified scope, acceptance criteria and confirmation record", wayfinder)

    def test_html_plan_is_a_presentation_tool(self):
        html_plan = read("html-plan/SKILL.md")
        self.assertIn("inside its default Wayfinder clarification flow", html_plan)
        self.assertIn("not as a separate requirements entry", html_plan)
        self.assertIn("Standalone `/html-plan` usage remains available", html_plan)

    def test_map_tickets_reuse_the_task_label(self):
        labels = read("orca-issue-orchestration/templates/issues/LABELS.md")
        self.assertIn("提前到首张 Map / 决策票之前创建", labels)
        self.assertIn("后续实现阶段复用", labels)
        self.assertIn("wayfinder Map / 决策票", labels)
        preparation = read("orca-issue-orchestration/references/preparation.md")
        self.assertIn("按已有权限记录完成结论并关闭 Map", preparation)
        self.assertIn("不能为停止 watcher 提前关闭未决 Map / 票据", preparation)

    def test_changed_shared_files_match_on_both_platforms(self):
        shared = (
            "orca-issue-orchestration/SKILL.md",
            "orca-issue-orchestration/references/preparation.md",
            "orca-issue-orchestration/templates/project-config.md",
            "orca-issue-orchestration/templates/issues/LABELS.md",
            "wayfinder/SKILL.md",
            "wayfinder/agents/openai.yaml",
            "html-plan/SKILL.md",
            "orca-issue-orchestration/scripts/clarification_flow_test.py",
        )
        for relative in shared:
            with self.subTest(file=relative):
                windows = (SUITE / "windows" / "skills" / relative).read_bytes()
                macos = (SUITE / "macos" / "skills" / relative).read_bytes()
                self.assertEqual(windows, macos)


if __name__ == "__main__":
    unittest.main()
