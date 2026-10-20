# Orca Issue Orchestration Skill Suite

**当前版本：v1.1.0** · Windows / macOS 双平台 · 11 个项目级 skills

面向 Orca 的目标驱动开发套件：从仓库探查、需求确认和 GitHub Issue 开始，由协调者安排独立 Agent 开发、评审与修复，最终按授权交付。协调者不代替 worker 写业务实现。

## 版权与使用许可

**Copyright (c) 2026 LambdaTheory. All rights reserved.**

代码公开可读。**未经 LambdaTheory 事先书面许可，不得使用、修改或分发其享有版权的原创代码与文档。** 该部分不提供开源许可证，详见根目录 [LICENSE](LICENSE)。

第三方组件继续遵循各自已有的 MIT、Apache 等许可证；本声明不覆盖这些许可。来源与许可索引见 [NOTICE.md](NOTICE.md)。

## 版本与下载

- 套件版本由根目录 `VERSION` 记录；这是整个套件的版本，不改变内含第三方 skill 的上游版本号。
- 源码分别位于 `windows/skills/` 和 `macos/skills/`，按实际执行宿主选择，不能混装两份同名 skill。
- 两平台源码只差一份 `references/commands.md`（本平台命令参考）；其余文件完全相同，避免两份手工维护产生漂移。
- 对应版本正式发布后，可在本仓库 Releases 下载下列 ZIP；若尚无该版本 Release，请从源码安装。两个平台包均包含完整的 11 个 skills、平台说明和许可文件。
- `SHA256SUMS.txt` 用于核对下载文件；变更记录见 [CHANGELOG.md](CHANGELOG.md)。

| 执行宿主 | 源码 | Release 文件 |
|---|---|---|
| Windows | [`windows/skills`](windows/skills) | `orca-issue-orchestration-v1.1.0-windows.zip` |
| macOS | [`macos/skills`](macos/skills) | `orca-issue-orchestration-v1.1.0-macos.zip` |

## 核心流程

```text
探查已有事实 → 确认需求 → 创建/复用实现 Issue
→ Orca 开发 worker → 一次独立评审 → 必要修复与验证
→ 按授权交付 PR → 记录结果并收口
```

- 默认串行执行；开发、修复、评审及原生子代理共用模型并发额度，已有项目约定优先。
- 空超时和存活心跳由等待脚本处理，不反复唤醒协调模型；脚本默认每轮检查后间隔 60 秒只读检查 worker，done/idle 未结算持续 120 秒或无可见进展 900 秒时发出核查提醒，不自动判死或重派。
- 状态默认读取当前摘要，详细记录按需读取。
- 默认一次独立评审；复用同 SHA、环境与范围未变的有效测试证据，不默认重复全量测试或 build。
- 主入口保持简短；准备、命令、操作和恢复细则保留在 `references/`，进入相应阶段再读取。
- 默认交付 PR；合并和其他外部动作沿用用户明确授权。

## 套件内容

| Skill | 用途 |
|---|---|
| `orca-issue-orchestration` | 主入口：初始化、需求确认、GitHub Issue、Orca 开发与评审编排 |
| `html-plan` | 交互式 HTML 方案与中文需求确认 |
| `implement` | 按已确认的 SPEC / Issue 执行实现 |
| `code-review` | 独立的 Standards / Spec 双轴评审；编排主流程不默认叠加调用 |
| `codebase-design` | 模块接口与设计分析 |
| `domain-modeling` | 领域模型、CONTEXT 与 ADR |
| `prototype` | 针对具体设计问题制作验证原型 |
| `research` | 查证资料与技术事实 |
| `tdd` | 按测试驱动方式实施指定任务 |
| `wayfinder` | 复杂工作的决策拆解 |
| `setup-matt-pocock-skills` | 准备 tracker、标签及领域文档约定 |

独立的 `code-review` 仍提供双轴评审能力，但编排主流程默认只安排一次独立评审，不因此自动开启更多评审 Agent。

## 运行前提

- 执行宿主已安装、启动 Orca，`orca status --json` 可访问。按当前二进制返回的 `orca skills get orchestration` / `orca skills get orca-cli` 使用命令。
- Python 3、Git、GitHub CLI；GitHub CLI 已登录并有目标仓库的必要权限。
- Node.js 用于 HTML 方案打包和相关测试。
- Windows 使用 PowerShell；仅旧 GitHub watcher 回退需要真正的 Git Bash，不能选 WSL 的同名 `bash.exe`。
- macOS 使用 bash / zsh。非交互进程也必须能找到实际 Python 和其他命令。
- 用 webhook 监听 Issue 时按平台的命令参考准备消息凭据，需要时用 `WATCH_URL` 覆盖默认 webhook 服务地址；发布包不包含令牌，不会替你创建订阅或启动 watcher。

## 安装

以下安装与运行说明供已获相应许可的用户参考。

可以直接从本仓库使用，也可以将**所选平台**的 `skills/` 内容放到你的 Agent 支持的技能目录。下面以项目级 `.agents/skills/` 为例；若客户端不扫描此位置，请按客户端约定安装，不要假定复制后所有 Agent 都会自动发现。

首次安装前检查目标目录；下面的复制只用于新目录。已有同名 skills 时先比较再合并，避免覆盖本地修改。

### Windows（PowerShell）

```powershell
git clone https://github.com/LambdaTheory/orca-issue-orchestration.git
cd orca-issue-orchestration
git checkout v1.0.3
# 首次安装到目标项目；把路径替换为实际项目目录。
$Destination = "E:/your-project/.agents/skills"
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
Copy-Item -Path "./windows/skills/*" -Destination $Destination -Recurse
```

详细命令见 [Windows 命令参考](windows/skills/orca-issue-orchestration/references/commands.md)。

### macOS（bash / zsh）

```bash
git clone https://github.com/LambdaTheory/orca-issue-orchestration.git
cd orca-issue-orchestration
git checkout v1.0.3
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
node --test windows/skills/html-plan/runtime/pack.test.mjs
```

```bash
python3 -X utf8 -B -m unittest discover -s macos/skills/orca-issue-orchestration/scripts -p '*_test.py'
node --test macos/skills/html-plan/runtime/pack.test.mjs
```

v1.0.1 的两平台脚本均在 macOS 运行回归；Windows 原生 `.cmd` 启动、PowerShell 命令及实际 Orca 入口未在 Windows 实机复验，不能把这些结果视为 Windows 端到端认证。webhook 编辑事件使用本地模拟验证，未验证线上服务投递。

这些是现有脚本的定向回归，不会新建真实开发任务或启动真实 GitHub 监听。完整 Orca 编排仍依赖用户的 Agent、模型、仓库和本机环境；本 Release 不宣称已在所有组合上跑过完整流程。

## 来源与许可文件

LambdaTheory 原创部分适用根目录 [LICENSE](LICENSE) 的保留权利声明。各平台目录保留 Matt Pocock 套件的 `LICENSE.matt-pocock` 和 `html-plan/LICENSE`，第三方材料仍按其原许可处理，详见 [NOTICE.md](NOTICE.md)。
