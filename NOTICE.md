# 来源与许可文件

- `code-review`、`codebase-design`、`domain-modeling`、`implement`、`prototype`、`research`、`setup-matt-pocock-skills`、`tdd`、`wayfinder` 来自 Matt Pocock 技能套件；两平台均保留 `skills/LICENSE.matt-pocock`（MIT 文本，Copyright (c) 2026 Matt Pocock）。本套件含本地适配。`wayfinder` 的规划方法沿用其 MIT 来源，新 Map 改用 Wayfinder MCP 地图服务；旧 GitHub/本地地图说明仅供只读历史恢复，不是当前建图流程。产品侧只连接 MCP 契约，不将服务源码并入本套件，服务许可以上游仓库为准，不在此替服务指定 MIT 或其他许可。
- `html-plan` 仅保留独立用途，不再是主需求流程的依赖或回退；来源与原提交记录在其 `SKILL.md`，两平台保留其 `LICENSE`（Apache License 2.0）及本地修改说明。
- `prd-writing` 来源于 `LambdaTheory/team-skills` 的 `skills/prd-writing`，固定提交 `db85b2c7fd8e9f4769d8bb6d963c9108b307e5cf`、上游版本 1.1.0，原 manifest 的许可标记为 `internal`，未改为 MIT/Apache。两平台保留原 `manifest.yaml` 和 `references/team-prd-template.md`；入口增加了 Orca / wayfinder / PRD / align-requirements 衔接、事实与决定区分、原位修订及纯文本状态的本地适配。使用与分发仍须遵守来源仓库的内部许可，不因加入本套件获得公开分发授权。
- `align-requirements` 来源于 `LambdaTheory/team-skills/skills/align-requirements`，固定提交 `db85b2c7fd8e9f4769d8bb6d963c9108b307e5cf`、上游版本 1.0.0，原 manifest 许可标记 `internal`。两平台保留原模板、写作/图参考及 manifest 的原版本/作者/internal 许可；入口和 manifest 简介补充统一 PRD/wayfinder 交接、意见与授权边界，`shot.mjs` 仅修正跨平台文件 URL，另加本地示例和测试。原内部许可不因并入本套件获得公开分发授权。
- `orca-issue-orchestration` 是本仓库维护的编排 skill；此次上传的是 Windows 与 macOS 当前版本及相关参考文档、模板和脚本。

LambdaTheory 享有版权的原创代码与文档适用根目录 `LICENSE` 的保留权利声明，不提供开源许可证。第三方材料及其已有许可不受该声明替换或限制；上述 MIT、Apache 许可文件继续原样保留。本文件是来源索引，不为整个套件另行指定统一许可证。
