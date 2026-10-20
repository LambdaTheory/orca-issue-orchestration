# 恢复与经验

从 SKILL.md 的阶段导航进入，只读取当前相关小节。下列模板与脚本路径均以已解析的 `SKILL_DIR` 为基准；`S` / `TW` 等命令定义见[命令参考](commands.md)。

经验中的 APFS、`cp -c` 和 `ln -s` 仅适用于对应平台；其他宿主沿用项目已定方式。

## 经验与坑

- **空等待安静，但保留健康检查**：`orch_wait.py` 在空超时后继续等待，存活心跳在脚本内确认；脚本低频只读检查当前 Run 的 worker，异常才输出 `health_alert` 唤醒协调者，不靠模型定时轮询。真实事件交出后处理整批并带 `--ack` 续等；健康提醒先按下节核实再处置。连接或执行错误只报告一次并处理原因，不自动反复创建等待任务。只有用户待答时按用户节点等待，不轮询 worker。
- **每个 Run 的 Orca 消息通道只能有一个长等待者**（不排斥独立的 GitHub watcher）：`orch_wait.py` 已经占着 `--wait`。再挂会报 `waiter_exists`。它在跑时不要另开 `check --wait`；不带 `--wait` 的 `check` 仍可用来处理已经交出来的那一批。
- **空心跳不进模型**：脚本把 `heartbeat` 放进 `--types`，Orca 把心跳交给这个等待者，不再往会话里插 “You have 1 orchestration message”。只有 `subject` 为 `alive` 且正文为空的心跳会在脚本里 ack。带进度的心跳、`worker_done`、`escalation`、`question` 会让脚本退出，把整批 check JSON 交给协调者。
- **Grok 首次进入仓库**会弹“是否信任此目录”，派发失败为 `agent-trust-workspace`；在该终端确认后用 `--retry-of` 重试。
- **cursor-agent 的信任提示**可能阻止任务送达。先用 `worker-read --source auto` 及回执核实；仅实际 terminal 模式才在该终端按真实提示处理信任向导，structured session 使用其支持的交互入口。只有原尝试已确认失败且恢复指引允许时，才用原 `--task` 与 `--retry-of`、正确的 Agent/工作区参数恢复，不因屏幕看似空闲盲目重派。
- **Codex 更新提示**会吞掉回车（手动起的终端）；worker-start 发送后核对 `turnStart: observed`。worker-start 也会因此失败为 `agent_readiness` / `agent-update-prompt`：在该终端先发送 `3`（Skip until next version），再单独发一次回车（`--text 3 --enter` 合在一起不生效），再用 `worker-start --task <原 task_id> --retry-of <dispatch> --terminal <原终端> --worktree <原 worktree>` 重试（不能用 `--spec`）。
- **并行任务接口漂移**：两个任务各自写死另一模块的字符串，合并后对不上。评审时要求“与已合并 <目标分支> 合并后再测”，任务说明要求引用常量。
- **新依赖**：系统环境缺包会让验收误判失败，统一用项目约定的测试环境。
- **评审意见常抓到真问题**（状态机越界、重复入队、合并后接口不一致），“必须修改”交付前要核对有效修复证据；仅证据不足或仍有疑点时亲自补充复现，不重复同 SHA 的有效测试。
- **合并会影响用户进行中数据的工具前**（如用户正在用的标注页），把用户数据复制到临时目录，用新代码读取并比对状态计数、确认不写文件，再合并；验证后删除副本。
- **需要人工判断的数据（测试集预期等）**：可让两个不同模型独立标注 → 脚本比对 → 分歧项把双方标注与理由互相给看、各自再判一次 → 共识作为候选导入、剩余分歧交用户裁决，用户只看分歧。若数据是私有的而标注者是线上模型，必须先得到用户对具体范围的明确同意，并把例外记录进项目的隐私决策；结果只留本机，以“候选”导入，不覆盖用户已确认的标注。
- **网络抖动**：`git pull` / `gh` 偶发 SSL 错误会让 `&&` 链静默中断，检查当前分支和输出后重试，不要假设成功。
- 列目录不要依赖交互别名：POSIX 脚本里用 `/bin/ls`，PowerShell 用 `Get-ChildItem -LiteralPath`，Git Bash 脚本需要时用 `command ls`。
- 状态只放在 `.orchestration/` 文件里（见上文），不要依赖 `/tmp` 笔记或对话记忆。
- **worktree 缺私有数据和端口**：被 Git 忽略的数据（数据库、照片）在新 worktree 里不存在，应用启动会悄悄建一个空库。在仓库根放 `orca.yaml` 的 `scripts.setup`，用 `$ORCA_ROOT_PATH` / `$ORCA_WORKTREE_PATH` 做数据快照（`cp -c -R` 走 APFS 写时复制，需要跨卷或普通复制时改用 `cp -R`）、关联项目测试环境（符号链接用 `ln -s`）、在被忽略的文件里分配固定端口；任务说明写明只连本 worktree 端口，禁止连主服务端口。
- **完成信号用持久事件**：开发与评审使用 `worker-start` 的 `worker_done`、question / escalation 和归档报告，不依赖终端屏幕标记，不定时唤醒协调模型。GitHub 是独立通道，当前工作有未关闭 Issue 就保持监听，包括开发评审期和长期用户等待；不能用 Orca worker 等待器替代 GitHub watcher。
- **收到 worker_done 后的固定收尾**：先核对当前 Dispatch 的完成报告。开发或修复完成后，有即时后续任务则按 Orca 机制复用；需要等待时仅用户授权才 `worker-retain`，否则 `worker-release`，后续修复在同 worktree 用 `--agent` 新派，不复用已释放 handle；评审完成后 `worker-release`，仅在释放成功且终端仍存在时关闭该评审终端，不执行 worktree 级 `--all`。具体关闭参数先查当前 Orca 帮助。PR 合并成功后才结算剩余 Dispatch、关闭该 worktree 全部终端、归档并删除 worktree。release 失败或状态未知时遵循 Orca 恢复指引，不能用关闭终端代替释放。
- **任务说明要区分失败层级**：「失败时跳过继续」要写明只适用于单条记录的冲突，全局故障（锁库、磁盘满）应停止并以非零码退出，否则开发者和评审都会按字面执行。
- **agent 自带的看图工具会把图片发到它的云端模型**：私有照片不允许外发时，要在任务说明里明确禁止，只写「只送本机模型」不够。
- **`agent_session_ownership_unknown`**：先核对 worker 类型和当前所有权，按回执/帮助选择正确入口；不能靠缩短文本或重复发送绕过所有权错误。任务说明过长时可以保存在 worker 可读的文件里，用正确派发入口引用。
- **在 <目标分支> 上直接提交的其他会话**会让 PR 反复冲突，开工前确认还有谁在动 <目标分支> 的热点文件。
- **权限确认会静默卡住整条编排**：协调者或 worker 停在确认弹窗上时不会报错，只会没有进展。用守护终端处理，不要靠反复重挂等待或猜终端状态。注意 Orca 默认以 `--dangerously-skip-permissions` 起 CodeBuddy，真正会卡住的是 HIGH/CRITICAL 级别的确认，别把"没弹窗"当成守护失效。
- **终端注入不能盲发**：屏幕解析要同时看到提示词、编号选项和拒绝项才按键，发完必须复核弹窗是否消失。布局变化时转挂起是正确行为，不是故障。

