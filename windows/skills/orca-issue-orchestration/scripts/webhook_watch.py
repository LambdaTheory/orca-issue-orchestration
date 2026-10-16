#!/usr/bin/env python3
"""webhook_watch —— GitHub webhook 消费者 CLI（替代旧 gh_watch.sh）。

子命令:
    init --repository-id <id> --subscription-key <key> [--start now|earliest_retained]
    wait
    ack --batch-id <id>

通用选项:
    --state-dir <dir>   状态目录，可放在子命令前或后（也可用环境变量 WATCH_DIR）

环境变量:
    MESSAGE_TOKEN   与团队共享的服务 Bearer 令牌（不会写入状态文件，也不会打印）
    WATCH_URL       服务基地址，如 https://example.com（开发环境允许 http://127.0.0.1:port）
    WATCH_DIR       默认状态目录

典型用法:
    export MESSAGE_TOKEN=... WATCH_URL=https://example.com WATCH_DIR=.run
    python client/webhook_watch.py init --repository-id 1001 --subscription-key my-run
    python client/webhook_watch.py wait        # 拿到事件即退出，不自动 ack
    python client/webhook_watch.py wait        # 有 pending 时只重放提示
    python client/webhook_watch.py ack --batch-id <id>

行为要点:
    - init 先把稳定参数写入 webhook-watch.json 再调用服务，失败重试不会重复登记参数；
      已成功初始化后再次 init 直接跳过（幂等，不重复创建 consumer）。
    - init 输出 initial boundary 提示：该序号为订阅时刻的服务端边界，
      首次读取 GitHub 后需人工校对（详见迁移文档，无额外确认文件）。
    - wait 本地存在未确认批次时只重放提示（先处理/ack，不取新批次）；
      空批次在内部继续长轮询，直到拿到批次后：先原子落盘 pending，再打印
      changed 摘要与 batch_id 提示，退出码 0。不自动 ack。
    - ack 必须与本地 pending 的 batch_id 一致；服务确认成功后才原子清除
      pending。失败（含响应丢失后重试失败）保留 pending，可再次 ack 恢复。
    - 状态只有一个 webhook-watch.json（不用 state.json，避免覆盖状态目录里
      旧 Skill 的同名文件且不含令牌），经 os.replace 原子写入；状态目录用
      OS 级锁防止同一 watcher 并发运行。状态绑定规范化后的 WATCH_URL，
      换服务端须新状态目录，不得拿旧 consumer 继续使用。
    - 仅 loopback 允许 http，其余必须 https；证书默认校验；拒绝跟随
      HTTP 重定向（避免把令牌发往其他主机）；任何输出不含令牌。
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager

# ---- 固定参数（如需调整可直接改这里，做成可配置属冗余设计） ----
POLL_WAIT_SECONDS = 25      # 服务端长轮询等待秒数
REQUEST_TIMEOUT = 30        # 普通请求超时（秒）
RETRY_ATTEMPTS = 3          # 网络错误重试次数（含首次共 3 次）
RETRY_SLEEP = 1.0           # 重试间隔（秒）

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_AUTH = 3
EXIT_NETWORK = 4
EXIT_INTERRUPTED = 130

STATE_FILE = "webhook-watch.json"   # 不用 state.json：状态目录属旧 Skill，避免覆盖
LOCK_FILE = "webhook-watch.lock"


class WatcherError(Exception):
    """带退出码的业务错误。"""

    def __init__(self, message: str, exit_code: int = EXIT_ERROR):
        super().__init__(message)
        self.exit_code = exit_code


# ---------------------------------------------------------------- 状态文件

def _load_state(state_dir: str) -> dict:
    path = os.path.join(state_dir, STATE_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except ValueError as exc:
        raise WatcherError("%s 损坏无法解析（%s），请人工修复后重试"
                           % (path, exc))


def _save_state(state_dir: str, state: dict) -> None:
    """原子写入：先写临时文件再 os.replace，失败时旧状态保持完整。"""
    final = os.path.join(state_dir, STATE_FILE)
    tmp = final + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, final)


@contextmanager
def _state_lock(state_dir: str):
    """OS 级排他锁，防止同一状态目录被两个 watcher 同时使用。"""
    path = os.path.join(state_dir, LOCK_FILE)
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                locked = True
            except OSError:
                raise WatcherError(
                    "另一个 watcher 正在使用该状态目录（%s 被占用）" % LOCK_FILE)
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                raise WatcherError(
                    "另一个 watcher 正在使用该状态目录（%s 被占用）" % LOCK_FILE)
        yield
    finally:
        if locked and os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(fd)


# ---------------------------------------------------------------- HTTP

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """拒绝跟随重定向，避免把 Authorization 头发往其他主机。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_base(base: str) -> str:
    """校验并规范化服务基址（规范化结果会绑定进状态文件）。"""
    base = (base or "").rstrip("/")
    if not base:
        raise WatcherError("未设置 WATCH_URL 环境变量")
    # 报错不带原始 URL，避免用户名/密码等凭据进入输出
    parsed = urllib.parse.urlparse(base)
    if parsed.username or parsed.password:
        raise WatcherError("WATCH_URL 不应包含用户名/密码（凭据不能进 URL）")
    if parsed.params or parsed.query or parsed.fragment:
        raise WatcherError("WATCH_URL 为纯 origin，不应包含 query/fragment")
    if not parsed.hostname:
        raise WatcherError("WATCH_URL 缺少主机名")
    if parsed.scheme == "https":
        return base
    if parsed.scheme == "http":
        if _is_loopback_host(parsed.hostname.lower()):
            return base  # 仅 loopback 允许 http（开发用途）
        raise WatcherError("仅 loopback 允许 http，其余地址必须使用 https")
    raise WatcherError("WATCH_URL 仅支持 http(loopback) 或 https")


