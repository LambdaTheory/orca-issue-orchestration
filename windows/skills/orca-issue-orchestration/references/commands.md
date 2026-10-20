# 命令参考

先解析本次实际位置，再使用下面的状态、等待和派发命令。`/code-review` 等是 Agent 命令，不是 Shell 命令。

## 位置变量（Windows：PowerShell）

先将占位符替换为本次实际值；环境变量覆盖不是必填配置。安装目录取被加载的 `SKILL.md` 所在位置，开发仓库与它可以完全不同。`PY` 是单个命令名或可执行文件路径，不拼接解释器参数。

```powershell
$SKILL_DIR = if ($env:ORCA_ISSUE_SKILL_DIR) { $env:ORCA_ISSUE_SKILL_DIR } else { '<本次加载的 Skill 绝对目录>' }
$PY = if ($env:ORCA_ISSUE_PYTHON) { $env:ORCA_ISSUE_PYTHON } else { 'python' }
$REPO = '<项目本地绝对路径>'
$RUN_ID = '<已有或刚创建的真实 Run ID>'
if (-not (Test-Path (Join-Path $SKILL_DIR 'scripts/orch_state.py'))) { throw 'Skill 路径无效' }
& $PY -V
function S { & $PY -X utf8 (Join-Path $SKILL_DIR 'scripts/orch_state.py') --repo $REPO --run $RUN_ID @args }
function RP { & $PY -X utf8 (Join-Path $SKILL_DIR 'scripts/orch_report.py') --repo $REPO --run $RUN_ID @args }
function C { & $PY -X utf8 (Join-Path $SKILL_DIR 'scripts/orch_clock.py') --repo $REPO @args }
function PT { & $PY -X utf8 (Join-Path $SKILL_DIR 'scripts/orch_pr_timing.py') --repo $REPO --run $RUN_ID @args }
```

使用原生 PowerShell 工具，不从 Bash 包装调用 PowerShell。只有旧 watcher 回退需要真实 Git Bash `bash.exe`，不要选 WSL 的同名入口。只读预检也可以用 S show；没有现有 Run 时先执行下节的创建步骤再设置 RUN_ID。


## 启动一次编排

先查持久化记录和 Orca 是否已有本任务 Run/Dispatch；恢复现有尝试，不重复建 Run。初始化通过、票据存在、派发已授权后，由协调者立即执行下列 Orca 命令启动可执行票。不能只输出待办清单、开普通子代理或说“下一步可派发”就结束。

以下代码块使用 PowerShell；`$RUN_ID = '<真实 ID>'`，其余 `S` 参数相同。

```text
orca orchestration run-create --objective "<项目> 实现" --json            # 得到 run_id
$RUN_ID = "<上一步真实返回的 Run ID>" # S 已在位置变量中定义
S init --objective "<项目> 实现" --max-parallel 1 --reviewers "<评审Agent>"
S set "<n>" stage=ready title="..." deps="..."                         # ready 或 blocked，每个 Issue 一行，含用户节点 Issue
S rule "<用户已定的、所有协调者必须遵守的约定>"
```

## 建票后的任务级 GitHub watcher

安装 Skill 或刚调用命令时不运行 watcher。需求分析、拆解、首批 Issue 创建且统一打上本任务专属标签后，才执行下列启动步骤；任务标签不是 `needs-user` 等状态标签。同名标签只有确认属于同一任务才复用。监听目录绑定本任务，创建开发 Run 后不要迁移或另开一份订阅。同一仓库＋任务标签必须复用同一目录；当前防重复实例限定在该目录，不会跨不同目录自动发现重复绑定。

系统托管的采集进程持续运行，`TW wait` 只是宿主管理的一次性本地通知桥。桥输出退出以触发后台完成通知，采集器不退出；pending 未确认时保持背压，不提前 ACK 或重复拉取下一批。`orch_wait.py` 另管 worker 消息，两者不能替代。

### 位置与首次启动


Windows PowerShell：

