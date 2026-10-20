# 变更记录

## v1.1.0 — 2026-10-20

- 新增守护终端（guard）：编排期间在独立终端常驻。协调者或 worker 停在权限确认弹窗时，守护读取渲染后的屏幕、按静态白名单判定，命中才按键并在按键后复核弹窗是否关闭；越界、凭据、强推、越界删除等一律挂起等人，不盲发按键。判定全程写入 `audit.jsonl`，支持 `pause` 急停与幂等重启。
- 授权边界集中在 `orch_policy.py`：产物清理（`node_modules`、`dist`、`build` 等）自动放行，删除源码目录、`git push --force`、`gh pr merge`、`gh auth` 等挂起；CodeBuddy 的 REPL 单条 Bash 调用按严格形态解包后再判定，形态不符即挂起。
- 停摆提示独立于权限放行：只有在没有活跃 worker、没有等用户节点、没有未答问题、没有待批调用且终端静默时才提示继续，最多 3 次，不重派、不杀进程。
- 新增时长统计：`C start/show`、`S timing`、`S finish`、`PT` 回填 PR 的「工作时长」章节；`state.json` 增加 `started_at`、`finished_at`、`stage_since`、`history`。
- 新增每小时进展回报（`RP`）：只读 `state.json`，不改状态、不派发、不判断 worker 健康。
- GitHub 监听改为任务级系统托管（launchd / systemd / 计划任务），`webhook_watch` 保留为历史回退；新增任务标签范围保护与对账版本。
- 新增 PR 规范（`references/pr.md`）与四类 Issue 模板（`templates/issues`）。
- 打包结构：双平台源码只差 `references/commands.md`，其余文件共享；新增 `orch_guard`、`orch_policy`、`orch_clock`、`orch_time`、`orch_report`、`orch_pr_timing` 及其测试。

验证边界：两平台源码的 11 个测试套件在 macOS 执行通过；守护的终端注入链路在真实 Orca 终端验证过「白名单放行」与「挂起后判定放行」两条路径。未在 Windows 实机复验 PowerShell 分支，未验证线上 webhook 自动续跑。不修改现有活动 Run/worker。

## v1.0.3 — 2026-10-19

- Windows / macOS 同步持续监听规则：当前工作范围内仍有未关闭或状态未知的 Issue，就保持 GitHub watcher；不因长期等待验收、PR 已交付或 worker 已释放主动停挂。
- 明确批次处理与 ACK 后重挂、监听状态检查点、部署中断恢复和交接接管；GitHub watcher 与 Orca worker 等待器各自单实例，可并存。
- 恢复默认 webhook 地址 `https://githubwebhook.madebynight.top`，仍允许通过 `WATCH_URL` 覆盖；撤回 v1.0.2 强制显式配置地址的要求。
- 更新双平台规则、配置模板与安装说明；保留 LambdaTheory 版权声明及全部许可文件。运行脚本、数据库和现有测试工作区不变。

验证边界：规则及命令示例修订，不代表既有会话已恢复监听；未进行 Windows 原生或线上 webhook 自动续跑验证。

## v1.0.2 — 2026-10-16

- 移除平台命令参考与项目配置模板中的真实 webhook 地址，改为占位示例。
- Windows / macOS 的 `W` 包装函数不再为 `WATCH_URL` 设置默认值；未配置时提示设置环境变量，不读取消息凭据或启动 watcher。
- 已配置时保留调用者提供的服务地址；v1.0.1 的运行脚本与功能不变。
- 更新版本文件、安装说明和两平台 Release 包；不改写 Git 历史或旧 Release。

## v1.0.1 — 2026-10-16

- 修复 worker 轮次异常结束但未发送完成事件时，协调者可能无限静默等待的问题。Windows/macOS 等待器统一加入当前 Run 的低频只读健康探测。
- 默认检查间隔 60 秒，done/idle 未结算宽限 120 秒，无可见进展 900 秒后提醒核查；心跳不阻碍检查，告警不自动判死、停止或重派。
- 健康告警前对账真实消息，保留未确认心跳 ACK；查询失败、人工输入、未知状态及待收尾分别报告。
- 两端同步协调规则、恢复说明与配置参数；Windows 保留 .cmd 测试启动器，并修正派发示例的 PowerShell 文本读取及续行写法。
- Windows 同步 webhook 初始化提示与同一评论编辑事件测试，消除 v1.0.0 的这项平台差异。
- 更新套件及平台版本、ZIP 附件和校验文件。旧等待进程不热加载，须由原协调者安全切换。

验证边界：两平台源码的现有脚本回归在 macOS 执行；未完成 Windows 原生运行复验、线上 webhook 编辑投递或真实故障自动恢复验证。不修改现有活动 Run/worker。

## v1.0.0 — 2026-10-16

首次发布 Windows 与 macOS 两套完整快照，各包含 11 个 skills。

- 提供初始化、HTML 需求确认、Issue 开发与独立评审的编排入口。
- 默认单次独立评审、串行模型任务、按需验证与证据复用。
- 保留安静等待、精简状态摘要和 webhook 消息接续能力。
- 统一 skill 安装路径与 Python 解释器变量，修正路径写死问题。
- 精简协调者入口，将细则拆成按需读取的参考文档。
- 分别保留 PowerShell 与 bash/zsh 命令，以及平台已有脚本修正。
- 发布 Windows / macOS ZIP 和 SHA-256 校验文件。

此版本是套件首次发布版本，不表示所有内含 skill 均为新开发，也不改写其上游版本或许可信息。
