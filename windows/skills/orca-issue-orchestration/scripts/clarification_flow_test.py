"""Regression checks for Wayfinder, the single PRD and Align Requirements."""

import unittest
from pathlib import Path


SKILLS = Path(__file__).resolve().parents[2]
SUITE = SKILLS.parents[1]


def read(relative):
    return (SKILLS / relative).read_text(encoding="utf-8")


class ClarificationFlow(unittest.TestCase):
    def test_clarification_phase_keeps_prototype_domain_modeling_and_reuse_boundaries(self):
        wayfinder = read("wayfinder/SKILL.md")
        for text in ["stay exploratory artifacts",
                     "do not follow its standalone fold-back/production-absorption step",
                     "a resolved design ticket is not authorization to implement",
                     "reuse the project's agreed document locations",
                     "ask the user to confirm the location",
                     "retain modeling decisions in the agreed clarification record",
                     "Reuse confirmed requirements and existing Maps instead of interviewing again",
                     "never pad the batch with invented questions"]:
            self.assertIn(text, wayfinder)
    def test_orchestration_routes_to_wayfinder_and_alignment(self):
        entry = read("orca-issue-orchestration/SKILL.md")
        self.assertIn("启动或恢复本工作流时必须调用同套件 `wayfinder`", entry)
        self.assertIn("不要求用户另行输入 `/wayfinder`", entry)
        self.assertIn("已有确认且范围未变就复用", entry)
        self.assertIn("align-requirements", entry)
        self.assertIn("首批实现 Issue", entry)

    def test_wayfinder_can_be_invoked_automatically(self):
        skill = read("wayfinder/SKILL.md")
        self.assertNotIn("disable-model-invocation: true", skill)
        self.assertNotIn("Use Wayfinder only when", skill)
        self.assertIn("invokes this skill by default", skill)
        config = read("wayfinder/agents/openai.yaml")
        self.assertIn("allow_implicit_invocation: true", config)

    def test_preparation_has_one_default_entry(self):
        preparation = read("orca-issue-orchestration/references/preparation.md")
        for text in ["## 需求澄清与确认：wayfinder", "**默认 wayfinder**", "批量发布到 Answer Desk",
                     "同一题只有一个答复受理口", "一条答复按 Answer ID 与 questionVersion 只处理一次",
                     "答题不自动等于批准"]:
            self.assertIn(text, preparation)
        self.assertNotIn("wayfinder 仅显式启用", preparation)

    def test_pending_remote_does_not_block_clarification(self):
        preparation = read("orca-issue-orchestration/references/preparation.md")
        for text in ["待新建计划及授权已明确", "Map 只用 Wayfinder MCP 的",
                     "工具缺失或服务失败时明确暂停并说明原因", "不做旁路 HTTP 写，不用 GitHub 或本地文件替代地图状态",
                     "规划阶段不启动 watcher、不创建开发 Run、不派开发 worker"]:
            self.assertIn(text, preparation)
        wayfinder = read("wayfinder/SKILL.md")
        self.assertIn("**Orca preparation override:**", wayfinder)
        self.assertIn("never migrate them into the service", wayfinder)

    def test_no_small_task_or_prior_confirmation_branch_skips_map(self):
        wayfinder = read("wayfinder/SKILL.md")
        for text in ["every start or resume", "create or restore an MCP Map", "Zero Tickets do not waive the Map"]:
            self.assertIn(text, wayfinder)
        for text in ["need no Map", "Small changes need no full PRD, Map", "lightweight requirement"]:
            self.assertNotIn(text, wayfinder)

    def test_alignment_is_presentation_and_feedback_not_another_interview(self):
        alignment = read("align-requirements/SKILL.md")
        for text in ["## Orca 套件衔接（本地适配）", "不重新访谈", "不输出实施方案树",
                     "模板没有内置评论、答题导出或自动回写", "默认在当前对话", "创建或恢复 MCP Map"]:
            self.assertIn(text, alignment + read("orca-issue-orchestration/references/requirements.md"))
        self.assertIn("不作为本流程的默认入口、依赖或失败回退",
                      read("orca-issue-orchestration/references/requirements.md"))

    def test_map_tickets_reuse_the_task_label(self):
        labels = read("orca-issue-orchestration/templates/issues/LABELS.md")
        for text in ["不再在 GitHub 建 Map", "只读历史，保持原样、不迁移不双写", "wayfinder Map / 决策票"]:
            self.assertIn(text, labels)
        preparation = read("orca-issue-orchestration/references/preparation.md")
        self.assertIn("以当前 `map_read` 核对相关票全部 settle、无阻断 Fog", preparation)
        self.assertIn("空 Frontier 不算完成", preparation)
        self.assertIn("不编造 map.close", preparation)

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
                     "do not turn an unresolved proposal into a complete PRD"]:
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
        for text in ["PRD ID、版本、澄清 ID、轮次和问题 ID", "不将真正过期的答复直接应用到新内容",
                     "将明确有效的决定更新回 PRD", "单票确认只作用于该范围", "明确确认只记录一次",
                     "只重新确认受影响且尚未确认的内容", "同一回复中明确选择修改并确认其结果",
                     "单票或单条答复的确认不自动覆盖整份 PRD", "模板不自带评论工具、答题导出或自动回写"]:
            self.assertIn(text, guide)

    def test_revision_growth_alone_does_not_require_another_answer(self):
        guide = read("orca-issue-orchestration/references/requirements.md")
        for required in ("claim、research 或独立票 settle 都会正常推进 Revision",
                         "当前 questionVersion 与 Answer ID 未变",
                         "不能仅因 Map Revision 或 PRD 版本前进就判过期或重问",
                         "Settlement 绑定", "所涉 PRD 需求是否实质改变",
                         "处理凭据由同一澄清检查点保存，服务 Settlements 才是权威"):
            self.assertIn(required, guide)
        self.assertNotIn("PRD 版本或 Map Revision 已前进的答复按过期处理", guide)

    def test_charting_can_continue_and_service_license_is_not_assigned(self):
        wayfinder = read("wayfinder/SKILL.md")
        self.assertIn("Continue directly into the first Frontier batch in the same session", wayfinder)
        self.assertNotIn("charting is one session's work", wayfinder)
        self.assertIn("Matt Pocock's wayfinder skill (MIT)", wayfinder)
        self.assertIn("license is governed by its upstream repository", wayfinder)
        self.assertNotIn("wayfinder_map_service (MIT)", wayfinder)
        self.assertNotIn(bytes([13]), (SKILLS / "wayfinder/SKILL.md").read_bytes())

    def test_desk_answered_user_does_not_need_a_second_comment(self):
        for relative in ("orca-issue-orchestration/templates/issues/user-decision.md",
                         "orca-issue-orchestration/references/operations.md"):
            text = read(relative)
            self.assertIn("Desk 已答的人不再要求发评论", text)
            self.assertIn("阻塞回链与已采用结论", text)

    def test_conversation_and_recommendation_scope_are_specific(self):
        preparation = read("orca-issue-orchestration/references/preparation.md")
        for required in ("需求页面的普通反馈默认让用户在当前对话", "已展示具体答案的明确对话 verdict",
                         "同批 withdraw", "answered in conversation", "不带 ",
                         "不同时保留两个受理口", "只覆盖已经展示且所指明确的具体答案",
                         "未展示的推荐、后续批次或含糊的 blanket 授权不算决定"):
            self.assertIn(required, preparation)

    def test_readme_distinguishes_connectivity_mechanical_checks_and_authority(self):
        readme = (SUITE / "README.md").read_text(encoding="utf-8")
        for required in ("WaitForMcpServers", "ToolSearch", "alwaysLoad: true",
                         "defer_loading: false", "required", "startup_readiness",
                         "wayfinder://workflow/exploration", "revision.state.tickets",
                         "只按传入的已处理键判重", "不保证业务只处理一次",
                         "服务 Settlements 才是权威", "工具名字符串齐全不等于已连上服务"):
            self.assertIn(required, readme)
        self.assertNotIn("保证同一条 Answer 只处理一次", readme)

    def test_mandatory_map_reuses_prior_approval_without_extra_approval_gate(self):
        guide = read("orca-issue-orchestration/references/requirements.md")
        for text in ["不跳过 Map", "不能因引入 PRD 增加批准关卡",
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
        for text in ["[需求衔接](requirements.md)", "展示与 Desk 答复已校验并更新回正文",
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