```powershell
$WATCH_DIR = Join-Path $REPO '.orchestration/<持久任务ID>'
$WATCH_REPO = '<已核实的owner/name>'
$TASK_LABEL = '<本任务专属标签>'
$WATCH_URL = '<已配置的webhook服务地址>'
$TOKEN_FILE = Join-Path $HOME '.config/orca-webhook/message-token'
function TW { & $PY -X utf8 (Join-Path $SKILL_DIR 'scripts/task_watch.py') --state-dir $WATCH_DIR @args }
TW start --repo $WATCH_REPO --repository-id <仓库数字ID> --label $TASK_LABEL --subscription-key '<稳定host-owner-name-task键>' --watch-url $WATCH_URL --token-file $TOKEN_FILE
```

先替换所有占位符。凭据文件由用户安全配置并限制读取权限；脚本只持久化文件路径，不将 token 写入系统任务定义、状态或日志。不要用 Read 等工具打开或打印凭据；系统服务不假定继承调用 Shell 的环境。

`start` 固定订阅边界并查询标签范围，不等于业务已对账。根据返回的对账版本，用 `gh` 读取当前全部任务 Issue、评论及关联 PR，处理建票至订阅启动之间可能遗漏的业务，再确认该版本。仅在采集就绪且对账确认后派发。无开发 Run 时沿用持久建票记录，不单独伪造 Run。

### 日常处理与新增票

以下是参数骨架，先替换占位符，字符串含空格时加引号；`wait` 必须交给宿主支持跨轮通知的后台工具。Shell 变量和函数不保证跨工具调用继承，每次调用须在同一 Shell 内重建，或直接使用自包含的绝对路径命令：

```powershell
& '<Python绝对路径>' -X utf8 '<Skill绝对目录>\scripts\task_watch.py' --state-dir '<任务状态绝对目录>' wait
```

以上作为宿主后台命令提交，保存真实后台 handle。不要假定新 Shell 中已有 `TW` 函数。

日常参数骨架：

```text
TW status --json
TW start                               # 已初始化后幂等恢复，保留 consumer/pending
TW reconcile --version <实际对账版本>   # 完成当前业务对账后才确认
TW wait                                # 后台通知桥；有批次/对账/故障才输出退出
TW ack --batch-id <实际batch_id>        # 业务处理、同步及持久化成功后确认
TW wait --after <已观察的通知版本>       # 续挂桥；故障通知去重，不代替业务 ACK
TW scope begin --op-id <稳定操作ID>     # 新增票/纳入既有票/需保护的同步收尾之前
# 执行已授权建票、加同一任务标签及状态同步，并核验远端真实结果
TW scope end --op-id <同一操作ID>       # 核验并记录成功后；未知/失败不得清除保护
TW stop                                # 正常结束，脚本检查完成条件并撤销本期托管
TW pause --reason "<用户明确暂停依据>"    # 仅明确用户授权，不冒充任务完成
```

- 每次被唤醒及进入等待前查 `status`；仅 `desired=running` 且采集异常死亡时优先 `start` 恢复并核实真实就绪。明确暂停或已停止的任务不得因唤醒自动恢复，先遵守用户授权状态。正常通知桥退出只需要处理消息并重挂桥，不重建 consumer。
- 范围以仓库＋任务标签动态分页查询为准，不只看初始票单。新增票先 scope begin，再建票/打标，scope end 后处理新的范围对账。结果未知保留操作标记，避免旧票归零与新票创建之间提前停机。已停止的任务须先 start 再追加票。
- 批次逐项读取真实 Issue/PR 当前内容，判断是否属于任务标签范围或关联 PR；无关仓库事件明确忽略后 ACK，不据此扩大范围。自身评论不重复回复。共享 GitHub 账号时结合评论内容、更新时间和既有记录识别用户答复，不只看作者。
- pending 代表尚未确认，不代表业务一定未执行；崩溃恢复后先核对远端结果，避免重复写操作。任何同步失败保留 pending 或 scope 保护，不将 `sync_pending` 只藏在文字备注里。
- 若桥返回 `quiescing`，先核对停止/暂停命令的结果，不机械重挂；上次命令中断且没有在途操作时，用 `stop` 接续收口或 `start` 恢复运行。`cleanup_pending` 表示停止意图已保存但系统任务撤销未确认，按原 `stop` / `pause` 重试收尾，不能假称已完全停止。
- 保存任务标签、绑定目录、真实系统任务标识、通知桥后台 handle、状态/阻断原因。`status` 的系统任务存活和业务对账就绪是不同事实，不把 `orch_wait.py` 当 GitHub 监听。

