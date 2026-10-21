#!/usr/bin/env python3
"""Task-label scoped watcher: on-demand system service plus a local notification bridge.

凭据: 使用 webhook_watch 的个人凭据 JSON（login --trust-server 申领）。
start 时凭据文件路径固化进绑定（默认为按服务 URL 哈希的用户级配置路径，
可 --credentials-file 覆盖），owner_github_user_id 一并落盘；绑定/状态不存
任何令牌。后台 serve 只读已保存凭据：不调用 gh、不重新签发、不读
MESSAGE_TOKEN；401/403 认证失败持久化 auth blocked，不再退避重试，由用户
在前台 login 后 start 恢复。旧 --token-file（原始共享令牌）接口已停用。
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.parse

import webhook_watch as ww
from task_watch_service import TaskService, ServiceError

CONTROL_FILE = "task-watch.json"
RUNTIME_FILE = "task-watch-runtime.json"
CONTROL_LOCK = "task-watch-control.lock"
OPERATION_LOCK = "task-watch-operation.lock"
SERVE_LOCK = "task-watch-serve.lock"
PER_PAGE = 100
GH_TIMEOUT = 30
READY_BUDGET_SECONDS = 180
STOP_BUDGET_SECONDS = 180
RUNTIME_FRESH_SECONDS = 180
SCOPE_REFRESH_SECONDS = 300
SERVE_INTERVAL = 2
BRIDGE_INTERVAL = 2
REQUIRED_ARGS = ("repo", "repository_id", "task_label", "subscription_key", "watch_url")
STABLE_BINDING_KEYS = REQUIRED_ARGS + ("gh_path", "credentials_file", "start")
_SECRETS = set()


class TaskWatchError(Exception):
    pass


def _safe(message):
    text = str(message)
    for secret in _SECRETS:
        text = text.replace(secret, "[redacted]")
    return text


def _load_control(directory):
    return ww._load_json(directory, CONTROL_FILE)


def _save_control(directory, control):
    ww._save_json(directory, CONTROL_FILE, control)


def _load_runtime(directory):
    return ww._load_json(directory, RUNTIME_FILE)


def _save_runtime(directory, runtime):
    ww._save_json(directory, RUNTIME_FILE, runtime)


@contextmanager
def _lock(directory, name):
    with ww._lock_file(directory, name):
        yield


def _owner_id(owner):
    return (owner or {}).get("github_user_id")


def _saved_credentials(binding):
    """后台/离线只读已保存个人凭据：绝不 gh、绝不签发、绝不读 MESSAGE_TOKEN。

    凭据 owner 与绑定 owner 不一致时拒绝（用前台 login/start 恢复）。
    """
    token, owner = ww.resolve_saved_credentials(
        binding["watch_url"], "采集", binding["credentials_file"])
    cred_id = _owner_id(owner)
    if not ww._valid_owner_id(cred_id):
        raise TaskWatchError("已保存凭据缺少合法 owner，请前台重新 login")
    bound_id = binding.get("owner_github_user_id")
    if bound_id and str(bound_id) != str(cred_id):
        raise TaskWatchError(
            "已保存凭据 owner(%s)与绑定 owner(%s)不一致，拒绝采集；请核对 "
            "login 使用的账号后重新 start" % (cred_id, bound_id))
    _SECRETS.add(token)
    return token, owner


def _check_current_credentials(binding):
    """无锁校验当前已保存凭据与绑定 owner 仍一致（只读）。

    凭据缺失同样直接认证错误：不等待后台稍后发现，绝不以假成功早退。
    """
    creds = ww.read_credentials_file(binding["credentials_file"],
                                     binding["watch_url"])
    if creds is None:
        raise TaskWatchError(
            "本地没有已保存的个人凭据（%s）：不能当作已运行；请前台运行 "
            "login --trust-server %s 恢复，或核对绑定的凭据路径"
            % (binding["credentials_file"], binding["watch_url"]))
    cred_id = _owner_id(creds.get("owner"))
    bound = binding.get("owner_github_user_id")
    if bound and str(cred_id) != str(bound):
        raise TaskWatchError(
            "当前已保存凭据 owner(%s)与绑定 owner(%s)不一致（个人凭据已被"
            "替换），不当作仍在监听；请按状态目录核对后 start 恢复"
            % (cred_id, bound))


def _run_gh(binding, endpoint, paginate=False):
    args = [binding["gh_path"], "api", "--method", "GET"]
    if paginate:
        args += ["--paginate", "--slurp"]
    args.append(endpoint)
    try:
        result = subprocess.run(args, capture_output=True, text=True,
                                encoding="utf-8", timeout=GH_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise TaskWatchError("GitHub 查询超时，状态未知") from exc
    if result.returncode:
        raise TaskWatchError("GitHub 查询失败: " + _safe(result.stderr[:400]))
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise TaskWatchError("GitHub 查询返回无效 JSON") from exc


def query_repo_id(binding):
    body = _run_gh(binding, "repos/" + binding["repo"])
    if not isinstance(body, dict) or type(body.get("id")) is not int:
        raise TaskWatchError("仓库响应缺少数字 ID")
    return str(body["id"])


def query_scope(binding):
    label = urllib.parse.quote(binding["task_label"], safe="")
    pages = _run_gh(binding, "repos/%s/issues?state=all&labels=%s&per_page=%s" %
                    (binding["repo"], label, PER_PAGE), paginate=True)
    if not isinstance(pages, list) or not pages or any(not isinstance(p, list) for p in pages):
        raise TaskWatchError("任务范围分页响应不完整")
    found = {}
    for page in pages:
        for item in page:
            if not isinstance(item, dict):
                raise TaskWatchError("任务范围包含无效条目")
            if "pull_request" in item:
                continue
            number, state = item.get("number"), item.get("state")
            if type(number) is not int or number <= 0 or state not in ("open", "closed"):
                raise TaskWatchError("Issue 编号或状态未知，不能判定完成")
            if number in found and found[number] != state:
                raise TaskWatchError("分页过程中 Issue 状态变化，请重试查询")
            found[number] = state
    return [{"number": n, "state": found[n]} for n in sorted(found)]


def query_issue_state(binding, number):
    item = _run_gh(binding, "repos/%s/issues/%d" % (binding["repo"], number))
    if (not isinstance(item, dict) or item.get("number") != number or
            "pull_request" in item or item.get("state") not in ("open", "closed")):
        raise TaskWatchError("已纳管票 #%s 状态未知" % number)
    return item["state"]


def _record_scope_locked(directory, control, entries):
    previous = control.get("scope_check", {})
    entries = sorted(entries, key=lambda item: item["number"])
    changed = entries != previous.get("issues")
    version = previous.get("version", 0) + int(changed)
    scope = {"issues": entries, "version": max(1, version), "ts": time.time()}
    control["scope_check"] = scope
    control["known_issues"] = sorted(set(control.get("known_issues", [])) |
                                      {item["number"] for item in entries})
    _save_control(directory, control)
    return scope


def _resolve_binding(args, control):
    old = control.get("binding", {})
    if old.get("token_file"):
        raise TaskWatchError(
            "检测到旧 --token-file 绑定：本版本已改用个人凭据 JSON"
            "（webhook_watch login --trust-server <服务地址> 申领），"
            "旧绑定不可复用；请用新状态目录重新 start。未确认批次与对账"
            "记录已保留，不会丢失")
    if getattr(args, "token_file", None):
        raise TaskWatchError(
            "--token-file（原始 MESSAGE_TOKEN 文件）是旧接口，已停用且不再读取；"
            "请先运行 webhook_watch login --trust-server <服务地址> 申领个人"
            "凭据，再用默认凭据路径或 --credentials-file 指定其 JSON 文件")
    binding = {}
    for key in REQUIRED_ARGS:
        value = getattr(args, key, None)
        value = old.get(key) if value is None else value
        if not isinstance(value, str) or not value.strip():
            raise TaskWatchError("首次 start 需要 --" + key.replace("_", "-"))
        binding[key] = value
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", binding["repo"]):
        raise TaskWatchError("--repo 必须为有效 owner/name")
    if not binding["repository_id"].isdigit():
        raise TaskWatchError("--repository-id 必须为数字")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", binding["task_label"]):
        raise TaskWatchError("任务标签必须为小写 kebab-case，不能含逗号或路径分隔符")
    if binding["task_label"] in ("enhancement", "bug", "needs-user", "gate"):
        raise TaskWatchError("不能用状态/类型标签作为任务标签")
    binding["watch_url"] = ww._check_base(binding["watch_url"])
    # 凭据文件：显式 --credentials-file 覆盖；缺省用按规范 URL 哈希的个人
    # 凭据默认路径（在前台固化进绑定，供后台 serve 只读复用）
    cred_file = getattr(args, "credentials_file", None) or old.get("credentials_file")
    binding["credentials_file"] = str(Path(
        cred_file or ww.default_credentials_file(binding["watch_url"])
    ).expanduser().resolve())
    gh = getattr(args, "gh_path", None) or old.get("gh_path") or shutil.which("gh")
    if not gh:
        raise TaskWatchError("未找到 gh，请配置 --gh-path")
    binding["gh_path"] = str(Path(gh).expanduser().resolve())
    if not os.path.isfile(binding["gh_path"]):
        raise TaskWatchError("gh 可执行文件不存在")
    binding["start"] = getattr(args, "start", None) or old.get("start", "now")
    for key in STABLE_BINDING_KEYS:
        if old.get(key) is not None and old.get(key) != binding[key]:
            raise TaskWatchError("状态目录已绑定其他参数，不能换仓库、任务、服务、gh 或凭据路径（%s）" % key)
    return binding


def _runtime_fresh(runtime, generation):
    age = time.time() - runtime.get("heartbeat_ts", 0)
    return runtime.get("generation") == generation and 0 <= age < RUNTIME_FRESH_SECONDS


def _wait_ready(directory, service, binding, generation):
    deadline = time.monotonic() + READY_BUDGET_SECONDS
    while time.monotonic() < deadline:
        state = service.status(binding)
        runtime = _load_runtime(directory)
        # 认证阻断是最明确失败信号：仅当前代次生效（上一代 401 残留
        # 不能把新的恢复 start 立即判失败），绝不等到 ready 才被抹掉
        if (runtime.get("generation") == generation
                and runtime.get("auth_blocked")):
            raise TaskWatchError(
                runtime.get("blocked_note") or
                "服务端认证失败(401/403)；请前台 login --trust-server 后重试 start")
        if (state.get("running") and _runtime_fresh(runtime, generation) and
                runtime.get("desired_seen") == "running" and not runtime.get("quiesced")):
            return
        time.sleep(0.25)
    raise TaskWatchError("未收到当前代次的采集进程就绪回执；状态保留，可重试 start")


def cmd_start(args, directory):
    with _lock(directory, OPERATION_LOCK):
        control = _load_control(directory)
        binding = _resolve_binding(args, control)
        # 共享令牌已停用：检测到 MESSAGE_TOKEN 即给升级提示（不静默采用）
        ww.reject_shared_token_env()
        # 旧 consumer 缺 schema/owner：拒绝复用（pending 保留不丢）
        old_state = ww._load_state(directory)
        ww._reject_legacy_state(old_state)
        service = TaskService(directory)
        managed = control.get("service_binding")
        if not managed:
            managed = service.prepare()
        if (managed.get("available") is False or managed.get("user_manager_available") is False or
                managed.get("interactive_available") is False):
            raise TaskWatchError("系统托管不可用: " + managed.get("detail", "请检查用户登录环境"))
        # 已运行时不与长轮询争 init 锁，也不重置代次或对账状态——但心跳
        # 可能已缓存旧结果：先只读校验当前凭据 owner 与绑定仍一致。
        if control.get("desired") == "running" and control.get("phase") == "ready":
            state = service.status(managed)
            runtime = _load_runtime(directory)
            if (state.get("running") and _runtime_fresh(runtime, control.get("generation")) and
                    not runtime.get("blocked_note")):
                _check_current_credentials(control.get("binding") or binding)
                print(json.dumps(collect_status(directory), ensure_ascii=False))
                return
        if query_repo_id(binding) != binding["repository_id"]:
            raise TaskWatchError("仓库数字 ID 与 owner/name 不匹配")
        entries = query_scope(binding)
        if not entries:
            raise TaskWatchError("任务标签下没有真实 Issue，请先完成建票并打标")
        # 凭据在前台解析/申领（缺省路径在此固化进绑定，供后台只读复用）：
        # 失败立刻报错，不落任何绑定/业务状态，也不当作已监听。
        token, owner = ww.claim_credentials(
            binding["watch_url"], trust_server=getattr(args, "trust_server", None),
            credentials_file=binding["credentials_file"])
        cred_id = _owner_id(owner)
        if not ww._valid_owner_id(cred_id):
            raise TaskWatchError(
                "个人凭据缺少合法 owner，请重新 login --trust-server <服务地址>")
        old_owner = control.get("binding", {}).get("owner_github_user_id")
        if old_owner and str(old_owner) != cred_id:
            raise TaskWatchError(
                "本次个人凭据 owner(%s)与原绑定 owner(%s)不一致；请用新状态目录"
                "重新 start（未确认批次已保留）" % (cred_id, old_owner))
        binding["owner_github_user_id"] = str(cred_id)
        # 写 control 之前先对既有 webhook 状态做稳定参数/owner 检查——
        # 无论 consumer 是否存在：CLI 先 init 失败留下的未完成订阅（已登记
        # 参数或 owner A）不得被另一凭据/参数的 start 锁死目录。
        pre_state = ww._load_state(directory)
        _reject_init_conflict(pre_state, binding, cred_id)
        with _lock(directory, CONTROL_LOCK):
            control = _load_control(directory)
            control.update(binding=binding, service_binding=managed, desired="running", phase="starting",
                           generation=control.get("generation", 0) + 1)
            control.pop("reconcile", None)
            control.pop("pause_reason", None)
            _record_scope_locked(directory, control, entries)
        state = ww._load_state(directory)
        if not state.get("consumer_id"):
            ww.init_impl(directory, binding["watch_url"], token, binding["repository_id"],
                         binding["subscription_key"], binding["start"],
                         credential_owner=owner)
        else:
            for key in ("watch_url", "repository_id", "subscription_key", "start"):
                if state.get(key) != binding[key]:
                    raise TaskWatchError("已有 webhook 订阅绑定不匹配: " + key)
            if str(state.get("owner_github_user_id")) != cred_id:
                raise TaskWatchError(
                    "已初始化状态 owner(%s)与本次凭据 owner(%s)不一致，拒绝复用；"
                    "请用新状态目录重新 start（未确认批次已保留）"
                    % (state.get("owner_github_user_id"), cred_id))
        # 固定订阅边界之后再查询一次；协调者还须读取正文/评论补齐历史。
        entries = query_scope(binding)
        if not entries:
            raise TaskWatchError("订阅初始化后任务标签范围为空，拒绝启动，请核对标签")
        with _lock(directory, CONTROL_LOCK):
            control = _load_control(directory)
            _record_scope_locked(directory, control, entries)
        service.ensure(managed)
        _wait_ready(directory, service, managed, control["generation"])
        with _lock(directory, CONTROL_LOCK):
            control = _load_control(directory)
            control["phase"] = "ready"
            _save_control(directory, control)
        print(json.dumps(collect_status(directory), ensure_ascii=False))


def _reject_init_conflict(state, binding, cred_id):
    """start 写 control 之前对既有 webhook 状态的稳定参数/owner 预检
    （不修改任何 control/reconcile/pending，先拒绝再动绑定）。"""
    ww._check_init_want(state, binding["watch_url"], binding["repository_id"],
                        binding["subscription_key"], binding["start"])
    prev_owner = state.get("owner_github_user_id")
    if ww._valid_owner_id(prev_owner) and str(prev_owner) != str(cred_id):
        raise TaskWatchError(
            "该状态目录已有 webhook 订阅登记 owner(%s)，与本次凭据 owner(%s)"
            "不一致，拒绝 start；请用新状态目录（未确认批次已保留，不会丢失）"
            % (prev_owner, cred_id))


def collect_status(directory):
    control, runtime = _load_control(directory), _load_runtime(directory)
    service = {"registered": False, "running": False, "detail": "尚未注册"}
    service_error = None
    if control.get("service_binding"):
        try:
            service = TaskService(directory).status(control["service_binding"])
        except ServiceError as exc:
            service_error = _safe(exc)
            service["detail"] = service_error
    scope = control.get("scope_check", {})
    reconcile = control.get("reconcile", {}).get("version")
    desired = control.get("desired", "stopped")
    pending = ww._load_state(directory).get("pending")
    required = bool(control.get("binding")) and reconcile != scope.get("version")
    result = {"state": "unbound", "desired": desired, "service": service,
              "generation": control.get("generation"), "scope_version": scope.get("version"),
              "reconcile_required": required, "known_issues": control.get("known_issues", []),
              "open_scope_ops": sorted(control.get("scope_ops", {})),
              "pending": pending, "last_error": runtime.get("last_error"),
              "auth_blocked": bool(runtime.get("auth_blocked")),
              "heartbeat_ts": runtime.get("heartbeat_ts"), "detail": "未绑定任务"}
    if not control.get("binding"):
        return result
    if desired in ("stopped", "paused"):
        result.update(state=desired, detail=control.get("pause_reason", "已停止"))
        if service_error or service.get("registered"):
            result.update(state="cleanup_pending", detail="停止意图已保存，系统任务尚需撤销/核验")
    elif desired == "quiescing":
        result.update(state="quiescing", detail="暂停采集以核验停止条件")
    elif service_error:
        result.update(state="blocked", detail=service_error)
    elif not service.get("running") or not _runtime_fresh(runtime, control.get("generation")):
        result.update(state="recovering", detail="采集进程未确认存活，优先 start 恢复")
    elif pending:
        result.update(state="processing", detail="未确认批次等待业务处理")
    elif runtime.get("blocked_note"):
        # 认证 blocked 等明确失败须优先呈现，不被 reconcile 遮蔽
        result.update(state="blocked", detail=runtime["blocked_note"])
    elif required:
        result.update(state="blocked", detail="须对账当前票据/评论并 reconcile --version %s" % scope.get("version"))
    else:
        result.update(state="running", detail="采集存活；通知桥需由宿主挂载")
    return result


def cmd_status(args, directory):
    print(json.dumps(collect_status(directory), ensure_ascii=False, indent=2))


def bridge_once(directory, after):
    control, runtime = _load_control(directory), _load_runtime(directory)
    if not control.get("binding"):
        raise TaskWatchError("未绑定任务，请先 start")
    if control.get("desired") == "quiescing":
        return {"kind": "quiescing",
                "message": "停止/暂停事务尚未结束；核对事务结果，失败后用 start 或 stop 接续，不机械重挂桥"}
    if control.get("desired") in ("stopped", "paused"):
        return {"kind": control["desired"]}
    pending = ww._load_state(directory).get("pending")
    if pending:
        return {"kind": "batch", "pending": pending}
    error = runtime.get("last_error", {})
    # 新错误（含认证 blocked）优先于 reconcile 提示，避免 scope 变化把 401 遮住
    if error.get("version", 0) > after:
        return {"kind": "error", **error}
    scope = control.get("scope_check", {})
    if control.get("reconcile", {}).get("version") != scope.get("version"):
        return {"kind": "reconcile", "scope_version": scope.get("version"), "issues": scope.get("issues", [])}
    if control.get("desired") == "running" and not _runtime_fresh(runtime, control.get("generation")):
        # 一个运行代次只报告一次失联，--after 不屏蔽未来的新业务批次。
        version = max(runtime.get("notice_version", 0) + 1, control.get("generation", 0) * 1000000)
        if version > after:
            return {"kind": "recovering", "version": version, "message": "采集心跳失联，请 status 并 start 恢复"}
    return None


def cmd_wait(args, directory):
    if args.after < 0:
        raise TaskWatchError("--after 不能为负数")
    while True:
        notice = bridge_once(directory, args.after)
        if notice:
            print(json.dumps(notice, ensure_ascii=False))
            return
        time.sleep(BRIDGE_INTERVAL)


def cmd_ack(args, directory):
    with _lock(directory, OPERATION_LOCK):
        control = _load_control(directory)
        binding = control.get("binding")
        if not binding:
            raise TaskWatchError("未绑定任务")
        token, owner = _saved_credentials(binding)
        ww.ack_impl(directory, args.batch_id, binding["watch_url"], token,
                    credential_owner=owner)


def cmd_reconcile(args, directory):
    with _lock(directory, OPERATION_LOCK), _lock(directory, CONTROL_LOCK):
        control = _load_control(directory)
        version = control.get("scope_check", {}).get("version")
        if not version or args.version != version:
            raise TaskWatchError("对账版本已变化，请读取当前范围和评论后确认新版本")
        control["reconcile"] = {"version": version, "ts": time.time()}
        _save_control(directory, control)
    print("已确认对账版本 %s" % version)


def cmd_scope(args, directory):
    if not args.op_id.strip():
        raise TaskWatchError("scope 操作 ID 不能为空")
    with _lock(directory, OPERATION_LOCK), _lock(directory, CONTROL_LOCK):
        control = _load_control(directory)
        if control.get("desired") != "running":
            raise TaskWatchError("任务未运行，须先 start 恢复再追加票")
        ops = control.setdefault("scope_ops", {})
        if args.scope_op == "begin":
            ops.setdefault(args.op_id, {"ts": time.time()})
        else:
            if args.op_id not in ops:
                raise TaskWatchError("未找到该 scope 操作，请核实原记录")
            entries = query_scope(control["binding"])
            _record_scope_locked(directory, control, entries)
            del ops[args.op_id]
        _save_control(directory, control)
    print("scope %s: %s" % (args.scope_op, args.op_id))


def _check_completion(control, entries):
    if control.get("scope_ops"):
        raise TaskWatchError("存在未完成的 scope/同步操作")
    scope = control.get("scope_check", {})
    if control.get("reconcile", {}).get("version") != scope.get("version"):
        raise TaskWatchError("范围有未确认的对账版本")
    if not control.get("known_issues"):
        raise TaskWatchError("没有已核实的任务票据，不能判定全部完成")
    if any(item["state"] != "closed" for item in entries):
        raise TaskWatchError("任务标签下仍有未关闭 Issue")
    missing = set(control["known_issues"]) - {item["number"] for item in entries}
    for number in sorted(missing):
        if query_issue_state(control["binding"], number) != "closed":
            raise TaskWatchError("已纳管但失去标签的 Issue #%s 仍未关闭" % number)


@contextmanager
def _wait_webhook_lock(directory):
    deadline = time.monotonic() + STOP_BUDGET_SECONDS
    # 只在进入锁时重试；不能把受保护业务的异常误当锁竞争重执行。
    while True:
        lock = ww._state_lock(directory)
        try:
            lock.__enter__()
            break
        except ww.LockBusy:
            if time.monotonic() >= deadline:
                raise TaskWatchError("无法取得采集事务锁，拒绝停止")
            time.sleep(0.25)
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def _finish_stop(directory, control):
    managed = control.get("service_binding")
    if managed:
        TaskService(directory).remove(managed)
    print("已撤销本任务系统托管；状态和未确认批次保留")


def cmd_stop(args, directory):
    with _lock(directory, OPERATION_LOCK):
        control = _load_control(directory)
        if not control.get("binding"):
            raise TaskWatchError("未绑定任务")
        if control.get("desired") == "stopped":
            _finish_stop(directory, control)
            return
        if control.get("desired") == "paused":
            raise TaskWatchError("用户已暂停；先按授权恢复并对账，才能判定正常完成")
        try:
            with _lock(directory, CONTROL_LOCK):
                control = _load_control(directory)
                entries = query_scope(control["binding"])
                _record_scope_locked(directory, control, entries)
                _check_completion(control, entries)
                control["desired"] = "quiescing"
                _save_control(directory, control)
            # 所有采集从此锁内重新读取 desired；拿到锁证明在途取批已落盘。
            with _wait_webhook_lock(directory):
                pending = ww._load_state(directory).get("pending")
                if pending:
                    raise TaskWatchError("仍有未确认批次，请处理后 ACK")
                binding = control["binding"]
                state = ww._load_state(directory)
                if not state.get("consumer_id"):
                    raise TaskWatchError("订阅初始化未完成，不能判定队列已清空")
                token, owner = _saved_credentials(binding)
                response = ww.poll_once(binding["watch_url"], state["consumer_id"],
                                        token, wait_seconds=0, state=state)
                if ww.persist_pending(directory, state, response):
                    raise TaskWatchError("远端还有积压批次，已保存 pending，请先处理")
                with _lock(directory, CONTROL_LOCK):
                    control = _load_control(directory)
                    entries = query_scope(binding)
                    _record_scope_locked(directory, control, entries)
                    _check_completion(control, entries)
                    control.update(desired="stopped", phase="finalized", stopped_ts=time.time())
                    _save_control(directory, control)
        except BaseException:
            with _lock(directory, CONTROL_LOCK):
                control = _load_control(directory)
                if control.get("desired") == "quiescing":
                    control["desired"] = "running"
                    _save_control(directory, control)
            raise
        _finish_stop(directory, control)


def cmd_pause(args, directory):
    if not args.reason.strip():
        raise TaskWatchError("暂停必须记录用户授权依据")
    with _lock(directory, OPERATION_LOCK):
        control = _load_control(directory)
        if not control.get("binding"):
            raise TaskWatchError("未绑定任务")
        if control.get("desired") in ("paused", "stopped"):
            _finish_stop(directory, control)
            return
        try:
            with _lock(directory, CONTROL_LOCK):
                control = _load_control(directory)
                control.update(desired="quiescing", pause_reason=args.reason)
                _save_control(directory, control)
            # 等在途批次保存后再撤销，保留 pending；pause 不冒充全部完成。
            with _wait_webhook_lock(directory), _lock(directory, CONTROL_LOCK):
                control = _load_control(directory)
                control.update(desired="paused", phase="paused")
                _save_control(directory, control)
        except BaseException:
            with _lock(directory, CONTROL_LOCK):
                control = _load_control(directory)
                if control.get("desired") == "quiescing":
                    control["desired"] = "running"
                    _save_control(directory, control)
            raise
        _finish_stop(directory, control)


def _record_error(runtime, message):
    message = _safe(message)
    if runtime.get("last_error", {}).get("message") != message:
        runtime["notice_version"] = runtime.get("notice_version", 0) + 1
    runtime["last_error"] = {"version": runtime.get("notice_version", 1), "message": message}
    runtime["blocked_note"] = message


def serve_step(directory):
    control = _load_control(directory)
    runtime = _load_runtime(directory)
    generation = control.get("generation", 0)
    if runtime.get("generation") != generation:
        runtime = {"notice_version": generation * 1000000}
    runtime.update(pid=os.getpid(), generation=generation, heartbeat_ts=time.time(),
                   desired_seen=control.get("desired"), quiesced=False)
    desired = control.get("desired", "stopped")
    if desired in ("stopped", "paused"):
        runtime["quiesced"] = True
        _save_runtime(directory, runtime)
        return "stopped"
    if desired == "quiescing":
        runtime["quiesced"] = True
        _save_runtime(directory, runtime)
        return "quiesced"
    _save_runtime(directory, runtime)
    # 认证阻断是持久状态：每轮直接呈现 blocked（跨时钟跳跃/进程重启不自动
    # 恢复，不靠超长定时器模拟）；新代次（start 成功 bump generation）丢弃
    if runtime.get("auth_blocked"):
        return "blocked"
    if time.time() < runtime.get("retry_at", 0):
        return "backoff"
    try:
        scope = control.get("scope_check", {})
        if time.time() - scope.get("ts", 0) >= SCOPE_REFRESH_SECONDS:
            entries = query_scope(control["binding"])
            try:
                with _lock(directory, CONTROL_LOCK):
                    latest = _load_control(directory)
                    if (latest.get("desired") == "running" and
                            latest.get("generation") == control.get("generation") and
                            latest.get("scope_check") == scope):
                        _record_scope_locked(directory, latest, entries)
            except ww.LockBusy:
                return "busy"
        control = _load_control(directory)
        if control.get("desired") != "running":
            return "quiesced"
        scope = control.get("scope_check", {})
        if control.get("reconcile", {}).get("version") != scope.get("version"):
            return "reconcile"
        with ww._state_lock(directory):
            control = _load_control(directory)
            if control.get("desired") != "running":
                return "quiesced"
            if control.get("reconcile", {}).get("version") != control.get("scope_check", {}).get("version"):
                return "reconcile"
            binding = control["binding"]
            state = ww._load_state(directory)
            if state.get("pending"):
                return "backpressure"
            if not state.get("consumer_id"):
                raise TaskWatchError("订阅未初始化，请 start 恢复")
            ww._reject_legacy_state(state)
            ww._check_server_binding(state, binding["watch_url"])
            # 后台只读已保存凭据：不 gh 重签、不用 env 兜底；缺凭据即报错
            # 并持久化 blocked（绝不降级为已监听）
            token, owner = _saved_credentials(binding)
            response = ww.poll_once(binding["watch_url"], state["consumer_id"],
                                    token, state=state)
            got = ww.persist_pending(directory, state, response)
        runtime.pop("blocked_note", None)
        runtime.pop("last_error", None)
        runtime.pop("auth_blocked", None)
        runtime.update(retry_at=0, failures=0, last_poll_ts=time.time(), heartbeat_ts=time.time())
        _save_runtime(directory, runtime)
        return "consumed" if got else "idle"
    except (TaskWatchError, ww.WatcherError, OSError) as exc:
        if isinstance(exc, ww.WatcherError) and getattr(exc, "http_auth", False):
            # 服务端 401/403：持久化 auth blocked（不退避重试、不后台重签
            # 发）；恢复只由前台 login --trust-server 后 start（新代次丢弃
            # 旧 runtime）触发
            note = ("认证失败(401/403)：后台已停止重试，也不会重新签发凭据；"
                    "请在前台运行 login --trust-server %s --credentials-file "
                    "<绑定路径> 申领个人 Token 后 task_watch start 恢复"
                    % control.get("binding", {}).get("watch_url", "<服务地址>"))
            _record_error(runtime, note)
            runtime.update(failures=runtime.get("failures", 0) + 1,
                           auth_blocked=True, retry_at=0,
                           heartbeat_ts=time.time())
            _save_runtime(directory, runtime)
            return "blocked"
        _record_error(runtime, exc)
        failures = runtime.get("failures", 0) + 1
        runtime.update(failures=failures, retry_at=time.time() + min(300, 10 * 2 ** min(failures - 1, 5)),
                       heartbeat_ts=time.time())
        _save_runtime(directory, runtime)
        return "error"


def cmd_serve(args, directory):
    with _lock(directory, SERVE_LOCK):
        while True:
            if serve_step(directory) == "stopped":
                return
            time.sleep(SERVE_INTERVAL)


def _build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", dest="state_dir_main")
    sub = parser.add_subparsers(dest="command", required=True)
    handlers = {"start": cmd_start, "status": cmd_status, "wait": cmd_wait,
                "ack": cmd_ack, "reconcile": cmd_reconcile, "scope": cmd_scope,
                "stop": cmd_stop, "pause": cmd_pause, "serve": cmd_serve}
    for name, handler in handlers.items():
        p = sub.add_parser(name)
        p.add_argument("--state-dir", dest="state_dir_sub")
        p.set_defaults(func=handler)
        if name == "start":
            for key in REQUIRED_ARGS:
                flags = ["--" + key.replace("_", "-")]
                if key == "task_label":
                    flags.append("--label")
                p.add_argument(*flags, dest=key)
            p.add_argument("--credentials-file",
                           help="个人凭据 JSON 覆盖路径（缺省为按服务 URL 哈希的用户级配置路径）")
            p.add_argument("--trust-server",
                           help="首次签发个人凭据的显式确认（规范化后须与服务地址完全一致）")
            p.add_argument("--token-file", help=argparse.SUPPRESS)  # 旧接口：给出迁移指引后报错
            p.add_argument("--gh-path")
            p.add_argument("--start", choices=("now", "earliest_retained"))
        elif name == "status":
            p.add_argument("--json", action="store_true")
        elif name == "wait":
            p.add_argument("--after", type=int, default=0)
        elif name == "ack":
            p.add_argument("--batch-id", required=True)
        elif name == "reconcile":
            p.add_argument("--version", type=int, required=True)
        elif name == "scope":
            p.add_argument("scope_op", choices=("begin", "end"))
            p.add_argument("--op-id", required=True)
        elif name == "pause":
            p.add_argument("--reason", required=True)
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    try:
        directory = args.state_dir_sub or args.state_dir_main or os.environ.get("WATCH_DIR")
        if not directory:
            raise TaskWatchError("请提供 --state-dir")
        directory = str(Path(directory).expanduser().resolve())
        if args.command == "start":
            os.makedirs(directory, mode=0o700, exist_ok=True)
        elif not os.path.isdir(directory):
            raise TaskWatchError("状态目录不存在，请先 start")
        args.func(args, directory)
        return 0
    except KeyboardInterrupt:
        print("已中断；任务状态保留。", file=sys.stderr)
        return 130
    except (TaskWatchError, ww.WatcherError, ServiceError, OSError) as exc:
        print("错误: " + _safe(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
