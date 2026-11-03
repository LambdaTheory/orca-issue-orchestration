# Orca Issue Orchestration Skill Suite

**当前版本：v2.0.0（2026-11-03）** · Windows / macOS 双平台各 13 个项目级 skills（html-plan 仅保留独立用途）

面向 Orca 的目标驱动开发套件：从仓库探查、需求确认和 GitHub Issue 开始，由协调者安排独立 Agent 开发、评审与修复，最终按授权交付。协调者不代替 worker 写业务实现。

## 版权与使用许可

**Copyright (c) 2026 LambdaTheory. All rights reserved.**

代码公开可读。**未经 LambdaTheory 事先书面许可，不得使用、修改或分发其享有版权的原创代码与文档。** 该部分不提供开源许可证，详见根目录 [LICENSE](LICENSE)。

第三方组件继续遵循各自已有的 MIT、Apache 等许可证；本声明不覆盖这些许可。来源与许可索引见 [NOTICE.md](NOTICE.md)。

## 版本与下载

- 套件版本由根目录 `VERSION` 记录；这是整个套件的版本，不改变内含第三方 skill 的上游版本号。
- 源码分别位于 `windows/skills/` 和 `macos/skills/`，按实际执行宿主选择，不能混装两份同名 skill。
- 两平台源码只差参考文档中的平台示例：`references/commands.md` 按平台整套生成，`references/guard.md` 启动示例与 `references/pr.md` 平台选择说明随平台变化；其余文件完全相同，避免两份手工维护产生漂移。
- 本次 v2.0.0 的 Windows / macOS 发布包各含 13 个 skills，包含 `prd-writing`、`align-requirements`；`html-plan` 仅保留独立用途，非主流程依赖。对应版本正式发布后，可在本仓库 Releases 下载下列 ZIP；若尚无该版本 Release，请从当前版本源码安装，不将待发布附件视为已可下载。
- 本次为不兼容升级：工作流强制依赖外部 Wayfinder MCP，每次启动或恢复必须创建或恢复当前 MCP Map，移除旧 `small-task` 与 `--map-required` 接口，因此套件版本升为 2.0.0。团队 `prd-writing`、`align-requirements` 的原 `internal` 许可及 manifest 保持不变，本次发版不向使用方新增上游授权，详见 [NOTICE.md](NOTICE.md)。
- 历史发布事实：v1.1.1 的 Windows / macOS 包各含 11 个 skills；本次 v2.0.0 不改写旧 Release，也不表示既有安装已更新。
- `SHA256SUMS.txt` 用于核对下载文件；变更记录见 [CHANGELOG.md](CHANGELOG.md)。

| 执行宿主 | 源码 | Release 文件 |
|---|---|---|
| Windows | [`windows/skills`](windows/skills) | `orca-issue-orchestration-v2.0.0-windows.zip` |
| macOS | [`macos/skills`](macos/skills) | `orca-issue-orchestration-v2.0.0-macos.zip` |

## 核心流程

```text
探查已有事实 → wayfinder 澄清 → prd-writing 整理统一需求
→ align-requirements 展示需求并收意见确认（决定更新回 PRD）→ 创建/复用实现 Issue
→ Orca 开发 worker → 一次独立评审 → 必要修复与验证
→ 按授权交付 PR → 记录结果并收口
```

- 每次启动或恢复本工作流必须进入同套件 `wayfinder` 并创建或恢复 MCP Map；Map 只用 Wayfinder MCP（`map_create` / `map_list` / `map_read` / `map_apply` / `map_wait`），Answer Desk 批量提问，旧 GitHub 图只读保留。`prd-writing` 检查并整理统一 PRD（已有 SPEC 可直接承担），`align-requirements` 用需求文档式页面展示对应版本并收集意见，默认在当前对话反馈，不假定模板自带评论或答题导出。关键缺口回到 wayfinder，有效决定校验后更新回 PRD；已有确认直接复用，不重复问、不虚构票、不跳过 Map；无真实待决可零票但不可免 Map。三者不会各自访谈或重复确认，见[需求衔接](windows/skills/orca-issue-orchestration/references/requirements.md)。

## 工作流 Map 门禁

