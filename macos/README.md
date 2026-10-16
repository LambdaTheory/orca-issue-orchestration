# Orca Issue Orchestration · macos · v1.0.0

本平台包含完整 11 个 skills，使用 bash / zsh 命令。主入口为 `skills/orca-issue-orchestration/SKILL.md`，命令参考为 `skills/orca-issue-orchestration/references/commands.md`。

## 安装与启动

1. 将本目录的 `skills/` 内容放入所用 Agent 支持的技能目录。已有同名文件时先比较，不直接覆盖本地修改。
2. 在 Orca 打开目标项目，确认 Python 3、Git、GitHub CLI 和目标 Agent 可用。HTML 方案打包另需 Node.js。
3. 让 Agent 使用 `orca-issue-orchestration` 读取仓库事实和现有需求，再继续编排。

`ORCA_ISSUE_SKILL_DIR` 可覆盖主 skill 的安装目录；`ORCA_ISSUE_PYTHON` 可指定 Python 3 解释器。路径必须按当前执行宿主填写，安装目录与开发项目目录不是同一个概念。

按需读取参考文档，不把所有历史记录加载到协调者上下文。只按已有授权交付；首次安装不会自动创建 Run、Issue、订阅或开发任务。

## 许可文件

保留 `skills/LICENSE.matt-pocock` 与 `skills/html-plan/LICENSE`，上游来源见对应 skill。
