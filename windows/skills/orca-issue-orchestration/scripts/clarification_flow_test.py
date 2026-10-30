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
            "html-plan/examples/prd-confirmation.html",
            "html-plan/runtime/pack.test.mjs",
            "prd-writing/SKILL.md",
            "prd-writing/manifest.yaml",
            "prd-writing/references/team-prd-template.md",
            "orca-issue-orchestration/references/requirements.md",
            "orca-issue-orchestration/scripts/clarification_flow_test.py",
        )
        for relative in shared:
            with self.subTest(file=relative):
                windows = (SUITE / "windows" / "skills" / relative).read_bytes()
                macos = (SUITE / "macos" / "skills" / relative).read_bytes()
                self.assertEqual(windows, macos)


    def test_three_skills_have_distinct_roles_and_one_requirements_source(self):
        guide = read("orca-issue-orchestration/references/requirements.md")
        for text in ["wayfinder", "prd-writing", "html-plan", "PRD 是统一需求依据",
                     "HTML 是其对应版本的展示", "已有项目 SPEC 满足要求时沿用其位置和身份"]:
            self.assertIn(text, guide)
        wayfinder = read("wayfinder/SKILL.md")
        self.assertIn("prd-writing/SKILL.md", wayfinder)
        self.assertIn("A key gap goes back to Wayfinder", wayfinder)
        self.assertIn("Do not call the writer to create a new PRD for every ticket", wayfinder)

    def test_prd_writer_is_pinned_and_requires_materials(self):
        writer = read("prd-writing/SKILL.md")
        self.assertIn("db85b2c7fd8e9f4769d8bb6d963c9108b307e5cf", writer)
        for text in ["材料门槛", "关键分歧未解决", "不生成伪完整 PRD", "需要产品决定的缺口交回 wayfinder", "wayfinder",
                     "问题/目标 → 开发项 → 测试项 → 验收项", "老用户影响", "数据迁移", "灰度与回滚",
                     "当前行为以已验证源码/数据证据为准", "修订同一份文档"]:
            self.assertIn(text, writer)
        self.assertIn("license: internal", read("prd-writing/manifest.yaml"))
        self.assertIn("## 8. 验收标准", read("prd-writing/references/team-prd-template.md"))

    def test_responses_bind_to_version_scope_and_return_to_prd(self):
        guide = read("orca-issue-orchestration/references/requirements.md")
        for text in ["PRD ID、版本、澄清 ID、轮次和问题 ID", "不将过期答复直接应用到新版本",
                     "将明确有效的决定更新回 PRD", "单票确认只作用于该范围",
                     "明确确认只记录一次", "只重新确认受影响且尚未确认的内容",
                     "同一回复中明确选择修改并确认其结果", "单票页面的确认不自动覆盖整份 PRD"]:
            self.assertIn(text, guide)

    def test_small_changes_and_prior_approval_do_not_add_a_gate(self):
        guide = read("orca-issue-orchestration/references/requirements.md")
        for text in ["小改动不强制完整 PRD、Map 或 HTML", "不能因引入 PRD 增加批准关卡",
                     "关键阻断未决项未解决时，不派开发", "需求确认和实施授权分别核对"]:
            self.assertIn(text, guide)
        html = read("html-plan/SKILL.md")
        for text in ["## PRD presentation in the Orca suite", "The PRD is the single requirements source",
                     "PRD ID/version, clarification ID and round", "do not demand a second approval",
                     "updates the same PRD", "Technical details are optional drill-down"]:
            self.assertIn(text, html)

    def test_worker_templates_share_the_fixed_prd_and_acceptance_ids(self):
        for name in ("dev", "review", "fix"):
            with self.subTest(template=name):
                template = read("orca-issue-orchestration/templates/" + name + ".md")
                self.assertIn("统一 PRD / SPEC", template)
                self.assertIn("需求/验收 ID", template)
                self.assertIn("快照", template)

    def test_example_exposes_version_and_scope_without_automatic_writeback(self):
        example = read("html-plan/examples/prd-confirmation.html")
        title = "预约取消 PRD-DEMO v1 CLAR-DEMO r1"
        self.assertIn("<title>" + title + "</title>", example)
        self.assertIn("<h1>" + title + "</h1>", example)
        for marker in ['data-prd-id="PRD-DEMO"', 'data-prd-version="v1"',
                       'data-clarification-id="CLAR-DEMO"', 'data-round="r1"',
                       'id="q1-cancel-retry"', "Q-1 → R-2 / AC-2", "不能确认整份需求",
                       "协调者校验版本后更新同一份 PRD"]:
            self.assertIn(marker, example)
        self.assertNotIn("https://", example)
        self.assertNotIn("fetch(", example)

    def test_suite_contains_twelve_skills_and_readiness_references_prd(self):
        self.assertEqual(len(list(SKILLS.glob("*/SKILL.md"))), 12)
        preparation = read("orca-issue-orchestration/references/preparation.md")
        self.assertIn("[需求衔接](requirements.md)", preparation)
        self.assertIn("展示答复已校验并更新回正文", preparation)
        self.assertIn("确认版 PRD / SPEC 的不可变快照与需求/验收 ID", preparation)


if __name__ == "__main__":
    unittest.main()
