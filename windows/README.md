# Orca Issue Orchestration · windows · v1.1.1

本平台包含完整 11 个 skills，使用 PowerShell 命令。主入口为 `skills/orca-issue-orchestration/SKILL.md`，命令参考为 `skills/orca-issue-orchestration/references/commands.md`。

## 安装与启动

1. 将本目录的 `skills/` 内容放入所用 Agent 支持的技能目录。已有同名文件时先比较，不直接覆盖本地修改。
2. 在 Orca 打开目标项目，确认 Python 3、Git、GitHub CLI 和目标 Agent 可用。HTML 方案打包另需 Node.js。
3. 让 Agent 使用 `orca-issue-orchestration` 读取仓库事实和现有需求，再继续编排。

`ORCA_ISSUE_SKILL_DIR` 可覆盖主 skill 的安装目录；`ORCA_ISSUE_PYTHON` 可指定 Python 3 解释器。路径必须按当前执行宿主填写，安装目录与开发项目目录不是同一个概念。

按需读取参考文档，不把所有历史记录加载到协调者上下文。只按已有授权交付；首次安装不会自动创建 Run、Issue、订阅或开发任务。

## v1.1.1 升级说明

- watcher 凭据改为个人 message token：首次 `TW start` 在前台用 `gh auth token` 申领，仅发送到用户明确信任的 HTTPS 服务，凭据自动保存为按服务 URL hash 区分的本机 JSON；后台 serve/wait/ack 只读已保存凭据，不调用 `gh` 换 token。
- 旧共享 `--token-file` 接口停用；迁移旧监听不复用旧共享 token 或无 owner 的旧 consumer——请用户确认暂停旧托管，保留旧状态与 pending，在新状态目录建立个人订阅并完成对账后再继续。
- 首次运行失败给出可执行指引（`state.json` 缺失/损坏等不再报 cryptic 错误）；守护缺 `--coordinator`/`--run` 时明确拒绝启动。
- 本版 commands.md 为 Windows 裁剪的 PowerShell 文档（POSIX text 块与续行已去除）；该包的 466 项 Python 测试与 5 项 Node 测试在 macOS 执行通过。Windows 原生 PowerShell 分支与计划任务未实机复验，不宣称 Windows 端到端通过。

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

本版 Windows 源码回归运行于 macOS；保留 Windows .cmd 测试启动器和 PowerShell 文档，但 Windows 原生分支与真实 Orca 启动尚未实机复验。

## 许可文件

保留 `skills/LICENSE.matt-pocock` 与 `skills/html-plan/LICENSE`，上游来源见对应 skill。
