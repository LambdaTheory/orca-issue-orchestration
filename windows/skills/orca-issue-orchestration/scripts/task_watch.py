#!/usr/bin/env python3
"""Task-label scoped watcher: on-demand system service plus a local notification bridge."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import stat
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
REQUIRED_ARGS = ("repo", "repository_id", "task_label", "subscription_key", "watch_url", "token_file")
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


def _read_token(path):
    info = os.stat(path)
    if not stat.S_ISREG(info.st_mode):
        raise TaskWatchError("凭据路径必须是普通文件")
    if os.name != "nt" and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
        raise TaskWatchError("凭据文件必须属于当前用户且仅本人可读写（如 chmod 600）")
    token = Path(path).read_text(encoding="utf-8").strip()
    if not token or "\n" in token or "\r" in token:
        raise TaskWatchError("凭据文件必须包含非空单行令牌")
    _SECRETS.add(token)
    return token


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
    binding["token_file"] = str(Path(binding["token_file"]).expanduser().resolve())
    gh = getattr(args, "gh_path", None) or old.get("gh_path") or shutil.which("gh")
    if not gh:
        raise TaskWatchError("未找到 gh，请配置 --gh-path")
    binding["gh_path"] = str(Path(gh).expanduser().resolve())
    if not os.path.isfile(binding["gh_path"]):
        raise TaskWatchError("gh 可执行文件不存在")
    binding["start"] = getattr(args, "start", None) or old.get("start", "now")
    if old and old != binding:
        raise TaskWatchError("状态目录已绑定其他参数，不能换仓库、任务、服务或凭据路径")
    return binding


def _runtime_fresh(runtime, generation):
    age = time.time() - runtime.get("heartbeat_ts", 0)
    return runtime.get("generation") == generation and 0 <= age < RUNTIME_FRESH_SECONDS


def _wait_ready(directory, service, binding, generation):
    deadline = time.monotonic() + READY_BUDGET_SECONDS
    while time.monotonic() < deadline:
        state = service.status(binding)
        runtime = _load_runtime(directory)
        if (state.get("running") and _runtime_fresh(runtime, generation) and
                runtime.get("desired_seen") == "running" and not runtime.get("quiesced")):
            return
        time.sleep(0.25)
    raise TaskWatchError("未收到当前代次的采集进程就绪回执；状态保留，可重试 start")


def cmd_start(args, directory):
    with _lock(directory, OPERATION_LOCK):
        control = _load_control(directory)
        binding = _resolve_binding(args, control)
        token = _read_token(binding["token_file"])
        service = TaskService(directory)
        managed = control.get("service_binding")
        if not managed:
            managed = service.prepare()
        if (managed.get("available") is False or managed.get("user_manager_available") is False or
                managed.get("interactive_available") is False):
            raise TaskWatchError("系统托管不可用: " + managed.get("detail", "请检查用户登录环境"))
        # 已运行时不与长轮询争 init 锁，也不重置代次或对账状态。
        if control.get("desired") == "running" and control.get("phase") == "ready":
            state = service.status(managed)
            runtime = _load_runtime(directory)
            if (state.get("running") and _runtime_fresh(runtime, control.get("generation")) and
                    not runtime.get("blocked_note")):
                print(json.dumps(collect_status(directory), ensure_ascii=False))
                return
        if query_repo_id(binding) != binding["repository_id"]:
            raise TaskWatchError("仓库数字 ID 与 owner/name 不匹配")
        entries = query_scope(binding)
        if not entries:
            raise TaskWatchError("任务标签下没有真实 Issue，请先完成建票并打标")
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
                         binding["subscription_key"], binding["start"])
        else:
            for key in ("watch_url", "repository_id", "subscription_key", "start"):
                if state.get(key) != binding[key]:
                    raise TaskWatchError("已有 webhook 订阅绑定不匹配: " + key)
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
    elif required:
        result.update(state="blocked", detail="须对账当前票据/评论并 reconcile --version %s" % scope.get("version"))
    elif runtime.get("blocked_note"):
        result.update(state="blocked", detail=runtime["blocked_note"])
    elif pending:
        result.update(state="processing", detail="未确认批次等待业务处理")
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
    scope = control.get("scope_check", {})
    if control.get("reconcile", {}).get("version") != scope.get("version"):
        return {"kind": "reconcile", "scope_version": scope.get("version"), "issues": scope.get("issues", [])}
    error = runtime.get("last_error", {})
    if error.get("version", 0) > after:
        return {"kind": "error", **error}
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
        ww.ack_impl(directory, args.batch_id, binding["watch_url"], _read_token(binding["token_file"]))


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
                response = ww.poll_once(binding["watch_url"], state["consumer_id"],
                                        _read_token(binding["token_file"]), wait_seconds=0)
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
            ww._check_server_binding(state, binding["watch_url"])
            response = ww.poll_once(binding["watch_url"], state["consumer_id"], _read_token(binding["token_file"]))
            got = ww.persist_pending(directory, state, response)
        runtime.pop("blocked_note", None)
        runtime.pop("last_error", None)
        runtime.update(retry_at=0, failures=0, last_poll_ts=time.time(), heartbeat_ts=time.time())
        _save_runtime(directory, runtime)
        return "consumed" if got else "idle"
    except (TaskWatchError, ww.WatcherError, OSError) as exc:
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