def _check_server_binding(state: dict, base: str) -> None:
    bound = state.get("watch_url")
    if bound and bound != base:
        raise WatcherError(
            "状态目录已绑定其他服务端（%s），请勿更换 WATCH_URL 复用旧 consumer" % bound)


def _token() -> str:
    token = os.environ.get("MESSAGE_TOKEN", "")
    if not token:
        raise WatcherError("未设置 MESSAGE_TOKEN 环境变量", EXIT_AUTH)
    return token


def _api_request(method: str, url: str, token: str,
                 body: dict | None = None, timeout: int = REQUEST_TIMEOUT) -> dict:
    """发起 JSON API 请求。网络/服务端错误短重试；401/403 不重试。"""
    data = None
    headers = {"Accept": "application/json", "Authorization": "Bearer " + token}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    last_error: WatcherError | None = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                message = json.loads(raw).get("error", {}).get("message") or raw
            except ValueError:
                message = raw
            if 300 <= exc.code < 400:
                # redirect_request 已被禁用，会以 HTTPError 形式抛出
                raise WatcherError(
                    "服务返回重定向 HTTP %d，为避免令牌外泄已拒绝跟随: %s"
                    % (exc.code, message))
            if exc.code in (401, 403):
                raise WatcherError(
                    "认证失败 HTTP %d: %s" % (exc.code, message), EXIT_AUTH)
            if exc.code < 500:
                raise WatcherError(
                    "请求被拒绝 HTTP %d: %s" % (exc.code, message))
            last_error = WatcherError(
                "服务端错误 HTTP %d: %s" % (exc.code, message), EXIT_NETWORK)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last_error = WatcherError("网络错误: %s" % exc, EXIT_NETWORK)
        if attempt < RETRY_ATTEMPTS:
            time.sleep(RETRY_SLEEP)
    raise last_error  # type: ignore[misc]


def _consumer_url(base: str, consumer_id, suffix: str = "") -> str:
    return "%s/v1/consumers/%s%s" % (
        base, urllib.parse.quote(str(consumer_id), safe=""), suffix)


# ---------------------------------------------------------------- 输出