### 正常退出与平台边界

`TW stop` 重新完整查询任务标签，核实所有当前及已知票 CLOSED、无 scope 操作和未确认对账，暂停采集后再核对范围及 pending；任何未知/开放票/待处理消息都会拒绝停止并恢复监听。关闭不等于验收通过，按实际结果结算。不要用 kill、TaskStop、卸载系统任务或删除状态绕过接口，也不得提前关票换取停机。交接仅交接本地通知桥，不停止采集。stop 与 scope 操作在本地串行；外部人员不经 scope 协议同时建票/重开无法与本地停机形成原子事务，最终复查只能缩小窗口。任务停止后再次纳管必须先恢复 start。

| 平台 | 本任务运行期托管 | 限制 |
|---|---|---|
| macOS | 动态 launchd 用户任务 | 需可用 user/gui 域，不安装自动启动的 LaunchAgents |
| Linux | systemd 用户 transient 服务 | 需可用用户管理器；不自动提权或启用 linger，不保证最后登录退出后存活 |
| Windows | 当前用户交互登录计划任务 | 失败重启加周期触发补偿，已有实例不重复运行；无交互登录的纯 SSH 环境不保证可用 |

三端都只在建票后的 start 注册，正常 stop 撤销；Windows 恢复可能等下次周期触发，不承诺即时。任务残留触发必须服从已停止状态。权限/环境不满足时报 blocked，不静默退回易丢失的后台 Shell。旧 `gh_watch.sh` 保留用于历史诊断，不作为绕过标签校验和托管保护的常规回退；也不要直接并行运行底层 `webhook_watch.py wait` 争用 consumer。

系统托管不等于权限隔离：同权限进程仍可能强制卸载。机器睡眠、注销/重启和 Agent 会话结束不在自动接续保证内；宿主必须能接收通知桥完成消息，无法跨轮通知时明确报告阻断，不能承诺未来自动唤醒。

所有状态与 watcher 命令复用本页解析的位置变量，参数按当前 Shell 的规则传递。

## 时长统计

`C start` 在确定 `REPO` 后立即执行一次，记录本次 skill 的启动时间；重复执行保留仍在进行的最早时间，上一任务已结束后自动开始新时钟。
```powershell
C start                          # 首次持久化：记录 skill 启动
C show                           # 查看开始/结束；--json 输出原值
S timing                         # 当前总历时、有效工作、等待、阶段与单票周期
S timing --json                  # 机器可读结果
S finish --reason completed      # 收口：落完成时间、写 timing.md 并输出摘要
PT --pr <最终PR>                 # 回填最终 PR 的 ## 工作时长，保留其余正文
```

- `S init` 继承 `clock.json` 的 `started_at`；没有时钟时才以初始化时间为起点。`S finish` 写 `finished_at` 并结束时钟。
- `S set` 只在阶段真正变化时写 `stage_since` 与 `history`，重复设置同一阶段不追加；历史只追加，不改写。
- 总历时是墙钟时间；有效工作为开发、修复、评审、验证，等待为待派发、阻塞、等用户。并发任务按 Issue 累加，累计可能大于墙钟，不能混用。
- 没有阶段历史的票不猜时间，单票周期显示“未记录”；修复回环按实际阶段分段。
- 暂停或取消同样执行 `S finish --reason paused|cancelled`，保留墙钟；本期不扣除暂停区间。
- 最终 PR 取本次 Run 中 `mergedAt` 最晚的关联 PR；没有已合并 PR 时取最后交付的 PR；没有 PR 时不回填，保留 `timing.md` 并在最终报告说明。
- `PT` 必须在 `S finish` 后执行，按 PR 正文现有内容追加或替换 `## 工作时长`，编辑后回读确认恰好一个章节；失败时报告并保留 `timing.md`。
- `timing.md` 位于 `.orchestration/<run>/`（本地、已忽略）；长期留档需另行复制到项目报告目录。

## 每小时进展回报

`RP` 只读当前 Run 的 `state.json` 并输出进展；不改状态、不派发、不判断 worker 健康，也不替代 worker 等待、GitHub watcher 或健康检查。
```powershell
RP                # 立即输出一次
RP --wait 3600    # 作为宿主后台任务：等待 1 小时后输出，完成后唤醒协调者
```