- 每次启动或恢复 `orca-issue-orchestration` 都必须调用同套件 wayfinder 并创建或恢复 MCP Map，不分大小、一轮可答或已有确认；复用当前业务工作流的同一张 Map，不是每轮消息或每个实现子 Issue 各建一图。
- 已有明确决定与授权复用为票内依据，不重复问、不虚构票、不跳过 Map。没有真实待决事项可以零票，Ticket / Answer 关联如实记为无，但 Map 标题、mapId 与 Revision 必须保留。
- 工具检查恒查 map_create / map_list / map_read / map_apply / map_wait 五项，缺任一项或服务失败即暂停，不提供 HTTP、GitHub 或本地替代图旁路。核对命令为 `<PY> -X utf8 <SKILL_DIR>/scripts/mcp_gate.py tools --tools <客户端工具列表>`；PY 按宿主选择实际 Python 3 解释器。
- gate 接收真实 map_read found envelope：revision.mapId 必须是非空字符串、revision.revision 必须是正整数，revision.state 必须存在，state.id 与 revision.mapId、state.currentRevision 与 revision.revision 分别一致。缺元数据、身份版本不一致、未知/缺失票状态、未决票或残留 Fog 均拒绝。
- 四项许可必须齐全：Map 相关票全部 settle 且无未决项或 Fog、PRD 同步、范围确认、实施授权。tickets 与 Fog 都为空的合法零票 Map 满足第一项，不要求造票；其余三项仍须为真。空 Frontier 本身不是完成证据。allow / proceed / accept / pass 退出码为 0，拒绝或暂停为 3，失效命令为参数错误。

## Wayfinder MCP 接入与行为校验

- 使用方须在宿主中真实连接 Wayfinder MCP，等待 `map_create`、`map_list`、`map_read`、`map_apply`、`map_wait` 工具就绪，并加载服务提供的当前工作流规则（`wayfinder://workflow/exploration`）及工具契约；本套件不代替宿主建立连接，也不持有地址或凭据。Windows 接入说明针对上游 PR #151 的采用分支，不代表主线已具备该能力；无需为安装本套件重测服务。
- CodeBuddy 用 `WaitForMcpServers` 等待连接，并通过 `ToolSearch`（Code Mode 用 `REPL.searchTools`）发现工具；默认 defer 可能使首轮工具不可见，需首轮可用时在服务配置显式使用 `alwaysLoad: true` 或 `defer_loading: false`。Codex 按宿主版本使用 `required` / `startup_readiness` 等待必需服务就绪。这里仅说明配置，不替用户改全局设置。工具名字符串齐全不等于已连上服务；真实接入和规则加载失败时明确暂停。
- 每次启动或恢复都需要 Map，缺上述任一工具，或服务调用失败时，流程**明确暂停**：可用 `python -X utf8 <SKILL_DIR>/scripts/mcp_gate.py tools --tools <客户端工具列表>`（macOS 用 python3）核对缺项并留档。脚本不提供旁路 HTTP 写，也不生成 GitHub/本地替代地图；恢复只能来自补齐工具接入或服务恢复，恢复后从暂停处重新 `map_read` 继续。
- `scripts/mcp_gate.py` 只做机械检查：`gate` 接收真实 `map_read` 的 found envelope，从 `revision.state.tickets` / `revision.state.fog` 读取状态（claim 为独立字段），先校验 revision.mapId、正整数 revision.revision 与 state 身份版本一致，再检查票；未知/缺失状态或残留 Fog 保守拒绝。合法零票且无 Fog 的 Map 在 PRD 同步、范围确认和实施授权齐全时可通过，空 Frontier 本身不算完成；三项确认由调用者据证提供。`answer-once` 只按传入的已处理键判重，不保证业务只处理一次；处理凭据由同一澄清检查点保存，服务 Settlements 才是权威。`version` 比较当前 Desk 题版本与 Answer ID，不因 Revision 或文档版本增长自动判过期；Settlement 绑定、所涉 PRD 需求实变、明确对话 verdict 与同批 withdraw/settle 仍由 Agent 按服务契约核对。`old-map` 拒绝列明的旧图写操作，不是所有外部写入的拦截器。JSON 拒绝/暂停的退出码为 3，并写明原因。
- 行为级校验命令：`python -X utf8 windows/skills/orca-issue-orchestration/scripts/mcp_gate_test.py`，macOS 用 `python3 -X utf8 macos/skills/orca-issue-orchestration/scripts/mcp_gate_test.py`；需求澄清流程定向回归跑同目录 `clarification_flow_test.py`。
- 新任务暂按用户确认的任务分类核对模型，唯一分类表见[临时模型分类与只读查询](windows/skills/orca-issue-orchestration/references/models.md)；MCP 提供正式分组后移除。本端只读取 OMC 模型目录与额度，不实现分组、排序、预约或自动替换，不维护第二套长期调度规则。
- 默认串行执行；开发、修复、评审及原生子代理共用模型并发额度，临时分类不扩大并发，已有项目约定优先。
- 空超时和存活心跳由等待脚本处理，不反复唤醒协调模型；脚本默认每轮检查后间隔 60 秒只读检查 worker，done/idle 未结算持续 120 秒或无可见进展 900 秒时发出核查提醒，不自动判死或重派。
- 状态默认读取当前摘要，详细记录按需读取。
- 默认一次独立评审；复用同 SHA、环境与范围未变的有效测试证据，不默认重复全量测试或 build。
- 主入口保持简短；准备、命令、操作和恢复细则保留在 `references/`，进入相应阶段再读取。
- 默认交付 PR；合并和其他外部动作沿用用户明确授权。