def _print_batch(events: list, batch_id: str, replay: bool) -> None:
    if replay:
        print("(本地存在未确认批次，重放提示；处理后请用同一状态目录 ack)")
    for ev in events:
        subject = ev.get("subject") or {}
        repo = ev.get("repository") or ("repository_id=%s" % ev.get("repository_id"))
        line = "changed: %s %s%s %s/%s" % (
            repo, subject.get("kind") or "item", subject.get("number", ""),
            ev.get("event"), ev.get("action"))
        extra = []
        if ev.get("actor"):
            extra.append("by " + str(ev["actor"]))
        if ev.get("comment_id") is not None:
            extra.append("comment_id=" + str(ev["comment_id"]))
        if ev.get("head_sha"):
            extra.append("head_sha=" + str(ev["head_sha"]))
        if extra:
            line += " (" + ", ".join(extra) + ")"
        print(line)
    print("batch_id: %s（确认运行: ack --batch-id %s）" % (batch_id, batch_id))


def _print_init_hint(state: dict) -> None:
    boundary = state.get("initial_boundary_seq")
    if boundary is not None:
        print("提示: initial boundary seq=%s 是订阅时刻的服务端边界，"
              "首次读取 GitHub 后需人工校对其正确性（详见迁移文档）。" % boundary)


# ---------------------------------------------------------------- 子命令

def cmd_init(args, state_dir: str) -> None:
    token = _token()
    base = _check_base(os.environ.get("WATCH_URL", ""))
    with _state_lock(state_dir):
        state = _load_state(state_dir)
        want = {"subscription_key": args.subscription_key,
                "repository_id": args.repository_id,  # 十进制字符串
                "start": args.start,
                "watch_url": base}
        if state.get("subscription_key") is not None \
                or state.get("watch_url") is not None:
            # 已登记过稳定参数（含响应丢失后重试）：所有参数必须一致
            for key, val in want.items():
                have = state.get(key)
                if have is not None and have != val:
                    raise WatcherError(
                        "该状态目录已绑定不同 %s（现为 %r），拒绝重新 init"
                        % (key, have))
        if state.get("consumer_id"):
            print("已初始化，跳过重复创建: consumer_id=%s" % state["consumer_id"])
            _print_init_hint(state)
            return
        # 先持久化稳定参数再 POST，失败重试不会重复登记参数
        state = {**want, "consumer_id": None}
        _save_state(state_dir, state)
        response = _api_request(
            "POST", base + "/v1/consumers", token,
            {"subscription_key": args.subscription_key,
             "repository_id": args.repository_id,
             "start": args.start})
        for key in ("consumer_id", "initial_boundary_seq", "cursor_seq"):
            if key not in response:
                raise WatcherError("服务响应缺少字段 %s: %r" % (key, response))
        state.update({key: response[key]
                      for key in ("consumer_id", "initial_boundary_seq", "cursor_seq")})
        _save_state(state_dir, state)
        print("已初始化: consumer_id=%s repository_id=%s subscription_key=%s start=%s"
              % (state["consumer_id"], state["repository_id"],
                 state["subscription_key"], state["start"]))
        _print_init_hint(state)