- 首批派发后启动一个 `RP --wait <间隔秒>` 后台计时；收到完成通知后把 stdout 原样发到终端，再挂下一个。同一时间只保留一个计时任务，默认间隔 3600 秒。
- 只剩用户待答、评审等待或 PR 待合并时继续保留；`TW stop`、用户明确暂停或取消后停止计时，不补发历史小时报。
- 计时任务失败或宿主不支持后台完成通知时报告一次阻断并继续业务，不承诺未来自动唤醒；不要改成就地轮询。
- 报告固定包含：进度计数、正在开发、评审中、待合并、验收中、待派发、等用户、阻塞，以及存在时的下一步。没有活动任务时明确写“当前没有在开发、评审或待合并的任务”。

## 守护终端（guard）

首批 Issue 建好、`TW start` 之前拆出；边界与判定流程见[守护终端](guard.md)。守护只处理权限确认与停摆提示，不重派、不代替需求决定。


Windows PowerShell：

```powershell
$GUARD_DIR = Join-Path $REPO '.orchestration/<持久任务ID>/guard'
function GUARD { & $PY -X utf8 (Join-Path $SKILL_DIR 'scripts/orch_guard.py') --state-dir $GUARD_DIR @args }
$cmd = '"{0}" -X utf8 "{1}" --state-dir "{2}" --coordinator "{3}" --run "{4}" --repo "{5}" --workspace "{5}" --branch "{6}" --skill-dir "{7}" pane' -f $PY, (Join-Path $SKILL_DIR 'scripts/orch_guard.py'), $GUARD_DIR, $env:ORCA_TERMINAL_HANDLE, $RUN_ID, $REPO, '<工作分支>', (Join-Path $SKILL_DIR 'scripts')
orca terminal split --terminal $env:ORCA_TERMINAL_HANDLE --direction horizontal --command $cmd --json
```

协调者是 chat 没有终端 handle 时，用 `orca terminal create --worktree active --title "orchestration-guard" --command "<同一条命令>" --json` 起独立终端。

日常参数骨架：

```text
GUARD status --json                    # desired / fresh / open_holds / awaiting_agent
GUARD audit --tail 20                  # 最近的 prompt、hold、applied、nudge
GUARD verdict <hold_id> allow --reason "<依据>"   # 协调者判定流程内的挂起
GUARD pause --reason "<用户明确暂停依据>"          # 只观测不注入
GUARD resume
GUARD start                            # 进程退出后按原状态目录幂等恢复
GUARD stop                             # 与 TW stop 同轮；hold 保留待查
```

- 启动后先 `GUARD status --json` 确认 `fresh` 为真，再派发。守护写 `guard.json` 心跳；`fresh` 与"终端还在"是两件事。
- 每个 `health_alert` 里的 `guard_hold` 都要处理：读 `holds/<id>.json` 的 `tool`、`input`、`rule`，流程内的用 `verdict` 放行，其余保留给用户。不要绕过接口直接删 hold 文件。
- `--no-nudge` 关闭停摆提示，`--no-agent` 关闭待命 Agent，`--idle-seconds` / `--max-nudges` 调阈值；默认 300 秒、最多 3 次。
- 放行项不足时用项目配置的 `extra_allow` 正则补充（`--extra-allow <json>`），改动后重启守护生效。

## 命令骨架

以下为 Windows PowerShell 派发与 worker 等待骨架；变量及函数须在本次工具调用内初始化。

