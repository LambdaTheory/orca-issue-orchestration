"""Regression checks for Wayfinder, the single PRD and Align Requirements."""

import unittest
from pathlib import Path


SKILLS = Path(__file__).resolve().parents[2]
SUITE = SKILLS.parents[1]


def read(relative):
    return (SKILLS / relative).read_text(encoding="utf-8")


class ClarificationFlow(unittest.TestCase):
    def test_orchestration_routes_to_wayfinder_and_alignment(self):
        entry = read("orca-issue-orchestration/SKILL.md")
        self.assertIn("需要澄清时默认调用同套件 `wayfinder`", entry)
        self.assertIn("不要求用户另行输入 `/wayfinder`", entry)
        self.assertIn("已有确认且范围未变就直接继续", entry)
        self.assertIn("align-requirements", entry)
        self.assertIn("首批实现 Issue", entry)

    def test_wayfinder_can_be_invoked_automatically(self):
        skill = read("wayfinder/SKILL.md")
        self.assertNotIn("disable-model-invocation: true", skill)
        self.assertNotIn("Use Wayfinder only when", skill)
        self.assertIn("invokes Wayfinder by default", skill)
        config = read("wayfinder/agents/openai.yaml")
        self.assertIn("allow_implicit_invocation: true", config)

    def test_preparation_has_one_default_entry(self):
        preparation = read("orca-issue-orchestration/references/preparation.md")
        for text in ["## 需求澄清与确认：wayfinder", "**默认 wayfinder**", "每 session 一张 HITL 票",
                     "答题不自动等于批准", "模板没有内置评论、答题导出或自动回写"]:
            self.assertIn(text, preparation)
        self.assertNotIn("wayfinder 仅显式启用", preparation)

    def test_pending_remote_does_not_block_clarification(self):
        preparation = read("orca-issue-orchestration/references/preparation.md")
        for text in ["待新建计划及授权已明确", "在下述本地澄清记录目录暂存 Map / 决策记录",
                     "不提前建库、不强行写 GitHub", "需求澄清期间不启动 watcher、不创建开发 Run、不派开发 worker"]:
            self.assertIn(text, preparation)
        wayfinder = read("wayfinder/SKILL.md")
        self.assertIn("**Orca preparation override:**", wayfinder)
        self.assertIn("keep the map and decision records in the agreed local clarification directory", wayfinder)

    def test_small_task_returns_without_an_extra_start_question(self):
        wayfinder = read("wayfinder/SKILL.md")
        for text in ["you don't need a map", "continue without an extra proceed question",
                     "return the clarified scope, acceptance criteria and confirmation record"]:
            self.assertIn(text, wayfinder)

    def test_alignment_is_presentation_and_feedback_not_another_interview(self):
        alignment = read("align-requirements/SKILL.md")
        for text in ["## Orca 套件衔接（本地适配）", "不重新访谈", "不输出实施方案树",
                     "模板没有内置评论、答题导出或自动回写", "默认在当前对话", "小改动不强制完整 PRD"]:
            self.assertIn(text, alignment + read("orca-issue-orchestration/references/requirements.md"))
        self.assertIn("不作为本流程的默认入口、依赖或失败回退",
                      read("orca-issue-orchestration/references/requirements.md"))

    def test_map_tickets_reuse_the_task_label(self):
        labels = read("orca-issue-orchestration/templates/issues/LABELS.md")
        for text in ["提前到首张 Map / 决策票之前创建", "后续实现阶段复用", "wayfinder Map / 决策票"]:
            self.assertIn(text, labels)
        preparation = read("orca-issue-orchestration/references/preparation.md")
        self.assertIn("按已有权限记录完成结论并关闭 Map", preparation)
        self.assertIn("不能为停止 watcher 提前关闭未决 Map / 票据", preparation)

    def test_changed_shared_files_match_on_both_platforms(self):
        shared = (
            "orca-issue-orchestration/SKILL.md",
            "orca-issue-orchestration/references/preparation.md",
            "orca-issue-orchestration/references/requirements.md",
            "orca-issue-orchestration/templates/project-config.md",
            "orca-issue-orchestration/templates/issues/LABELS.md",
            "wayfinder/SKILL.md", "wayfinder/agents/openai.yaml",
            "align-requirements/SKILL.md", "align-requirements/manifest.yaml",
            "align-requirements/assets/template.html",
            "align-requirements/references/writing.md", "align-requirements/references/figures.md",
            "align-requirements/scripts/shot.mjs", "align-requirements/scripts/shot.test.mjs",
            "align-requirements/examples/prd-confirmation.html",
            "prd-writing/SKILL.md", "prd-writing/manifest.yaml",
            "prd-writing/references/team-prd-template.md",
            "orca-issue-orchestration/scripts/clarification_flow_test.py",
        )
        for relative in shared:
            with self.subTest(file=relative):
                self.assertEqual((SUITE / "windows/skills" / relative).read_bytes(),
                                 (SUITE / "macos/skills" / relative).read_bytes())

    def test_three_skills_have_distinct_roles_and_one_requirements_source(self):
        guide = read("orca-issue-orchestration/references/requirements.md")
        for text in ["wayfinder", "prd-writing", "align-requirements", "PRD 是统一需求依据",
                     "HTML 是其对应版本的展示", "已有项目 SPEC 满足要求时沿用其位置和身份"]:
            self.assertIn(text, guide)
        wayfinder = read("wayfinder/SKILL.md")
        for text in ["prd-writing/SKILL.md", "A key gap goes back to Wayfinder",
                     "Do not call the writer to create a new PRD for every ticket"]:
            self.assertIn(text, wayfinder)

    def test_prd_writer_still_requires_materials_and_keeps_internal_source(self):
        writer = read("prd-writing/SKILL.md")
        self.assertIn("db85b2c7fd8e9f4769d8bb6d963c9108b307e5cf", writer)
        for text in ["材料门槛", "关键分歧未解决", "不生成伪完整 PRD", "需要产品决定的缺口交回 wayfinder",
                     "问题/目标 → 开发项 → 测试项 → 验收项", "老用户影响", "数据迁移", "灰度与回滚",
                     "当前行为以已验证源码/数据证据为准", "修订同一份文档", "align-requirements"]:
            self.assertIn(text, writer)
        for name in ["prd-writing", "align-requirements"]:
            self.assertIn("license: internal", read(name + "/manifest.yaml"))
        self.assertIn("## 8. 验收标准", read("prd-writing/references/team-prd-template.md"))

    def test_feedback_binds_to_version_scope_and_returns_to_prd(self):
        guide = read("orca-issue-orchestration/references/requirements.md")
        for text in ["PRD ID、版本、澄清 ID、轮次和问题 ID", "不将过期答复直接应用到新版本",
                     "将明确有效的决定更新回 PRD", "单票确认只作用于该范围", "明确确认只记录一次",
                     "只重新确认受影响且尚未确认的内容", "同一回复中明确选择修改并确认其结果",
                     "单票页面的确认不自动覆盖整份 PRD", "模板不自带评论工具、答题导出或自动回写"]:
            self.assertIn(text, guide)

    def test_small_changes_and_prior_approval_do_not_add_a_gate(self):
        guide = read("orca-issue-orchestration/references/requirements.md")
        for text in ["小改动不强制完整 PRD、Map 或 HTML", "不能因引入 PRD 增加批准关卡",
                     "关键阻断未决项未解决时，不派开发", "需求确认和实施授权分别核对"]:
            self.assertIn(text, guide)
        alignment = read("align-requirements/SKILL.md")
        self.assertIn("已有存放位置、确认和提交/推送授权直接复用", alignment)
        self.assertIn("不新增固定打分或确认门槛", alignment)

    def test_worker_templates_share_the_fixed_prd_and_acceptance_ids(self):
        for name in ("dev", "review", "fix"):
            with self.subTest(template=name):
                template = read("orca-issue-orchestration/templates/" + name + ".md")
                for text in ["统一 PRD / SPEC", "需求/验收 ID", "快照"]:
                    self.assertIn(text, template)

    def test_example_has_page_prd_and_discussion_versions_without_export_widgets(self):
        example = read("align-requirements/examples/prd-confirmation.html")
        self.assertIn("<title>预约取消需求 v1</title>", example)
        self.assertIn("<h1>预约取消需求 v1</h1>", example)
        for marker in ['data-page-version="v1"', 'data-prd-id="PRD-DEMO"', 'data-prd-version="v1"',
                       'data-clarification-id="CLAR-DEMO"', 'data-round="r1"', 'id="q1"',
                       "不能确认整份需求", "有效意见校验后更新同一份 PRD"]:
            self.assertIn(marker, example)
        for removed in ["<doc-ask", "<doc-plan", "Copy response", "Respond", "fetch(", 'href="https://']:
            self.assertNotIn(removed, example)

    def test_suite_adds_alignment_and_readiness_references_prd(self):
        self.assertEqual(len(list(SKILLS.glob("*/SKILL.md"))), 13)
        preparation = read("orca-issue-orchestration/references/preparation.md")
        for text in ["[需求衔接](requirements.md)", "展示答复已校验并更新回正文",
                     "确认版 PRD / SPEC 的不可变快照与需求/验收 ID"]:
            self.assertIn(text, preparation)

    def test_main_flow_no_longer_invokes_old_presentation_or_its_export(self):
        for relative in ["orca-issue-orchestration/SKILL.md",
                         "orca-issue-orchestration/references/preparation.md",
                         "orca-issue-orchestration/templates/project-config.md",
                         "wayfinder/SKILL.md", "prd-writing/SKILL.md"]:
            with self.subTest(file=relative):
                text = read(relative)
                for old in ["html-plan", "Copy response", "Respond", "packed HTML", "doc-ask"]:
                    self.assertNotIn(old, text, relative)

    def test_question_policy_does_not_let_obvious_defaults_make_product_decisions(self):
        guide = read("orca-issue-orchestration/references/requirements.md")
        self.assertIn("没有旧文档的新需求仍按用户目标提出必要问题", guide)
        self.assertIn("产品取舍、权限和范围不能因“答案明显”就由 Agent 代定", guide)
        self.assertIn("不新建评论服务", guide)


    def test_screenshot_commands_keep_each_platform_shell(self):
        for platform, shell in [("windows", "powershell"), ("macos", "bash")]:
            document = (SUITE / platform / "skills/orca-issue-orchestration/references/commands.md").read_text(encoding="utf-8")
            section = document.split("## 需求页面截图自查", 1)[1].split("## 只读模型与额度查询", 1)[0]
            self.assertIn("```" + shell, section)
            self.assertIn("align-requirements", section)
            self.assertIn("scripts/shot.mjs", section)
            self.assertIn("--playwright", section)
            if platform == "macos":
                self.assertNotIn("Join-Path", section)
            else:
                self.assertNotIn("$(dirname", section)


if __name__ == "__main__":
    unittest.main()