def cmd_wait(args, state_dir: str) -> None:
    with _state_lock(state_dir):
        state = _load_state(state_dir)
        consumer_id = state.get("consumer_id")
        if not consumer_id:
            raise WatcherError("尚未初始化，请先运行 init")
        base = _check_base(os.environ.get("WATCH_URL", ""))
        _check_server_binding(state, base)
        pending = state.get("pending")
        if pending and pending.get("batch_id"):
            _print_batch(pending.get("events") or [], pending["batch_id"], replay=True)
            return
        token = _token()
        url = _consumer_url(base, consumer_id, "/events")
        while True:
            wait_seconds = POLL_WAIT_SECONDS
            response = _api_request(
                "GET", "%s?wait_seconds=%d" % (url, wait_seconds), token,
                timeout=wait_seconds + REQUEST_TIMEOUT // 2)
            batch_id = response.get("batch_id")
            events = response.get("events") or []
            if batch_id is None and not events:
                continue  # 空批次：内部继续长轮询
            if batch_id is None:
                raise WatcherError("服务返回事件但缺少 batch_id，无法归属，已终止")
            # 先原子落盘 pending，再打印（崩溃后可用 wait 重放）
            state["pending"] = {
                "batch_id": batch_id,
                "events": events,
                "through_seq": response.get("through_seq"),
            }
            _save_state(state_dir, state)
            _print_batch(events, batch_id, replay=False)
            return


def cmd_ack(args, state_dir: str) -> None:
    with _state_lock(state_dir):
        state = _load_state(state_dir)
        pending = state.get("pending")
        if not pending or not pending.get("batch_id"):
            raise WatcherError("本地没有待确认批次（可运行 wait 获取）")
        if args.batch_id != pending["batch_id"]:
            raise WatcherError(
                "--batch-id 与本地 pending 不匹配（本地: %s）" % pending["batch_id"])
        consumer_id = state.get("consumer_id")
        if not consumer_id:
            raise WatcherError("状态文件缺少 consumer_id")
        base = _check_base(os.environ.get("WATCH_URL", ""))
        _check_server_binding(state, base)
        token = _token()
        response = _api_request(
            "POST", _consumer_url(base, consumer_id, "/ack"), token,
            {"batch_id": args.batch_id})
        if not response.get("ok"):
            # 服务未确认成功：保留 pending，可重试
            raise WatcherError("ack 未被服务确认: %r" % response, EXIT_NETWORK)
        state["pending"] = None  # 服务确认成功后才清除 pending
        _save_state(state_dir, state)
        print("已确认批次 %s" % args.batch_id)


# ---------------------------------------------------------------- CLI

def _repository_id(value: str) -> str:
    """repository_id 协议为十进制字符串，CLI 同样收十进制字符串。"""
    value = value.strip()
    if not value.isdigit():
        raise argparse.ArgumentTypeError(
            "repository-id 需为十进制数字字符串（如 1001）")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="webhook_watch",
        description="GitHub webhook 消费者 CLI（替代旧 gh_watch.sh）。"
                    "依赖环境变量 MESSAGE_TOKEN、WATCH_URL，状态目录默认取 WATCH_DIR。")
    parser.add_argument("--state-dir", dest="state_dir_main",
                        help="状态目录（覆盖环境变量 WATCH_DIR，也可放在子命令之后）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser(
        "init", help="创建 consumer 并初始化状态（幂等，已初始化则跳过）",
        epilog="例: init --repository-id 1001 --subscription-key my-run")
    p_init.add_argument("--repository-id", type=_repository_id, required=True,
                        help="要订阅的 GitHub repository 数字 ID（十进制字符串）")
    p_init.add_argument("--subscription-key", required=True,
                        help="本次运行的唯一订阅键（如 run 编号）")
    p_init.add_argument("--start", choices=("now", "earliest_retained"),
                        default="now",
                        help="起始位置: now=订阅时刻之后, earliest_retained=最早保留事件")
    p_init.add_argument("--state-dir", dest="state_dir_sub")
    p_init.set_defaults(func=cmd_init)

    p_wait = sub.add_parser(
        "wait", help="长轮询事件；本地 pending 优先重放；拿到批次先落盘再打印后退出")
    p_wait.add_argument("--state-dir", dest="state_dir_sub")
    p_wait.set_defaults(func=cmd_wait)

    p_ack = sub.add_parser(
        "ack", help="向服务确认批次（成功后清除本地 pending，失败保留可重试）")
    p_ack.add_argument("--batch-id", required=True, help="wait 输出的 batch_id")
    p_ack.add_argument("--state-dir", dest="state_dir_sub")
    p_ack.set_defaults(func=cmd_ack)
    return parser


_HANDLERS = {"init": cmd_init, "wait": cmd_wait, "ack": cmd_ack}


def main(argv: list | None = None) -> int:
    args = _build_parser().parse_args(argv)  # argparse 错误自行 SystemExit(2)
    try:
        state_dir = (args.state_dir_sub or args.state_dir_main
                     or os.environ.get("WATCH_DIR") or "")
        if not state_dir:
            raise WatcherError("未指定状态目录：使用 --state-dir 或设置 WATCH_DIR")
        os.makedirs(state_dir, exist_ok=True)
        try:
            _HANDLERS[args.command](args, state_dir)
        except KeyboardInterrupt:
            print("\n已中断。", file=sys.stderr)
            return EXIT_INTERRUPTED
    except WatcherError as exc:
        print("错误: %s" % exc, file=sys.stderr)
        return exc.exit_code
    except OSError as exc:
        print("错误: 文件/系统错误: %s" % exc, file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())