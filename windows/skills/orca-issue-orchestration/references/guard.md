# 守护终端（guard）

从 SKILL.md 执行主线进入。守护解决一个具体问题：协调者或 worker 卡在**权限确认弹窗**上，整条编排停下来等人。它不改变编排协议，不代替用户做需求决定，也不重派任务。

> 平台：本文件启动示例为 Windows PowerShell；macOS / Linux 版本见 `macos/skills/orca-issue-orchestration/references/guard.md` 的 bash 示例。

## 为什么不是"派一个 Agent 去点批准"

权限弹窗没有跨进程批准接口。第二个 Agent 无法替第一个会话点"允许"。真实可用的只有两条通路：

| 通路 | 能做什么 | 生效时机 |
|---|---|---|
| 终端注入（本文件主体） | 读渲染后的屏幕，认确认弹窗，发对应数字键 | 立刻，当前会话也能用 |
| `PreToolUse` hook 返回 `permissionDecision: allow` | 在弹窗出现前放行 | 需会话启动前装好；改配置对当前会话无效 |

hook 那条路留在 `orch_policy.py hook`，是否安装由用户单独决定；本版默认只跑终端注入。

## 启动与收口

启动时机在执行主线第 3 步：首批 Issue 建好、`TW start` 之前。先有守卫再派发。

```powershell
$GUARD_DIR = Join-Path $REPO '.orchestration/<task>/guard'
$cmd = '"{0}" -X utf8 "{1}" --state-dir "{2}" --coordinator "{3}" --run "{4}" --repo "{5}" --workspace "{5}" --branch "{6}" --skill-dir "{7}" pane' -f $PY, (Join-Path $SKILL_DIR 'scripts/orch_guard.py'), $GUARD_DIR, $env:ORCA_TERMINAL_HANDLE, $RUN_ID, $REPO, '<工作分支>', (Join-Path $SKILL_DIR 'scripts')
& orca terminal split --terminal $env:ORCA_TERMINAL_HANDLE --direction horizontal --command $cmd
```

协调者是 chat 而不是终端时没有 `ORCA_TERMINAL_HANDLE`，改用 `& orca terminal create --worktree active --title 'orchestration-guard' --command $cmd` 起一个独立终端，其余参数相同。

收口与 `TW stop` 同轮执行 `GUARD stop`（GUARD 函数见命令参考的守护终端小节）。用户明确暂停时用 `GUARD pause --reason <依据>`，不冒充完成。

## 状态目录

| 路径 | 内容 |
|---|---|
| `guard.json` | desired、generation、pid、heartbeat_ts、nudges、启动参数 |
| `audit.jsonl` | 每次 prompt / hold / applied / nudge / verdict，一行一条 |
| `holds/<id>.json` | 挂起等待人或 Agent 的请求 |
| `verdicts/<id>.json` | 待命 Agent 写回的判定 |
| `seen.json` | 已处理过的弹窗签名，避免重复按键 |
| `PAUSED` | 存在即只观测不注入（急停） |

## 授权边界

判定顺序：静态解析命令（含 `&&`、`;`、`|`，全部子命令都要命中）→ 命中白名单放行 → 越界或无法确定则挂起。表只决定"能否无人放行"，不决定"是否该做"。

**自动放行**

- 只读：`Read`/`Grep`/`Glob`、`git status|log|diff|show|rev-parse`、`gh pr view|list|checks`、`orca` 查询类
- 仓库内写：`Edit`/`Write`（限 worktree）、`git add|commit|checkout -b|switch|restore <具体路径>`
- 交付：`git push` 到非受保护分支（不带 `--force`、`--delete`）、`gh pr create|edit|comment`、`gh issue create|edit|comment|close`
- 编排自身：`python3 -X utf8 <SKILL_DIR>/scripts/*.py`、`orca orchestration|terminal|worktree create` 等已列入 `ORCA_ACTIONS` 的子命令
- 测试构建：`pytest|ruff|eslint|tsc|vitest` 等，以及 `npm|pnpm|yarn` 的 `test` 和 `run test|lint|build|typecheck`
- **产物清理**：`rm -rf` 的目标解析后必须落在 worktree 内且路径含 `node_modules|dist|build|target|.next|coverage|.cache|tmp` 等产物名，或落在本次 Run 的 scratch 目录；`git clean -fdX`（只清忽略文件）