## 套件内容

| Skill | 用途 |
|---|---|
| `orca-issue-orchestration` | 主入口：初始化、需求确认、GitHub Issue、Orca 开发与评审编排 |
| `prd-writing` | 写前查漏、统一 PRD / SPEC、需求—测试—验收对应；关键缺口回到 wayfinder |
| `align-requirements` | 主流程需求文档展示、意见迭代与确认，关联统一 PRD / SPEC |
| `html-plan` | 仅保留独立实施方案用途；不再参与主流程，也不作为回退 |
| `implement` | 按已确认的 SPEC / Issue 执行实现 |
| `code-review` | 独立的 Standards / Spec 双轴评审；编排主流程不默认叠加调用 |
| `codebase-design` | 模块接口与设计分析 |
| `domain-modeling` | 领域模型、CONTEXT 与 ADR |
| `prototype` | 针对具体设计问题制作验证原型 |
| `research` | 查证资料与技术事实 |
| `tdd` | 按测试驱动方式实施指定任务 |
| `wayfinder` | 需求澄清必经入口；每次启动或恢复都必须调用同套件 wayfinder，并创建或恢复当前 MCP Map |
| `setup-matt-pocock-skills` | 准备 tracker、标签及领域文档约定 |

独立的 `code-review` 仍提供双轴评审能力，但编排主流程默认只安排一次独立评审，不因此自动开启更多评审 Agent。

## 运行前提

- 执行宿主已安装、启动 Orca，`orca status --json` 可访问。按当前二进制返回的 `orca skills get orchestration` / `orca skills get orca-cli` 使用命令。
- Python 3、Git、GitHub CLI；GitHub CLI 已登录并有目标仓库的必要权限。
- Node.js 用于页面与截图自查；`align-requirements/scripts/shot.mjs` 另需已有 Playwright 与浏览器，可通过 `--playwright` 或 `PLAYWRIGHT_DIR` 指定，不改全局 Agent 配置。旧 html-plan 独立打包仍使用 Node.js。
- Windows 使用 PowerShell；仅旧 GitHub watcher 回退需要真正的 Git Bash，不能选 WSL 的同名 `bash.exe`。
- macOS 使用 bash / zsh。非交互进程也必须能找到实际 Python 和其他命令。
- 用 webhook 监听 Issue 时按平台的命令参考申领个人凭据：首次 `TW start` 用 `gh auth token` 换取当前 GitHub 登录凭据，仅发送到你明确信任的 HTTPS 服务换取个人 message token，凭据自动保存为按服务 URL hash 区分的本机 JSON；401/403 属认证阻断，回到前台登录后再启动。需要时用 `WATCH_URL` 覆盖默认 webhook 服务地址。发布包不包含令牌，不会替你创建订阅或启动 watcher；旧监听迁移不复用旧共享 token 或无 owner 的旧 consumer，由用户确认暂停旧托管后在新状态目录建立个人订阅并完成对账。

## 安装

以下安装与运行说明供已获相应许可的用户参考。

可以直接从本仓库使用，也可以将**所选平台**的 `skills/` 内容放到你的 Agent 支持的技能目录。下面以项目级 `.agents/skills/` 为例；若客户端不扫描此位置，请按客户端约定安装，不要假定复制后所有 Agent 都会自动发现。

首次安装前检查目标目录；下面的复制只用于新目录。已有同名 skills 时先比较再合并，避免覆盖本地修改。

以下固定版本命令适用于 v2.0.0 tag 已发布后；发布前可从当前版本源码复制所选平台的 skills。

### Windows · v2.0.0 · 13 skills（PowerShell）

```powershell
git clone https://github.com/LambdaTheory/orca-issue-orchestration.git
cd orca-issue-orchestration
git checkout v2.0.0
# 首次安装到目标项目；把路径替换为实际项目目录。
$Destination = "E:/your-project/.agents/skills"
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
Copy-Item -Path "./windows/skills/*" -Destination $Destination -Recurse
```

详细命令见 [Windows 命令参考](windows/skills/orca-issue-orchestration/references/commands.md)。

### macOS · v2.0.0 · 13 skills（bash / zsh）

```bash
git clone https://github.com/LambdaTheory/orca-issue-orchestration.git
cd orca-issue-orchestration
git checkout v2.0.0
# 首次安装到目标项目；把路径替换为实际项目目录。
DEST="/path/to/your-project/.agents/skills"
mkdir -p "$DEST"
cp -R macos/skills/. "$DEST/"
```

详细命令见 [macOS 命令参考](macos/skills/orca-issue-orchestration/references/commands.md)。Release ZIP 解压后包含 `skills/`，复制其内容即可，不需要再次克隆仓库。

