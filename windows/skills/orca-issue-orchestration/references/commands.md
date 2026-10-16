# 命令参考

先解析本次实际位置，再使用下面的状态、等待和派发命令。`/code-review` 等是 Agent 命令，不是 Shell 命令。

## 位置变量（Windows：PowerShell）

先将占位符替换为本次实际值；环境变量覆盖不是必填配置。安装目录取被加载的 `SKILL.md` 所在位置，开发仓库与它可以完全不同。`PY` 是单个命令名或可执行文件路径，不拼接解释器参数。

```powershell
$SKILL_DIR = if ($env:ORCA_ISSUE_SKILL_DIR) { $env:ORCA_ISSUE_SKILL_DIR } else { "<本次加载的 Skill 绝对目录>" }
$PY = if ($env:ORCA_ISSUE_PYTHON) { $env:ORCA_ISSUE_PYTHON } else { "python" }
$REPO = "<项目本地绝对路径>"       # 不是 GitHub owner/name
$RUN_ID = "<已有或刚创建的真实 Run ID>"
if (-not (Test-Path -LiteralPath "$SKILL_DIR/scripts/orch_state.py" -PathType Leaf)) { throw "Skill 安装目录不正确" }
& $PY -V
function S { & $PY -X utf8 "$SKILL_DIR/scripts/orch_state.py" --repo $REPO --run $RUN_ID @args }
```

使用原生 PowerShell 工具，不从 Bash 包装调用 PowerShell。只有旧 watcher 回退需要真实 Git Bash `bash.exe`，不要选 WSL 的同名入口。只读预检也可以用 `S show`；没有现有 Run 时先执行下节的创建步骤再设置 `RUN_ID`。

## 启动一次编排

先查持久化记录和 Orca 是否已有本任务 Run/Dispatch；恢复现有尝试，不重复建 Run。初始化通过、票据存在、派发已授权后，由协调者立即执行下列 Orca 命令启动可执行票。不能只输出待办清单、开普通子代理或说“下一步可派发”就结束。

```text
orca orchestration run-create --objective "<项目> 实现" --json            # 得到 run_id
$RUN_ID = "<上一步真实返回的 Run ID>" # S 已在位置变量中定义
S init --objective "<项目> 实现" --max-parallel 1 --reviewers "<评审Agent>"
S set "<n>" stage=ready title="..." deps="..."                         # ready 或 blocked，每个 Issue 一行，含用户节点 Issue
S rule "<用户已定的、所有协调者必须遵守的约定>"
```

等待策略：开发、修复和评审只等待 Orca 完成/问题事件，不并挂 GitHub watcher。只有需要 Issue 回复时才挂 webhook watcher（`webhook_watch.py`）；若只剩用户待答，不轮询 worker。开始等回复前，先执行 `W init` 固定订阅起点（服务端地址、仓库 ID、由 host/owner/name/Run 组成的稳定订阅键），再用 `gh` 读取相关 Issue 的当前评论和任务状态、处理遗漏，最后挂 `W wait`；校对的是 GitHub 当前业务状态，不是事件序号。`W wait` 返回批次后先做业务处理与状态记录，`W ack --batch-id <id>` 确认成功后再重新挂 `W wait` 续等；本地存在未确认批次时 `W wait` 只重放提示，先处理或 ack，不取新批次。等待期间不反复写“继续等待”的评论或 note；无新事件不唤醒协调模型。脚本须在支持后台任务的入口运行，避免前台工具超时杀掉等待者。连接故障只报告一次，保留进度，不自动反复创建新等待任务；故障恢复后再续接。`W` 正常退出（拿到或重放批次）表示变化，非零退出表示认证、网络或状态目录故障（退出码 3 认证、4 网络），不当成“没有变化”；状态目录绑定规范化后的服务端地址与订阅参数，不得更换服务端后复用旧状态目录。

```text
# PowerShell：使用上面已经解析的 $SKILL_DIR、$REPO、$RUN_ID
$WatchDir = Join-Path $REPO ".orchestration/$RUN_ID"
$WatchRepo = "<owner/name>"
function W {
    $previousToken = $env:MESSAGE_TOKEN
    $previousUrl = $env:WATCH_URL
    try {
        if (-not $env:MESSAGE_TOKEN) {
            $tokenFile = Join-Path $HOME ".config/orca-webhook/message-token"
            $env:MESSAGE_TOKEN = (Get-Content -LiteralPath $tokenFile -Raw -ErrorAction Stop).Trim()
        }
        if (-not $env:WATCH_URL) { $env:WATCH_URL = "https://githubwebhook.madebynight.top" }
        & $PY -X utf8 "$SKILL_DIR/scripts/webhook_watch.py" --state-dir $WatchDir @args
    } finally {
        $env:MESSAGE_TOKEN = $previousToken
        $env:WATCH_URL = $previousUrl
    }
}
W init --repository-id <仓库数字ID> --subscription-key "<host>-<owner/name>-<run_id>" # 先固定订阅起点；重复 init 幂等
# init 后用 gh 校对当前相关 Issue/PR 状态，处理遗漏，再挂后台等待
W wait                         # 返回批次后先处理业务并记录；未确认批次只重放，不取新批次
W ack --batch-id <batch_id>     # 确认成功后再挂 W wait；检查 $LASTEXITCODE，不把失败当无变化

# 旧脚本回退：先停止 W wait，同一 Run 不并挂两个 GitHub watcher
$env:WATCH_DIR = $WatchDir
$env:WATCH_REPO = $WatchRepo
$GitBash = "<实际 Git Bash bash.exe 的绝对路径>"
$LegacyWatcher = (Join-Path $SKILL_DIR "scripts/gh_watch.sh").Replace('\', '/')
& $GitBash $LegacyWatcher --resync # 先检查 absorbed 输出并处理夹带变化，再运行下一行
& $GitBash $LegacyWatcher
```