## 健康提醒后的处理

1. `health_alert` 表示需要检查，不是 Orca 的失败/完成回执。核对当前 Run、Task、Dispatch 与状态文件；先检查已有完整消息和完成报告，再用提醒中的只读命令核实。`live` 仅说明存活，`done` 仅说明活动轮次结束，都不表示任务已经完成。
2. 对 `turn_ended_without_report`（如模型截断后未上报），通过 `worker-read --source auto` 获取可用输出，结合版本、覆盖范围和实际任务状态确认是否仍在执行/重试；终端尾屏、无输出或缺失信息不能单独证明已停止。已有结果先取回，不重做。确有正面证据表明原执行结束后，遵循当前 Orca recovery 指引结算或恢复原 Task/worktree，仍最多自动重试 2 次，不新建重复任务，不替活 worker 伪造 worker_done。
3. `suspected_stall` 可能只是长任务，核对正在运行的工具与最近真实进展；确认正常则记下依据和继续观察阈值再挂等待。`needs_input` 按真实问题处理；缺少 `agentWait` 字段不能当作没有等待。`worker_unverifiable`、`worker_missing` 或查询故障保留不确定性并报告，不能据此 stop/release/retry。
4. `settlement_pending` 核实正式结算及完成报告后再释放/收尾。`no_active_workers` 查有无待派任务，停止无用的 worker 等待器；它不表示当前 Issue 全部关闭。只要当前工作仍有开放票就保持/重挂 GitHub watcher，全部关闭才按命令参考收口。
5. 同一异常本轮只记录一次处置结果和下一步；未处理根因不机械重挂。需要暂停时明确告知用户阻断，不能继续声称“正在开发”。按命令参考处理 `ack_pending`；告警不授权任何生命周期变更。
6. `guard_hold`：读 `holds/<id>.json` 的 `tool`、`input`、`rule`、`reason`。属于本次编排流程的（产物清理、本分支推送、建票评论等）用 `GUARD verdict <id> allow --reason "<依据>"` 放行；不属于的保留给用户，不放行也不删文件。`apply-failed` 说明键已发出但弹窗仍在，先看终端实际状态再决定，不要重复放行。
7. `guard_unavailable`：先看 `$GUARD_DIR/guard.log` 和 `status --json`；守护进程死亡时用原状态目录 `GUARD start` 幂等恢复并核实 `fresh`，再处理业务。守护停止不等于编排完成，`TW` 采集照常。`PAUSED` 存在时不要因唤醒自动 `resume`，先服从用户暂停授权。