## 位置与解释器

| 变量 | 说明 |
|---|---|
| `ORCA_ISSUE_SKILL_DIR` | 可选：`orca-issue-orchestration/SKILL.md` 所在目录的绝对路径；不是整个套件根目录 |
| `OMC_MCP_ENDPOINT` | 用户信任的 OMC HTTPS MCP 端点；不在套件内写死部署地址 |
| `OMC_MCP_KEY` / `OMC_MCP_KEY_FILE` | 安全提供的只读密钥或本机密钥文件；不提交密钥、不传给 worker |
| `ORCA_ISSUE_PYTHON` | 可选：Python 3 命令名或可执行文件路径；不拼接参数 |
| `REPO` / `$REPO` | 正在开发的项目本地绝对路径；不是 GitHub `owner/name` |
| `RUN_ID` / `$RUN_ID` | 当前 Orca Run 的真实 ID，不填占位字符串 |

不指定覆盖时，skill 目录取本次实际加载的位置；Python 默认在 Windows 用 `python`，macOS 用 `python3`。安装目录与项目目录可以不同。具体的 `S` / `W` 定义、命令引用和凭据读取方式以对应平台的命令参考为准。

## 开始使用

在 Orca 中打开目标项目，从该工作空间里的 Agent 发起，例如：

> 使用 orca-issue-orchestration，先读取当前仓库与已有需求，复用已确认的事实，再按 GitHub Issue 推进这项功能。

协调者会检查项目配置、需求和已有编排记录。新项目可以参考 `templates/project-config.md`；不要把这个模板中的占位符当成可执行配置，也不用重复回答仓库中已经明确的问题。

## 平台差异与维护

Webhook 服务地址默认 `https://githubwebhook.madebynight.top`；需要时可设置环境变量 `WATCH_URL` 覆盖为其他服务地址，未设置时使用默认值。

两版不是运行时自动切换：Windows 保留 PowerShell 命令和 Windows 测试启动器；macOS 保留 bash/zsh 命令。v1.0.1 两端均包含 worker 健康检查、webhook 初始化提示修正和旧评论编辑事件回归测试。

升级时已运行的旧等待进程不会热加载新版。由原协调者在安全切换点核对未处理批次、停止自己持有的旧等待任务，再启动新版；不能同时挂两个长等待者。`health_alert` 是本地核查提醒，不是 worker_done 或死亡证明。它不负责在协调会话退出或宿主重启后自动拉起会话。

- 改共享逻辑时分别核对两版，不用一份平台目录直接覆盖另一份。
- 改平台命令时只改对应目录，并保留路径/解释器变量约定。
- 更新版本时同步 `VERSION`、本 README、`CHANGELOG.md`、Git tag 和 Release 附件文件名。
- 不把运行中的 `.orchestration/` 状态、凭据、会话日志或个人对比副本打入 Release。

## 本地检查

在仓库根目录执行，选择本机平台：

```powershell
python -X utf8 -B -m unittest discover -s windows/skills/orca-issue-orchestration/scripts -p "*_test.py"
node --test windows/skills/align-requirements/scripts/shot.test.mjs
# html-plan 独立工具保留原测试，不属于主流程
node --test windows/skills/html-plan/runtime/pack.test.mjs
```

```bash
python3 -X utf8 -B -m unittest discover -s macos/skills/orca-issue-orchestration/scripts -p '*_test.py'
node --test macos/skills/align-requirements/scripts/shot.test.mjs
# html-plan 独立工具保留原测试，不属于主流程
node --test macos/skills/html-plan/runtime/pack.test.mjs
```

v1.1.1 源码及两平台包分别通过 466 项 Python 测试和 5 项 Node 测试，均在 macOS 执行。已对默认线上服务验证首次自动申领个人凭据、launchd 托管、评论新增/编辑事件、pending 重放、暂停后重启续接、ACK 和受控停止。Windows 原生 PowerShell 分支与计划任务托管未在 Windows 实机复验，不能把这些结果视为 Windows 端到端认证。服务当前按组织成员准入与服务覆盖范围授权，不保证逐用户仓库权限或 Token 自动到期/撤销。

这些是现有脚本的定向回归，不会新建真实开发任务或启动真实 GitHub 监听。完整 Orca 编排仍依赖用户的 Agent、模型、仓库和本机环境；本 Release 不宣称已在所有组合上跑过完整流程。

## 来源与许可文件

LambdaTheory 原创部分适用根目录 [LICENSE](LICENSE) 的保留权利声明。各平台目录保留 Matt Pocock 套件的 `LICENSE.matt-pocock` 和 `html-plan/LICENSE`，第三方材料仍按其原许可处理，详见 [NOTICE.md](NOTICE.md)。