```text
orca orchestration run-create --objective "<项目> 实现" --json
# 开发（先保存派发说明与需求版本，stage 保持 ready；成功回执落盘后才设 dev）
orca orchestration worker-start --spec "$(cat dev.txt)" --task-title "T<n> 标题 (#n)" \
  --worktree new-top-level --repo "<实际 repo 选择器>" --name issue-<n>-<slug> --base-branch "<目标分支>" --agent "<开发Agent>" --run "$RUN_ID" --json
# 评审（worktree 选择器用 orca worktree list 得到的完整 id）
orca orchestration worker-start --spec "$(cat review.txt)" --worktree "id:<repoId>::<path>" --agent "<评审Agent>" --run "$RUN_ID" --json
# 修改：仅已授权保留且仍可复用时使用开发终端
orca orchestration worker-start --spec "$(cat fix.txt)" --terminal <开发者 handle> --worktree "id:<repoId>::<path>" --run "$RUN_ID" --json
# 否则在同 worktree 新派修复 worker（二选一，不复用已释放 handle）
orca orchestration worker-start --spec "$(cat fix.txt)" --agent "<开发Agent>" --worktree "id:<repoId>::<path>" --run "$RUN_ID" --json
# 等待：挂后台。空心跳在脚本里 ack，不会把 “You have 1 orchestration message” 送进模型。
# 退出码 0：普通 check JSON 或 kind=health_alert，先区分类型；非零是查询/执行错误。
# 普通批次处理完用真实 deliveryId 续等；health_alert 没有伪造的 deliveryId，处置方式见下文。
# 不要另开 check --wait；原批次未处理完不能 ack。
"$PY" -X utf8 "$SKILL_DIR/scripts/orch_wait.py" --run "$RUN_ID"   # run_in_background
orca orchestration reply --id <msg> --body "<答复>" --json
"$PY" -X utf8 "$SKILL_DIR/scripts/orch_wait.py" --run "$RUN_ID" --ack <delivery>   # run_in_background
orca orchestration worker-retain --dispatch <开发 dispatch>   # 仅用户授权保留已结算开发终端时执行；否则 worker-release
orca orchestration worker-release --dispatch <dispatch>       # 评审者完成后、或合并后释放
```

### Windows PowerShell 派发与 worker 等待骨架