## GitHub watcher 恢复

- `TW wait` 是本地通知桥，交付批次后退出属于正常行为；系统托管的 `serve` 应继续存在。先区分桥退出与采集死亡，不把 Orca `health_alert` 当成关闭 GitHub 监听的依据。
- 每次恢复执行或进入等待前检查 `TW status --json`；当前任务仍活跃而采集死亡时，立即用原状态目录 `TW start` 恢复，核实真实就绪并处理恢复对账，再挂一个通知桥。不要重建 consumer、删 pending 或从 now 重置边界。
- 网络故障由采集器退避恢复；认证、状态绑定和权限错误记录 blocked，纠正具体原因后恢复。不能循环建立新任务、反复刷同一报错，也不能无有效监听却称“等你 Issue 回复”。
- macOS 需要可用的 launchd 用户域；Linux 需要可用的 systemd 用户管理器，不自动启用 linger；Windows 默认需要可用的交互登录和自身计划任务权限。失败时报告真实限制，不自行提权、保存登录密码或改用脱离 shell 的假托管。不承诺注销/重启后接续或唤醒已结束的 Agent 会话。
- `scope` 操作未结束、初始/恢复对账未确认、批次未 ACK、查询失败或标签票仍开放都会阻止正常停止。先解决对应事实，不能删除控制状态或提前关票来绕过。未知的建票结果先按任务标签及内容核对，再结束原 scope 操作。
- 用户明确暂停/取消时才调用 `TW pause --reason <授权依据>`；这不代表任务已完成。恢复使用同目录 `TW start`，保留范围、consumer 和未确认批次。

## 交接

1. 选干净的点：没有处于“已提问未回复”的 worker，刚合并完一批。
2. `S note "交接：进行中…；下一步…"` 与 `S event "交接：A → B"`。
3. 与新协调者确认接管责任后停止自己的 Orca 长等待及本地 `TW wait` 通知桥，不停止任务级系统托管采集。保留两种通道未处理批次；新协调者复用原 watcher 状态目录、consumer 和任务标签，检查 `TW status --json`，必要时 `TW start` 恢复后接管一个通知桥，不并挂重复实例。不能以交接为由先卸载采集器。
4. 在 Orca 开新协调者终端，任务说明写明：读本 skill、`orca skills get orchestration`、项目 AGENTS.md / 编排配置 / SPEC、`S show`（单票详情与历史按需读取）；执行 `orca orchestration run-use --id <run_id>`；处理积压消息后挂等待；按 state.json 的 rules 继续；先向用户报告接手情况。
5. 告诉用户以后到哪个终端看进展。