**挂起等用户**

- 未命中白名单的命令、`mcp__*`、`AskUserQuestion`（需求决定必须人答）
- 写入 worktree 外、`~/.ssh`、`~/.aws`、`~/.config/gh`、`.env*`
- `sudo`、`chmod`、`brew|apt|docker|launchctl`、`curl -o`、管道进 `sh|bash`
- `git push --force*`、`git push --delete`、推送到 `main|master|develop`、`git reset --hard`、`git clean -fd`、`git branch -D`、`git remote` 变更、`gh auth`、`gh pr merge`
- 删除目标越界（worktree 根、家目录、`/`）、含 `..`、含 `$`/通配等无法静态解析的路径
- 目标在工作区内但不像产物（如 `rm -rf src`）

新增放行项通过项目配置的 `extra_allow` 正则补充，不改脚本。

## 一次判定怎么走

1. `orca terminal show` 看到 `agentWait` 非空，或终端长时间无输出 —— 只是线索
2. `orca terminal read --screen` 确认屏幕上真有确认弹窗：命中提示词（`Confirm`/`Are you sure`/`需要你的许可` 等）且存在编号选项
3. 从 transcript 取最后一条**没有结果**的工具调用，得到工具名和参数
4. `orch_policy.decide` 判定
5. `allow`：解析选项，优先选纯 "Yes"（不是"don't ask again"），确认存在 No/Cancel 选项后才按键；发数字键 + 回车
6. 复核：重新读屏，弹窗消失才算成功；仍在则转挂起，绝不重试同一个键
7. `hold`：写 `holds/<id>.json` 并在守护终端打一行，不碰键盘

屏幕读不出编号选项、或只有 Yes 没有 No、或取不到 transcript，一律挂起。宁可等人，不盲发按键。

## 停摆提示

同时满足下面全部条件时，才向协调者终端发一次"继续推进编排"：

- Run 未 `finished`，且还有 Issue 不在 `merged|closed`
- **没有活跃 worker**（有 worker 在跑时，协调者停在那里等 `worker_done` 是正常状态）
- 没有 `user_gate` 节点、`state.json` 里没有未答问题（在等用户时不该催）
- 协调者没有待批的工具调用
- 终端静默超过 `--idle-seconds`（默认 300 秒）
- 未 `pause`

每次运行最多 3 次（`--max-nudges`），`--no-nudge` 关闭。这条只提示继续，不重派、不杀进程。

## 待命 Agent

`pane` 模式下，出现挂起且没有请求过 Agent 时，守护会起一个 `codebuddy -p` 会话，把 `holds/<id>.json` 交给它，要求它只写 `verdicts/<id>.json`（`allow|deny` + 理由），不执行被挂起的命令。判定 `allow` 后守护自己按键并复核；`deny` 的保持给用户。Agent 不可用时降级为纯人工，不阻塞守护循环。`--no-agent` 可关闭。

## 急停与恢复

- 急停：`GUARD pause --reason "<授权依据>"`，或直接在状态目录放 `PAUSED` 文件
- 恢复：`GUARD resume`
- 守护进程退出：`GUARD start` 幂等重启（写 `desired=running` 并后台拉起）；`status --json` 的 `fresh` 说明心跳是否还在
- `orch_wait.py --guard-dir <dir>` 会把 `guard_unavailable`（心跳过期）和 `guard_hold`（有未处理挂起）并入现有 `health_alert`，不新增第二个 Orca 长等待者

## 已知限制

- 弹窗布局会随版本变化。守护要求"提示词 + 编号选项 + 存在拒绝项"三者同时成立才按键，布局一改就转挂起，不会误按。
- 只覆盖能拿到 transcript 的 Agent（CodeBuddy、Claude 格式）。其他 CLI 的 worker 只能读屏，读不出待批内容就挂起。
- Orca 默认用 `--dangerously-skip-permissions` 起 CodeBuddy，多数动作本来就不弹窗；真正会卡住的是 HIGH/CRITICAL 级别的确认，守护针对的正是这批残留。
- 守护与协调者共用同一份权限，不能越过 Orca 的强制卸载，也不能恢复已经结束的 Agent 会话。
