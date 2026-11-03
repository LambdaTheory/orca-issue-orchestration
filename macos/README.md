# Orca Issue Orchestration · macos · v1.1.1

当前未发布源码包含完整 13 个 skills（html-plan 仅保留独立用途）（已发布 v1.1.1 包为 11 个，未更新），使用 bash / zsh 命令。主入口为 `skills/orca-issue-orchestration/SKILL.md`，命令参考为 `skills/orca-issue-orchestration/references/commands.md`。

## 工作流 Map 门禁

- 每次启动或恢复 `orca-issue-orchestration` 都必须调用同套件 wayfinder 并创建或恢复 MCP Map，不分大小、一轮可答或已有确认；复用当前业务工作流的同一张 Map，不是每轮消息或每个实现子 Issue 各建一图。
- 已有明确决定与授权复用为票内依据，不重复问、不虚构票、不跳过 Map。没有真实待决事项可以零票，Ticket / Answer 关联如实记为无，但 Map 标题、mapId 与 Revision 必须保留。
- 工具检查恒查 map_create / map_list / map_read / map_apply / map_wait 五项，缺任一项或服务失败即暂停，不提供 HTTP、GitHub 或本地替代图旁路。核对命令为 `<PY> -X utf8 <SKILL_DIR>/scripts/mcp_gate.py tools --tools <客户端工具列表>`；PY 按宿主选择实际 Python 3 解释器。
- gate 接收真实 map_read found envelope：revision.mapId 必须是非空字符串、revision.revision 必须是正整数，revision.state 必须存在，state.id 与 revision.mapId、state.currentRevision 与 revision.revision 分别一致。缺元数据、身份版本不一致、未知/缺失票状态、未决票或残留 Fog 均拒绝。
- 四项许可必须齐全：Map 相关票全部 settle 且无未决项或 Fog、PRD 同步、范围确认、实施授权。tickets 与 Fog 都为空的合法零票 Map 满足第一项，不要求造票；其余三项仍须为真。空 Frontier 本身不是完成证据。allow / proceed / accept / pass 退出码为 0，拒绝或暂停为 3，失效命令为参数错误。

## 安装与启动

1. 将本目录的 `skills/` 内容放入所用 Agent 支持的技能目录。已有同名文件时先比较，不直接覆盖本地修改。
2. 在 Orca 打开目标项目，确认 Python 3、Git、GitHub CLI 和目标 Agent 可用。页面自查另需 Node.js；截图使用已有 Playwright 与浏览器，未配置时如实说明。
3. 让 Agent 使用 `orca-issue-orchestration` 读取仓库事实和现有需求，再继续编排。

`ORCA_ISSUE_SKILL_DIR` 可覆盖主 skill 的安装目录；`ORCA_ISSUE_PYTHON` 可指定 Python 3 解释器。路径必须按当前执行宿主填写，安装目录与开发项目目录不是同一个概念。

需求澄清由 `wayfinder` 推进，`prd-writing` 整理统一 PRD / SPEC，`align-requirements` 展示对应版本并在对话或已有获准评论工具中收集意见；有效决定校验后更新回同一份正文，关键缺口回到 wayfinder。每次启动或恢复必须经同套件 wayfinder 创建或恢复 MCP Map；已有确认直接复用，不重复问、不虚构票、不跳过 Map，无真实待决可零票但不可免 Map。衔接见 `skills/orca-issue-orchestration/references/requirements.md`。

新任务暂按用户确认的分类核对模型，唯一分类表见 `skills/orca-issue-orchestration/references/models.md`；MCP 提供正式分组后移除临时分类。本端只读 OMC 目录与额度，不实现分组、排序、预约或自动替换，不改运行中的会话或全局模型。接入方式见命令参考。

按需读取参考文档，不把所有历史记录加载到协调者上下文。只按已有授权交付；首次安装不会自动创建 Run、Issue、订阅或开发任务。

## v1.1.1 升级说明

- watcher 凭据改为个人 message token：首次 `TW start` 在前台用 `gh auth token` 申领，仅发送到用户明确信任的 HTTPS 服务，凭据自动保存为按服务 URL hash 区分的本机 JSON；后台 serve/wait/ack 只读已保存凭据，不调用 `gh` 换 token。
- 旧共享 `--token-file` 接口停用；迁移旧监听不复用旧共享 token 或无 owner 的旧 consumer——请用户确认暂停旧托管，保留旧状态与 pending，在新状态目录建立个人订阅并完成对账后再继续。
- 首次运行失败给出可执行指引（`state.json` 缺失/损坏等不再报 cryptic 错误）；守护缺 `--coordinator`/`--run` 时明确拒绝启动。
- 466 项 Python 测试与 5 项 Node 测试通过；已在 macOS 对默认线上服务完成个人凭据、launchd、评论新增/编辑、pending/ACK、重启续接及受控停止验收。未在 Windows 实机复验 Windows 包内容。

## v1.1.0 升级说明

- 新增守护终端：编排开始后在独立终端常驻，遇到权限确认弹窗时按白名单放行并复核；越界、凭据、强推、越界删除一律挂起等人。所有判定写入 `audit.jsonl`，可 `pause` 急停。守护只处理确认弹窗和停摆提示，不重派任务、不代替需求决定。
- GitHub 监听改为任务级系统托管，`webhook_watch` 保留为历史回退；新增任务标签范围保护。
- 新增时长统计（`C` / `S timing` / `S finish` / `PT`）与每小时进展回报（`RP`）。
- 旧会话不会热加载新脚本；先让原协调者收口，再更新 skill 目录。已在运行的 Run 状态文件向前兼容。

## v1.0.3 升级说明

当前工作范围内还有未关闭的 Issue 时持续保留 GitHub watcher；处理批次并 ACK 后重挂，不能因长期验收或 PR 已交付停挂。GitHub 监听和 Orca worker 等待各自单实例，可并存。已有会话需由原协调者读取新规则并核对实际监听状态，不会自动热更新。

## Webhook 地址配置

Webhook 服务地址默认 `https://githubwebhook.madebynight.top`；需要时设置环境变量 `WATCH_URL` 覆盖。此前 v1.0.1 功能保持不变。

## v1.0.1 升级说明

等待器新增后台只读健康检查（默认间隔 60 秒、done/idle 宽限 120 秒、无可见进展阈值 900 秒）。正常等待不唤醒模型，异常输出 health_alert 交协调者核实，不自动重派。旧等待进程不会热加载；安全处理未确认消息后由原协调者切换，不能并挂两个等待者。

本版完成 macOS 本地脚本回归，未操作活跃编排任务；不宣称真实模型故障已经自动恢复。

## 许可文件

保留 `skills/LICENSE.matt-pocock` 与 `skills/html-plan/LICENSE`，上游来源见对应 skill。
