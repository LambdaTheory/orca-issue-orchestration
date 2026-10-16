# Orca Issue Orchestration · macos · v1.0.2

本平台包含完整 11 个 skills，使用 bash / zsh 命令。主入口为 `skills/orca-issue-orchestration/SKILL.md`，命令参考为 `skills/orca-issue-orchestration/references/commands.md`。

## 安装与启动

1. 将本目录的 `skills/` 内容放入所用 Agent 支持的技能目录。已有同名文件时先比较，不直接覆盖本地修改。
2. 在 Orca 打开目标项目，确认 Python 3、Git、GitHub CLI 和目标 Agent 可用。HTML 方案打包另需 Node.js。
3. 让 Agent 使用 `orca-issue-orchestration` 读取仓库事实和现有需求，再继续编排。

`ORCA_ISSUE_SKILL_DIR` 可覆盖主 skill 的安装目录；`ORCA_ISSUE_PYTHON` 可指定 Python 3 解释器。路径必须按当前执行宿主填写，安装目录与开发项目目录不是同一个概念。

按需读取参考文档，不把所有历史记录加载到协调者上下文。只按已有授权交付；首次安装不会自动创建 Run、Issue、订阅或开发任务。

## v1.0.2 升级说明

Webhook 服务地址无默认值；使用前先设置 `WATCH_URL`，将 `https://<your-webhook-host>` 替换为实际服务地址。未设置时 `W` 提示配置，不启动 watcher。此前 v1.0.1 功能保持不变。

## v1.0.1 升级说明

等待器新增后台只读健康检查（默认间隔 60 秒、done/idle 宽限 120 秒、无可见进展阈值 900 秒）。正常等待不唤醒模型，异常输出 health_alert 交协调者核实，不自动重派。旧等待进程不会热加载；安全处理未确认消息后由原协调者切换，不能并挂两个等待者。

本版完成 macOS 本地脚本回归，未操作活跃编排任务；不宣称真实模型故障已经自动恢复。

## 许可文件

保留 `skills/LICENSE.matt-pocock` 与 `skills/html-plan/LICENSE`，上游来源见对应 skill。