凭据文件仅供 `W` 内部读取并传给 watcher；不要用 Read 等工具打开、打印或写入任务状态。

协调者与用户共用同一个 GitHub 账号时，评论作者区分不了是谁发的：只看 `changed:` 行，再读评论正文判断是不是用户的回复。

所有状态与 watcher 命令复用本页解析的位置变量，参数按当前 Shell 的规则传递。

## 命令骨架

```text
orca orchestration run-create --objective "<项目> 实现" --json
# 开发（先保存派发说明与需求版本，stage 保持 ready；成功回执落盘后才设 dev）
orca orchestration worker-start --spec "$(cat dev.txt)" --task-title "T<n> 标题 (#n)" \
  --worktree new-top-level --repo "<实际 repo 选择器>" --name issue-<n>-<slug> --base-branch "<目标分支>" --agent "<开发Agent>" --run $RUN_ID --json
# 评审（worktree 选择器用 orca worktree list 得到的完整 id）
orca orchestration worker-start --spec "$(cat review.txt)" --worktree "id:<repoId>::<path>" --agent "<评审Agent>" --run $RUN_ID --json
# 修改：仅已授权保留且仍可复用时使用开发终端
orca orchestration worker-start --spec "$(cat fix.txt)" --terminal <开发者 handle> --worktree "id:<repoId>::<path>" --run $RUN_ID --json
# 否则在同 worktree 新派修复 worker（二选一，不复用已释放 handle）
orca orchestration worker-start --spec "$(cat fix.txt)" --agent "<开发Agent>" --worktree "id:<repoId>::<path>" --run $RUN_ID --json
# 等待：挂后台。空心跳在脚本里 ack，不会把 “You have 1 orchestration message” 送进模型。
# 退出码 0 时 stdout 是需要处理的那次 check JSON。处理完用其中的 deliveryId 重新挂上。
# 不要再另开 check --wait，也不要漏掉 --ack，否则同一批会立刻再交出来。
& $PY -X utf8 "$SKILL_DIR/scripts/orch_wait.py" --run $RUN_ID   # run_in_background
orca orchestration reply --id <msg> --body "<答复>" --json
& $PY -X utf8 "$SKILL_DIR/scripts/orch_wait.py" --run $RUN_ID --ack <delivery>   # run_in_background
orca orchestration worker-retain --dispatch <开发 dispatch>   # 仅用户授权保留已结算开发终端时执行；否则 worker-release
orca orchestration worker-release --dispatch <dispatch>       # 评审者完成后、或合并后释放
```

### 派发凭据与角色检查

- 首次启动核实实际可用 Agent、配置模型与初始化向导是否就绪；启动后核对回执中的实际 Agent/模型、输入接受与 turnStart，不把终端存在当作已开工。按回执区分 structured session 与 terminal：统一优先用 `worker-show` / `worker-read --source auto`，仅明确为 terminal 且 handle 支持时才使用 terminal 命令；不强行向 structured worker 发送终端按键。
- **当前任务内有限恢复**：503 / 断流先查看原 Task/Dispatch、持久报告及当前状态；已有完成报告先取回，不因最后一次接口错误重做。仅在当前 Orca 恢复指引允许重试且旧尝试已确认停止/失败后，同一任务最多自动重试 2 次，记录次数，用原 `--task`、`--retry-of` 及回执要求的参数恢复，不用 `--spec` 另建重复任务，不重建整套工作区或重跑已完成评审。等待超时、连接丢失或 `outcome_unknown` 不证明 worker 死亡，先对账；次数耗尽或配置不变的错误只报告阻断，保留进度。退避等待交给工具，不让模型轮询倒计时。
- 工具选错或参数不适用时按明确错误提示改用正确入口，先查帮助，不先要求用户改权限规则；真实权限拒绝不绕过，不把配置错误伪装成临时服务错误重试。

- 每票首次开发、每轮评审/修复都保存实际回执到 Run 目录，记录 Issue、需求确认版本、阶段/轮次、Task ID、Dispatch ID、实际 Agent、worktree、handle（若有）、报告位置及结果。`worker-start --spec` 创建新 Task/Dispatch；修复复用终端不等于复用 Task，禁止覆盖旧轮次记录。`S set` 可记录 `dev_task`、`dev_dispatch`、`dev_receipt` 等当前指针，历史回执另存。
- 只有权威启动回执确认 ready 且输入已接受才设置 `stage=dev` 并向用户报告“已派发”，同时提供票号、实际 Agent 与 Dispatch；普通子代理 ID、终端打开、写好 spec 都不是派发成功。失败或 `outcome_unknown` 记录真实回执与阻断，读取 `failedStage`、`residualResources` 和 Orca recovery 指引；未知状态先对账，不盲目重派，更不能自己接手实现。
- 若项目明确选择原生子代理作为唯一独立评审，它仍共用并发额度，不叠加 Orca 评审，也不再派生评审；它不具有 Orca Task/Dispatch，不能发送 Orca `worker_done`。协调者收取结果并记录 `review_head`、报告与完成状态；评审完成不表示实现任务已派发或完成。
- 收到 worker_done，先核对活跃 Task/Dispatch、outcome 和实际 diff/测试证据，再决定修复、验收或阻断；必须修改项交回 Orca 开发 worker，协调者只复现核验、不亲自修代码。