替换占位符后执行；不沿用 POSIX 的 `\` 续行、`cat` 命令替换或解释器调用语法。先用位置变量小节初始化本次 Shell：

```powershell
orca orchestration run-create --objective '<项目> 实现' --json
$RUN_ID = '<上一步真实返回的 Run ID>'
S init --objective '<项目> 实现' --max-parallel 1 --reviewers '<评审Agent>'
$spec = Get-Content -Raw -Encoding UTF8 '<开发任务说明绝对路径>'
orca orchestration worker-start --spec $spec --task-title 'T<n> 标题 (#n)' --worktree new-top-level --repo '<实际 repo 选择器>' --name 'issue-<n>-<slug>' --base-branch '<目标分支>' --agent '<开发Agent>' --run $RUN_ID --json
$reviewSpec = Get-Content -Raw -Encoding UTF8 '<评审说明绝对路径>'
orca orchestration worker-start --spec $reviewSpec --worktree 'id:<repoId>::<path>' --agent '<评审Agent>' --run $RUN_ID --json
# 下面的长等待交给宿主后台工具；新 Shell 使用真实绝对路径，不依赖此前函数/变量
& $PY -X utf8 (Join-Path $SKILL_DIR 'scripts/orch_wait.py') --run $RUN_ID
& $PY -X utf8 (Join-Path $SKILL_DIR 'scripts/orch_wait.py') --run $RUN_ID --ack '<已处理的真实 deliveryId>'
```

其他单行 Orca 命令参数沿用上方骨架；文件正文一律 `Get-Content -Raw -Encoding UTF8` 作为一个参数。GitHub watcher 使用 `TW` 的独立任务级生命周期，不能用上述 worker 等待替代。

### 等待器健康检查与输出

`orch_wait.py` 默认启用健康检查，仍只占用一个 Orca 长等待者。启动建立观测基线，每轮检查后默认间隔 60 秒（`--health-interval`）；`--timeout-ms` 是单次长等待上限，实际会截短到下次检查时刻。存活心跳不会推迟检查，也不算业务进展。只读查询单次最多 30 秒、每轮预算 60 秒、最多 20 页；这些是查询边界，不是 worker 执行超时。慢查询会延后检测，超预算/不支持接口/缺少必要字段明确报告 `health_probe_failed`，不静默降级为无限等待。

- 当前 Run 活跃 Dispatch 的 done/idle 状态及可见进展持续不变，至少两次观测且达到 `--idle-grace`（默认 120 秒），报告 `turn_ended_without_report`。
- 状态/活动及可用输出时间均无变化达到 `--stale-after`（默认 900 秒），仅报告 `suspected_stall`；长计算、工具运行和不可见输出都可能触发，不等于 worker 死亡。明确正常长任务可在核实后调整阈值，不能假称一直在推进。
- 人工输入提示、已退出、状态未知、待收尾分别报告 `needs_input`、`worker_exited`、`worker_unverifiable`、`settlement_pending`。已结算且已释放/保留的历史 Dispatch 不触发异常；没有活动任务则返回 `no_active_workers`，让协调者决定结束或转用户等待。
- 健康提醒输出 `kind=health_alert`、Run/Task/Dispatch、观测事实及只读核查命令，不伪造完成事件。输出前做一次非长等待消息检查，真实业务批次优先原样返回。健康提醒上的 `ack_pending` 仅是已识别空心跳但尚未确认的 delivery；处理提醒后可在续等时带它确认。若非零错误带 `ack_pending`，确认请求结果可能未知，须先对账，不能盲目重发或把它当业务消息已处理的证明。
- 提醒后脚本退出一次，协调者必须核实并记录恢复/继续观察/阻断等决定，再决定是否重挂。观测基线只保存在当前进程，重挂会重新计时；不得反复重挂来掩盖同一异常。该机制不负责重启已退出的协调会话或宿主进程。
- 带 `--guard-dir` 时同一封 `health_alert` 还会报告 `guard_unavailable`（守护心跳超过 `--guard-stale-after`，默认 180 秒）和 `guard_hold`（有 `open` / `denied-by-agent` / `apply-failed` 的挂起）。它们只是观测，不新增长等待者、不自动重启守护；处置见[守护终端](guard.md)。

### 派发凭据与角色检查

- 首次启动核实实际可用 Agent、配置模型与初始化向导是否就绪；启动后核对回执中的实际 Agent/模型、输入接受与 turnStart，不把终端存在当作已开工。按回执区分 structured session 与 terminal：统一优先用 `worker-show` / `worker-read --source auto`，仅明确为 terminal 且 handle 支持时才使用 terminal 命令；不强行向 structured worker 发送终端按键。
- **当前任务内有限恢复**：503 / 断流先查看原 Task/Dispatch、持久报告及当前状态；已有完成报告先取回，不因最后一次接口错误重做。仅在当前 Orca 恢复指引允许重试且旧尝试已确认停止/失败后，同一任务最多自动重试 2 次，记录次数，用原 `--task`、`--retry-of` 及回执要求的参数恢复，不用 `--spec` 另建重复任务，不重建整套工作区或重跑已完成评审。等待超时、连接丢失或 `outcome_unknown` 不证明 worker 死亡，先对账；次数耗尽或配置不变的错误只报告阻断，保留进度。退避等待交给工具，不让模型轮询倒计时。
- 工具选错或参数不适用时按明确错误提示改用正确入口，先查帮助，不先要求用户改权限规则；真实权限拒绝不绕过，不把配置错误伪装成临时服务错误重试。

- 每票首次开发、每轮评审/修复都保存实际回执到 Run 目录，记录 Issue、需求确认版本、阶段/轮次、Task ID、Dispatch ID、实际 Agent、worktree、handle（若有）、报告位置及结果。`worker-start --spec` 创建新 Task/Dispatch；修复复用终端不等于复用 Task，禁止覆盖旧轮次记录。`S set` 可记录 `dev_task`、`dev_dispatch`、`dev_receipt` 等当前指针，历史回执另存。
- 只有权威启动回执确认 ready 且输入已接受才设置 `stage=dev` 并向用户报告“已派发”，同时提供票号、实际 Agent 与 Dispatch；普通子代理 ID、终端打开、写好 spec 都不是派发成功。失败或 `outcome_unknown` 记录真实回执与阻断，读取 `failedStage`、`residualResources` 和 Orca recovery 指引；未知状态先对账，不盲目重派，更不能自己接手实现。
- 若项目明确选择原生子代理作为唯一独立评审，它仍共用并发额度，不叠加 Orca 评审，也不再派生评审；它不具有 Orca Task/Dispatch，不能发送 Orca `worker_done`。协调者收取结果并记录 `review_head`、报告与完成状态；评审完成不表示实现任务已派发或完成。
- 收到 worker_done，先核对活跃 Task/Dispatch、outcome 和实际 diff/测试证据，再决定修复、验收或阻断；必须修改项交回 Orca 开发 worker，协调者只复现核验、不亲自修代码。
