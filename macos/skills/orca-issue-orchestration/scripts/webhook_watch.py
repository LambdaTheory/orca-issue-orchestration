#!/usr/bin/env python3
"""webhook_watch —— GitHub webhook 消费者 CLI（替代旧 gh_watch.sh）。

子命令:
    init --repository-id <id> --subscription-key <key> [--start now|earliest_retained]
         [--trust-server <服务地址>]
    wait
    ack --batch-id <id>
    login --trust-server <服务地址>   申领个人 message Token 并保存到本机

通用选项:
    --state-dir <dir>   状态目录，可放在子命令前或后（也可用环境变量 WATCH_DIR）
                        （login 不使用状态目录）

服务地址:
    WATCH_URL 环境变量设置服务基地址；未设置时使用默认服务
    https://githubwebhook.madebynight.top（开发环境允许 http://127.0.0.1:port）。
    地址统一规范化（主机小写、默认端口省略、去结尾斜杠）后绑定进状态/凭据。

凭据管理（个人凭据，schema_version=1）:
    - 个人 message Token 与 owner 按规范化 WATCH_URL 哈希分开保存在操作系统
      用户级配置目录（POSIX: ~/.config/webhook-watch，Windows:
      %APPDATA%\\webhook-watch，后者依赖用户目录 ACL 保护），原子写入，
      POSIX 权限目录 0700 / 文件 0600。
    - 凭据优先级: 本地已保存个人凭据 → 仅 init 在无保存凭据时自动执行首次
      签发（签发仅尝试一次，不自动重试）。wait/ack 绝不调用 gh，也绝不
      重新签发；已保存但失效的 Token 不静默回退或重签。
    - 共享令牌方式已停用: 不再读取 MESSAGE_TOKEN 环境变量；检测到该变量时
      各命令直接拒绝并给出升级提示（绝不静默采用，也不覆盖本地凭据）。
    - 首次向服务发送 gh GitHub 凭据前必须显式确认目标服务: --trust-server
      <服务地址>，其值规范化后必须与服务地址完全相同；已有本地凭据时
      消费路径（init 复用/wait/ack）不重复确认。签发仅允许 https
      （loopback http 也不可签发）。
    - login / 自动签发先打印目标服务地址与“临时使用 gh 凭据”提示，再通过
      固定参数子进程调用 gh auth token；GitHub 凭据不写盘、不进参数、
      不进日志或输出。

行为要点:
    - init 先把稳定参数写入 webhook-watch.json 再调用服务，失败重试不会重复
      登记参数；已成功初始化后再次 init 直接跳过（幂等，不重复创建
      consumer）且仍然校验凭据 owner 与状态 owner 一致；首个服务端响应必须
      携带 owner 并与本地状态/凭据一致后才接受。
    - init 输出 initial boundary 提示：该序号仅记录订阅时刻的服务端边界，
      init 后用 gh 核对相关 Issue/PR 当前业务状态并处理遗漏，再运行 wait；
      无需人工核对事件序号，也不新增用户确认步骤。
    - wait 本地存在未确认批次时只重放提示（先处理/ack，不取新批次）；
      空批次在内部继续长轮询，直到拿到批次后：先原子落盘 pending，再打印
      changed 摘要与 batch_id 提示，退出码 0。不自动 ack。events/ack 响应
      （含空批次）的 owner 与状态不符时拒绝处理，不落盘。
    - ack 必须与本地 pending 的 batch_id 一致；服务确认成功后才原子清除
      pending。失败（含响应丢失后重试失败）保留 pending，可再次 ack 恢复。
      已保存 pending 仍可离线重放（本地文件由操作系统权限保护）。
    - 状态只有一个 webhook-watch.json（不用 state.json，避免覆盖状态目录里
      旧 Skill 的同名文件且不含令牌），含 schema_version=1 与
      owner_github_user_id，不含任何凭据；经 os.replace 原子写入；状态目录
      用 OS 级锁防止同一 watcher 并发运行。状态绑定规范化后的 WATCH_URL，
      换服务端须新状态目录，不得拿旧 consumer 继续使用。旧格式（个人 Token
      升级前）已初始化状态在 init 跳过 / wait 重放 / ack 前拒绝复用，需新
      状态目录重新 init；旧共享 Token 停用，旧数据不迁移（pending 不丢失）。
    - 仅 loopback 允许 http，其余必须 https；签发真实 GitHub 凭据仅允许
      https（即使 loopback）；证书默认校验；拒绝跟随 HTTP 重定向（避免把
      令牌发往其他主机）；错误输出脱敏，不含响应正文/异常对象。
    - 供 task_watch.py 复用的实现层：init_impl / ack_impl / poll_once /
      persist_pending，通用 JSON 读写（_load_json/_save_json）与文件锁
      （_lock_file，按文件名派生），以及个人凭据读写/签发
      （read_credentials_file / save_credentials_file /
      resolve_saved_credentials / claim_credentials）。
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager

# ---- 固定参数（如需调整可直接改这里，做成可配置属冗余设计） ----
POLL_WAIT_SECONDS = 25      # 服务端长轮询等待秒数
REQUEST_TIMEOUT = 30        # 普通请求超时（秒）
RETRY_ATTEMPTS = 3          # 网络错误重试次数（含首次共 3 次；签发不适用）
RETRY_SLEEP = 1.0           # 重试间隔（秒）
GH_TOKEN_TIMEOUT = 20       # gh auth token 子进程超时（秒）

DEFAULT_WATCH_URL = "https://githubwebhook.madebynight.top"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_AUTH = 3
EXIT_NETWORK = 4
EXIT_INTERRUPTED = 130

STATE_FILE = "webhook-watch.json"   # 不用 state.json：状态目录属旧 Skill，避免覆盖
LOCK_FILE = "webhook-watch.lock"

STATE_SCHEMA_VERSION = 1           # 状态文件格式版本（区分旧格式/首次未完成状态）
CRED_SCHEMA_VERSION = 1            # 凭据文件格式版本
CRED_PREFIX = "webhook-watch-credential-"   # 同一文件名前缀对应 .json/.lock


class WatcherError(Exception):
    """带退出码的业务错误。"""

    def __init__(self, message: str, exit_code: int = EXIT_ERROR):
        super().__init__(message)
        self.exit_code = exit_code


class LockBusy(WatcherError):
    """The lock is held by another process, not a filesystem failure."""


# ---------------------------------------------------------------- 状态文件

def _load_json(state_dir: str, filename: str) -> dict:
    path = os.path.join(state_dir, filename)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
            if not isinstance(value, dict):
                raise ValueError("状态必须是 JSON 对象")
            return value
    except ValueError as exc:
        raise WatcherError("%s 损坏无法解析（%s），请人工修复后重试"
                           % (path, exc))


def _save_json(state_dir: str, filename: str, data: dict) -> None:
    """原子写入：先写临时文件再 os.replace，失败时旧状态保持完整。"""
    final = os.path.join(state_dir, filename)
    tmp = final + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, final)


def _load_state(state_dir: str) -> dict:
    return _load_json(state_dir, STATE_FILE)


def _save_state(state_dir: str, state: dict) -> None:
    _save_json(state_dir, STATE_FILE, state)


def _valid_owner_id(uid) -> bool:
    """owner id 须为 ASCII 正整数字符串（与服务端一致），避免 isdigit()
    对 ² 等为真而 int() 抛错、或超长数字导致 traceback。"""
    return isinstance(uid, str) and _OWNER_ID_RE.match(uid) is not None


def _reject_legacy_state(state: dict) -> None:
    """旧格式/不完整状态在 init 跳过 / wait 重放 / ack 前拒绝复用。

    - 缺 schema_version 的已初始化状态（个人 Token 升级前）拒绝；
    - 当前版本已初始化状态必须携带合法 owner_github_user_id（规范正整数），
      不能仅凭 schema_version 视为可信；
    - 仅“未完成首次状态”（只有登记参数、无 consumer/pending）允许继续，
      在下一次成功写入时补全 owner。
    """
    version = state.get("schema_version")
    if version != STATE_SCHEMA_VERSION:
        if version is None:
            if state.get("consumer_id") or (state.get("pending") or {}).get("batch_id"):
                raise WatcherError(
                    "检测到个人 Token 升级前的旧格式已初始化状态（缺少 "
                    "schema_version），旧数据不迁移；请使用新的状态目录重新 init"
                    "（未确认批次已保留，不会丢失）", EXIT_ERROR)
            return
        raise WatcherError(
            "状态文件 schema_version=%r 高于当前支持的 %d，不受支持"
            % (version, STATE_SCHEMA_VERSION))
    initialized = bool(state.get("consumer_id")
                       or (state.get("pending") or {}).get("batch_id"))
    if initialized:
        if not _valid_owner_id(state.get("owner_github_user_id")):
            raise WatcherError(
                "已初始化状态缺少合法 owner_github_user_id（规范正整数），"
                "不满足当前格式要求；请使用新的状态目录重新 init"
                "（未确认批次已保留，不会丢失）", EXIT_ERROR)


@contextmanager
def _lock_file(state_dir: str, filename: str):
    """Nonblocking OS lock; callers that wait must supply their own bounded retry."""
    path = os.path.join(state_dir, filename)
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                locked = True
            except OSError as exc:
                if exc.errno not in (11, 13, 35, 36):
                    raise
                raise LockBusy(
                    "另一个 watcher 正在使用该锁（%s 被占用）" % filename) from exc
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as exc:
                if exc.errno not in (11, 13, 35, 36):
                    raise
                raise LockBusy(
                    "另一个 watcher 正在使用该锁（%s 被占用）" % filename) from exc
        yield
    finally:
        if locked and os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(fd)


@contextmanager
def _state_lock(state_dir: str):
    """OS 级排他锁，防止同一状态目录被两个 watcher 同时使用。"""
    with _lock_file(state_dir, LOCK_FILE):
        yield


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
    """校验并规范化服务基址（规范化结果会绑定进状态文件/凭据路径）。

    规范化: 主机名小写、默认端口（443/80）省略、保留路径、去除结尾斜杠。
    拒绝: 凭据组件、query/fragment、控制字符、非法端口。报错不回显原始 URL
    （避免 URL 内凭据/注入内容进入输出）。
    """
    base = (base or "").rstrip("/")
    if not base:
        raise WatcherError("未设置 WATCH_URL 环境变量")
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7f for ch in base):
        raise WatcherError("服务地址不应包含控制字符或未编码空白")
    parsed = urllib.parse.urlparse(base)
    try:
        port = parsed.port
    except ValueError:
        raise WatcherError("服务地址端口非法")
    if parsed.username or parsed.password:
        raise WatcherError("服务地址不应包含用户名/密码（凭据不能进 URL）")
    if parsed.params or parsed.query or parsed.fragment:
        raise WatcherError("服务地址为纯 origin，不应包含 query/fragment")
    if not parsed.hostname:
        raise WatcherError("服务地址缺少主机名")
    scheme = parsed.scheme
    if scheme not in ("http", "https"):
        raise WatcherError("服务地址仅支持 http(loopback) 或 https")
    host = parsed.hostname.lower()
    display_host = "[%s]" % host if ":" in host else host
    if port is not None and not ((scheme == "https" and port == 443)
                                 or (scheme == "http" and port == 80)):
        display_host = "%s:%d" % (display_host, port)
    canonical = "%s://%s%s" % (scheme, display_host, parsed.path)
    if scheme == "https":
        return canonical
    if _is_loopback_host(host):
        return canonical  # 仅 loopback 允许 http（开发用途）
    raise WatcherError("仅 loopback 允许 http，其余地址必须使用 https")


def _default_base() -> str:
    """服务地址解析：WATCH_URL 未设置时使用默认公开服务。"""
    return _check_base(os.environ.get("WATCH_URL") or DEFAULT_WATCH_URL)


def _require_https_for_issuance(base: str) -> None:
    """签发真实 GitHub 凭据仅允许 https（包括 loopback 上的 http）。"""
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme != "https":
        raise WatcherError(
            "签发真实 GitHub 凭据仅允许 https（包括本机 loopback）；"
            "请先为该服务配置可信 HTTPS 再执行签发")


def _check_server_binding(state: dict, base: str) -> None:
    bound = state.get("watch_url")
    if bound and bound != base:
        raise WatcherError(
            "状态目录已绑定其他服务端（%s），请勿更换 WATCH_URL 复用旧 consumer" % bound)


def _check_trust_server(base: str, trust_server) -> None:
    """首次发送 gh GitHub 凭据前的显式确认门。

    --trust-server 值规范化后必须与服务地址完全相同；缺失或不同都拒绝签发
    （报错不含原始输入，避免 URL 内注入内容进入输出）。
    """
    raw = (trust_server or "").strip() if isinstance(trust_server, str) else ""
    if not raw:
        raise WatcherError(
            "首次向服务发送 GitHub 凭据前必须显式确认目标地址："
            "--trust-server 须与服务地址规范化后完全一致（--trust-server %s）；"
            "也可先单独运行 login --trust-server <服务地址> 申领个人 Token"
            % base, EXIT_AUTH)
    try:
        trust = _check_base(raw)
    except WatcherError:
        raise WatcherError(
            "--trust-server 不是合法的服务地址，拒绝发送 GitHub 凭据", EXIT_AUTH)
    if trust != base:
        raise WatcherError(
            "--trust-server 与服务地址不一致（须规范化后完全相同），"
            "拒绝发送 GitHub 凭据", EXIT_AUTH)


# 已知 error.code → 固定本地文案（服务端原文不透传，未知 code 只显示状态码）
_API_ERROR_TEXT = {
    "unauthorized": "令牌无效或未授权",
    "invalid_credentials": "GitHub 凭据无效或权限不足",
    "not_found": "资源不存在",
    "invalid_request": "请求参数无效",
    "invalid_json": "请求体不是合法 JSON",
    "payload_too_large": "请求体过大",
    "method_not_allowed": "请求方法不被允许",
    "already_waiting": "该 consumer 已有等待中的 watcher",
    "invalid_signature": "webhook 签名无效",
}
_API_UNKNOWN = "（未知错误，正文已省略）"


def _api_error_reason(raw: str) -> str:
    """从错误响应提取 error.code 映射为固定本地文案；不输出原始正文。"""
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    code = None
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        code = data["error"].get("code")
    if isinstance(code, str) and code in _API_ERROR_TEXT:
        return _API_ERROR_TEXT[code]
    return _API_UNKNOWN


def _api_request(method: str, url: str, token: str,
                 body: dict | None = None, timeout: int = REQUEST_TIMEOUT) -> dict:
    """发起 JSON API 请求。网络/服务端错误短重试；401/403 不重试。

    仅用于消费类接口；错误输出只含固定文案与状态码，不回显原始 body 或
    异常对象（网络层异常信息可能被中间设备/代理回显凭据）。
    """
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
                data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, dict):
                raise WatcherError("服务响应格式异常（应为 JSON 对象）")
            return data
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            reason = _api_error_reason(raw)
            if 300 <= exc.code < 400:
                # redirect_request 已被禁用，会以 HTTPError 形式抛出
                raise WatcherError(
                    "服务返回重定向 HTTP %d，为避免令牌外泄已拒绝跟随"
                    % exc.code)
            if exc.code in (401, 403):
                err = WatcherError(
                    "认证失败 HTTP %d: %s" % (exc.code, reason), EXIT_AUTH)
                err.http_auth = True  # 供外层区分 401/403 与“缺凭据配置”
                raise err
            if exc.code < 500:
                raise WatcherError(
                    "请求被拒绝 HTTP %d: %s" % (exc.code, reason))
            last_error = WatcherError(
                "服务端错误 HTTP %d: %s" % (exc.code, reason), EXIT_NETWORK)
        except (urllib.error.URLError, OSError, ValueError):
            # 不打印异常对象/正文，避免网络层回显凭据
            last_error = WatcherError(
                "网络错误（连接失败或超时，已重试；详情已省略）", EXIT_NETWORK)
        if attempt < RETRY_ATTEMPTS:
            time.sleep(RETRY_SLEEP)
    raise last_error  # type: ignore[misc]


def _issue_once(base: str, gh_token: str) -> dict:
    """向 POST /v1/auth/token 申请个人 message Token。

    固定协议: Authorization 携带已有 GitHub 凭据，请求体不含任何自报身份/
    组织；签发仅尝试一次（响应丢失可重新执行 login），HTTP 错误输出脱敏，
    不打印响应正文（可能含明文 Token）或异常对象。
    """
    url = base + "/v1/auth/token"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json",
                 "Authorization": "Bearer " + gh_token},
        method="POST")
    try:
        with _OPENER.open(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            exc.read()  # 读取并丢弃正文：正文不含可打印的安全信息
        except Exception:
            pass
        if 300 <= exc.code < 400:
            raise WatcherError(
                "签发请求收到重定向 HTTP %d，为避免凭据外泄已拒绝跟随"
                % exc.code)
        if exc.code in (401, 403):
            raise WatcherError(
                "签发被拒绝 HTTP %d（GitHub 凭据无效或组织校验未通过；"
                "详细原因已省略）" % exc.code, EXIT_AUTH)
        raise WatcherError(
            "签发失败 HTTP %d（详情已省略；响应丢失时可重新运行 login）"
            % exc.code, EXIT_NETWORK)
    except (urllib.error.URLError, OSError, ValueError):
        # 不打印异常对象/正文（网络层可能回显 Authorization），可重新执行 login
        raise WatcherError(
            "签发请求网络错误（不自动重试；响应丢失时可重新运行 login）",
            EXIT_NETWORK)
    token = data.get("message_token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token.strip():
        raise WatcherError("签发响应缺少 message_token（响应内容已省略）")
    token = token.strip()
    _check_message_token(token)
    owner = _validate_owner(data.get("owner") if isinstance(data, dict) else None)
    return {"message_token": token, "owner": owner}


def _consumer_url(base: str, consumer_id, suffix: str = "") -> str:
    return "%s/v1/consumers/%s%s" % (
        base, urllib.parse.quote(str(consumer_id), safe=""), suffix)


# ---------------------------------------------------------------- owner 校验

_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}\Z")

_OWNER_ID_RE = re.compile(r"^[1-9][0-9]{0,29}\Z")


def _validate_owner(owner) -> dict:
    """校验并规范化 owner: 仅返回 github_user_id（正整数）与 github_login
    （合法 GitHub 用户名）两个字段，额外字段不落盘、不透传。"""
    if not isinstance(owner, dict):
        raise WatcherError("服务响应缺少合法 owner（github_user_id/github_login）信息")
    uid = owner.get("github_user_id")
    login = owner.get("github_login")
    if not _valid_owner_id(uid):
        raise WatcherError("服务响应 owner github_user_id 不合法（应为正整数）")
    if not (isinstance(login, str) and _LOGIN_RE.match(login)):
        raise WatcherError("服务响应 owner github_login 不合法")
    return {"github_user_id": uid, "github_login": login}


def _check_response_owner(response: dict, state: dict) -> dict:
    """events/ack 响应（含空批次）必须携带 owner 且与状态 owner 一致，
    否则拒绝处理。（已初始化状态的 owner_github_user_id 由
    _reject_legacy_state 保证有效。）"""
    owner = _validate_owner(response.get("owner"))
    expected = state.get("owner_github_user_id")
    if str(expected) != owner["github_user_id"]:
        raise WatcherError(
            "服务响应 owner 与状态 owner 不相符，已拒绝处理（不落盘）"
            "（状态: %s）" % expected)
    return owner


# ---------------------------------------------------------------- 凭据存储

def _credentials_root() -> str:
    """操作系统用户级配置目录（Windows 依赖用户目录 ACL，POSIX chmod 0700）。"""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config")
    return os.path.join(base, "webhook-watch")


def _prepare_credential_root() -> str:
    """创建凭据根目录并立即收紧权限（须在取锁/签发之前完成）。"""
    root = _credentials_root()
    os.makedirs(root, exist_ok=True)
    if os.name != "nt":
        # POSIX: 目录 0700；Windows 依赖用户目录 ACL
        os.chmod(root, 0o700)
    return root


def _credential_file_path(base: str) -> str:
    """按规范化服务 URL 隔离的默认个人凭据 JSON 路径。"""
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]
    return os.path.join(_credentials_root(), CRED_PREFIX + digest + ".json")


def default_credentials_file(base: str) -> str:
    """按规范化服务 URL 隔离的默认个人凭据 JSON 路径（用户级配置目录）。"""
    return _credential_file_path(base)


def _credential_paths(base: str) -> tuple[str, str]:
    """默认个人凭据 JSON 与对应锁文件路径。"""
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]
    stem = os.path.join(_credentials_root(), CRED_PREFIX + digest)
    return stem + ".json", stem + ".lock"


def _resolve_credential_file(credentials_file: str | None, base: str) -> tuple[str, str]:
    """凭据 JSON/锁路径：显式覆盖路径或默认按 URL 哈希路径。"""
    if credentials_file:
        path = os.path.abspath(os.path.expanduser(credentials_file))
        return path, path + ".lock"
    return _credential_paths(base)


def _check_message_token(token: str) -> None:
    """个人 Token 不允许 CR/LF、空白或任何 ASCII 控制字符。"""
    if not token or any(ord(ch) <= 0x20 or ord(ch) == 0x7f for ch in token):
        raise WatcherError(
            "个人 Token 含非法字符（空白/控制字符），已拒绝")


def read_credentials_file(path: str, base: str) -> dict | None:
    """读取并校验个人凭据 JSON；不存在返回 None；损坏/格式不符/类型或权限
    不安全直接拒绝（绝不当作“无凭据”而触发重新签发）。

    POSIX: lstat 校验——symlink/非普通文件/ownership 不符/权限过宽一律拒绝；
    （symlink 可用于把凭据读取劫持到任意路径，或把写入引向别处。）
    """
    if not os.path.exists(path):
        return None
    if os.name != "nt":
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise WatcherError("凭据文件 %s 不可读，请人工检查后重新 login" % path) from exc
        if not stat.S_ISREG(info.st_mode):
            raise WatcherError(
                "凭据文件 %s 不是普通文件（symlink/目录/设备），已拒绝读取" % path)
        if info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise WatcherError(
                "凭据文件 %s 必须属于当前用户且仅本人可读写（chmod 600），"
                "已拒绝" % path)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as exc:
        raise WatcherError("凭据文件 %s 不可读，请人工检查后重新 login" % path) from exc
    except ValueError as exc:
        raise WatcherError(
            "凭据文件 %s 损坏无法解析（%s），请人工删除该文件后重新 login"
            % (path, exc))
    if not isinstance(data, dict):
        raise WatcherError(
            "凭据文件 %s 格式异常，请人工删除该文件后重新 login" % path)
    if data.get("schema_version") != CRED_SCHEMA_VERSION:
        raise WatcherError(
            "凭据文件 %s schema_version 不受支持，请人工删除该文件后重新 login"
            % path)
    if data.get("watch_url") != base:
        raise WatcherError("凭据文件 %s 记录了其他服务端地址，拒绝使用" % path)
    token = data.get("message_token")
    if not isinstance(token, str) or not token.strip():
        raise WatcherError(
            "凭据文件 %s 缺少 message_token，请人工删除该文件后重新 login" % path)
    try:
        _check_message_token(token)
    except WatcherError:
        raise WatcherError(
            "凭据文件 %s 中 Token 含非法字符，请人工删除该文件后重新 login" % path)
    owner = _validate_owner(data.get("owner"))
    return {"message_token": token, "owner": owner}


def save_credentials_file(path: str, base: str, token: str, owner: dict) -> None:
    """原子私有写入个人凭据 JSON（明文 Token；POSIX 0600）。

    临时文件用 tempfile.mkstemp（同一目录内唯一名、强制 0600），不用固定
    “.tmp”名（避免跟随预置 symlink 写到任意路径）；os.replace 原子替换
    （目标是 symlink 时也是替换链接本身）；任何失败都清理 tmp，避免明文
    Token 以临时文件形式残留。
    """
    _check_message_token(token)
    owner = _validate_owner(owner)
    parent = os.path.dirname(path) or "."
    if not os.path.isdir(parent):
        raise WatcherError("凭据目录不存在：请确认路径或先自行创建 %s" % parent)
    fd = None
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(
            prefix=os.path.basename(path) + ".", suffix=".tmp", dir=parent)
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None  # fdopen 接管
            json.dump({"schema_version": CRED_SCHEMA_VERSION,
                       "watch_url": base, "message_token": token,
                       "owner": owner},
                      f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = None
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


def _load_credentials_or_none(base: str, credentials_file: str | None = None) -> dict | None:
    """默认（或显式覆盖）路径的个人凭据读取。"""
    path, _ = _resolve_credential_file(credentials_file, base)
    return read_credentials_file(path, base)


def _save_credentials(base: str, token: str, owner: dict,
                      credentials_file: str | None = None) -> str:
    """保存个人凭据（默认路径），返回最终路径。"""
    path, _ = _resolve_credential_file(credentials_file, base)
    if credentials_file:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    else:
        _prepare_credential_root()
    save_credentials_file(path, base, token, owner)
    return path


def reject_shared_token_env() -> None:
    """共享令牌（MESSAGE_TOKEN）方式已停用：检测到即拒绝，绝不静默采用，
    也不覆盖本地已保存凭据；给出升级提示（必须先 unset，否则 login 也无法
    通过——本实现不读取该环境变量）。"""
    if (os.environ.get("MESSAGE_TOKEN") or "").strip():
        raise WatcherError(
            "检测到 MESSAGE_TOKEN 环境变量：共享令牌方式已停用，不再读取该"
            "环境变量（也不会用它覆盖本地已保存凭据）。请先 unset MESSAGE_TOKEN"
            "（否则即使运行 login 也会再次被拒绝），然后执行 login "
            "--trust-server <服务地址> 申领个人 Token", EXIT_AUTH)


def _gh_token() -> str:
    """通过固定参数子进程调用 gh 获取已有 GitHub 登录凭据（不打印其输出）。"""
    try:
        proc = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True, text=True, timeout=GH_TOKEN_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise WatcherError(
            "调用 gh 获取 GitHub 凭据超时（%d 秒未返回，详细输出已忽略）"
            % GH_TOKEN_TIMEOUT, EXIT_AUTH)
    except OSError:
        raise WatcherError(
            "无法运行 gh（未安装或不在 PATH；详细错误已省略）", EXIT_AUTH)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise WatcherError(
            "gh 未返回 GitHub 凭据（未登录或不可用；gh 输出已忽略），"
            "请先完成 gh auth login", EXIT_AUTH)
    return proc.stdout.strip()


def _print_issuance_notice(base: str, overwriting: bool) -> None:
    print("即将向服务 %s 申请个人 message Token。" % base)
    print("提示: 将通过 gh（gh auth token）临时读取本机 GitHub 凭据并仅发送给"
          "该服务用于签发；GitHub 凭据不会写入文件、不会出现在命令参数或输出中。")
    print("请确认你信任该服务地址及其 HTTPS 配置（--trust-server 已确认）。")
    if overwriting:
        print("说明: 将重新签发并覆盖本机已保存 Token（服务端旧 Token 仍有效）。")


def claim_credentials(base: str, trust_server: str | None = None,
                      credentials_file: str | None = None,
                      explicit: bool = False) -> tuple[str, dict]:
    """返回 (message_token, owner)；本地已有凭据时非显式路径直接复用（不
    确认、不 gh）。需要签发时：必须显式 --trust-server 精确匹配且为 https，
    经 gh 固定参数子进程读取凭据、单次签发并私有落盘。"""
    path, lock_path = _resolve_credential_file(credentials_file, base)
    default_path = _credential_file_path(base)
    is_default = bool(credentials_file) and \
        os.path.normcase(os.path.normpath(path)) == \
        os.path.normcase(os.path.normpath(default_path))
    existing = read_credentials_file(path, base)
    if existing and not explicit:
        return existing["message_token"], existing["owner"]
    _check_trust_server(base, trust_server)
    _require_https_for_issuance(base)
    if not credentials_file or is_default:
        # 默认路径：用户级配置目录按 0700 收紧（显式传入默认绝对路径同样）
        _prepare_credential_root()
    elif not os.path.isdir(os.path.dirname(path)):
        # 自定义路径：只建目录、绝不 chmod 任意用户的父目录
        os.makedirs(os.path.dirname(path), exist_ok=True)
    with _lock_file(os.path.dirname(lock_path), os.path.basename(lock_path)):
        # 锁内重新检查，避免并发首次申请重复签发
        existing = read_credentials_file(path, base)
        if existing and not explicit:
            return existing["message_token"], existing["owner"]
        _print_issuance_notice(base, existing is not None)
        gh_token = _gh_token()
        result = _issue_once(base, gh_token)
        save_credentials_file(path, base, result["message_token"], result["owner"])
        owner = result["owner"]
        print("凭据已保存: %s（含明文 Token，已设为私有权限，请勿外传）" % path)
        print("已绑定账号: github_login=%s (github_user_id=%s)"
              % (owner["github_login"], owner["github_user_id"]))
        return result["message_token"], owner


def resolve_init_credentials(base: str, trust_server: str | None = None,
                             credentials_file: str | None = None) -> tuple[str, dict]:
    """init 消费路径：本地已保存凭据 → 首次自动签发（须显式 trust）。"""
    return claim_credentials(base, trust_server=trust_server,
                             credentials_file=credentials_file, explicit=False)


def resolve_saved_credentials(base: str, action: str = "wait",
                              credentials_file: str | None = None) -> tuple[str, dict]:
    """wait/ack/后台采集路径：只读已保存凭据；绝不 gh、绝不签发、绝不读
    MESSAGE_TOKEN。"""
    path, _ = _resolve_credential_file(credentials_file, base)
    creds = read_credentials_file(path, base)
    if not creds:
        raise WatcherError(
            "本地没有已保存的个人凭据（%s 为只读凭据操作，不会自动签发，也"
            "不会读取 MESSAGE_TOKEN）；请先在前台运行 login --trust-server "
            "<服务地址> 或 init" % action, EXIT_AUTH)
    return creds["message_token"], creds["owner"]


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
        print("提示: initial boundary seq=%s 仅记录订阅时刻的服务端边界。"
              "请用 gh 核对相关 Issue/PR 的当前业务状态并处理遗漏，再运行 wait；"
              "无需人工核对事件序号，也不新增用户确认步骤。" % boundary)


# ---------------------------------------------------------------- 子命令

def _check_init_want(state: dict, base: str, repository_id: str,
                     subscription_key: str, start: str) -> dict:
    """校验该状态目录已登记的稳定参数（无锁调用时也是先拒绝再碰凭据）。

    返回 want 参数映射；已登记参数（含响应丢失后重试）不一致时拒绝。
    """
    want = {"subscription_key": subscription_key,
            "repository_id": repository_id,  # 十进制字符串
            "start": start,
            "watch_url": base}
    if state.get("subscription_key") is not None \
            or state.get("watch_url") is not None:
        for key, val in want.items():
            have = state.get(key)
            if have is not None and have != val:
                raise WatcherError(
                    "该状态目录已绑定不同 %s（现为 %r），拒绝重新 init"
                    % (key, have))
    return want


def init_impl(state_dir: str, base: str, token: str, repository_id: str,
              subscription_key: str, start: str,
              credential_owner: dict | None = None) -> dict:
    """init 的可复用实现（CLI 与 task_watch 复用，行为不变）。

    base/token/credential_owner 由调用方提供（凭据须来自个人凭据文件或
    本次签发，不得来自 MESSAGE_TOKEN）；credential_owner 必须是已校验的
    owner（{github_user_id, github_login}），缺失/非法即拒绝——不存在
    “无 owner 绕过”路径。写入 schema_version=1 与 owner_github_user_id；
    已初始化状态的 init 同样强制校验凭据 owner 与状态 owner 一致。
    """
    if credential_owner is None:
        raise WatcherError(
            "init 缺少凭据 owner（须来自本地已保存的个人凭据或本次签发）",
            EXIT_AUTH)
    cred_owner = _validate_owner(credential_owner)
    cred_id = cred_owner["github_user_id"]
    with _state_lock(state_dir):
        state = _load_state(state_dir)
        _reject_legacy_state(state)
        want = _check_init_want(state, base, repository_id, subscription_key, start)
        if state.get("consumer_id"):
            # 已初始化：凭据 owner 须与状态 owner 一致（owner 漂移拒绝）
            owner_id = state["owner_github_user_id"]  # _reject_legacy_state 保证合法
            if str(cred_id) != str(owner_id):
                raise WatcherError(
                    "本次凭据所属用户(%s)与已初始化状态 owner(%s)不一致，"
                    "拒绝初始化；请用新状态目录重新 init"
                    % (cred_id, owner_id), EXIT_AUTH)
            print("已初始化，跳过重复创建: consumer_id=%s" % state["consumer_id"])
            _print_init_hint(state)
            return state
        # 旧未完成状态已登记的 owner 必须保留并与当前凭据一致，
        # 避免响应丢失后用户切换凭据导致静默换 owner
        prev_owner = state.get("owner_github_user_id")
        if prev_owner and prev_owner != cred_id:
            raise WatcherError(
                "本次使用的凭据与该状态目录已登记的用户(%s)不一致，拒绝初始化"
                % prev_owner)
        # 先持久化稳定参数再 POST，失败重试不会重复登记参数
        state = {**want, "schema_version": STATE_SCHEMA_VERSION,
                 "consumer_id": None,
                 "owner_github_user_id": prev_owner or cred_id}
        _save_state(state_dir, state)
        response = _api_request(
            "POST", base + "/v1/consumers", token,
            {"subscription_key": subscription_key,
             "repository_id": repository_id,
             "start": start})
        for key in ("consumer_id", "initial_boundary_seq", "cursor_seq", "owner"):
            if key not in response:
                raise WatcherError("服务响应缺少字段 %s" % key)
        owner = _validate_owner(response["owner"])
        server_id = owner["github_user_id"]
        if prev_owner and prev_owner != server_id:
            raise WatcherError(
                "服务响应 owner 与初始化所属用户不符，拒绝初始化")
        if cred_id != server_id:
            raise WatcherError(
                "凭据 owner(%s)与服务响应 owner(%s)不符，拒绝初始化"
                % (cred_id, server_id))
        state["owner_github_user_id"] = server_id
        state.update({key: response[key]
                      for key in ("consumer_id", "initial_boundary_seq", "cursor_seq")})
        _save_state(state_dir, state)
        print("已初始化: consumer_id=%s repository_id=%s subscription_key=%s start=%s"
              % (state["consumer_id"], state["repository_id"],
                 state["subscription_key"], state["start"]))
        _print_init_hint(state)
        return state


def cmd_init(args, state_dir: str) -> None:
    reject_shared_token_env()
    base = _default_base()
    # 先拒绝绑定冲突/旧格式状态，再处理凭据（失败不签发 gh 凭据）
    preflight = _load_state(state_dir)
    _reject_legacy_state(preflight)
    _check_init_want(preflight, base, args.repository_id,
                     args.subscription_key, args.start)
    # 凭据: 已保存 Token → 首次自动签发（须显式 --trust-server，仅 https）
    token, owner = resolve_init_credentials(
        base, trust_server=getattr(args, "trust_server", None),
        credentials_file=getattr(args, "credentials_file", None))
    init_impl(state_dir, base, token, args.repository_id,
              args.subscription_key, args.start, credential_owner=owner)


def cmd_login(args, state_dir: str | None = None) -> None:
    """显式 login：无需状态目录；总是重新签发并覆盖本地 Token（签发仅
    https、仅一次尝试、须显式 --trust-server）。"""
    reject_shared_token_env()
    base = _default_base()
    claim_credentials(base, trust_server=getattr(args, "trust_server", None),
                      credentials_file=getattr(args, "credentials_file", None),
                      explicit=True)


def poll_once(base: str, consumer_id: str, token: str, state: dict,
              wait_seconds: int = POLL_WAIT_SECONDS,
              timeout: int | None = None) -> dict:
    """一次长轮询（不持锁、不落盘）；供 CLI wait 与 serve 复用。

    state 必须提供（含合法 owner_github_user_id）：每次响应（含空批次）都
    校验 owner 与状态一致，不一致即拒绝，绝不静默放行。
    """
    url = _consumer_url(base, consumer_id, "/events")
    to = wait_seconds + REQUEST_TIMEOUT // 2 if timeout is None else timeout
    response = _api_request(
        "GET", "%s?wait_seconds=%d" % (url, wait_seconds), token, timeout=to)
    _check_response_owner(response, state)
    return response


def persist_pending(state_dir: str, state: dict, response: dict) -> bool:
    """把服务返回批次原子落盘 pending（调用方须持 webhook 锁）。

    防御性再校验: 状态必须有合法 owner，且响应 owner 与其一（不一致不落盘、
    不丢已有 pending）。空批次返回 False；返回 True 表示已落盘一个待确认
    批次。
    """
    if not _valid_owner_id(state.get("owner_github_user_id")):
        raise WatcherError(
            "状态缺少合法 owner_github_user_id，拒绝写入 pending（请重新 init）")
    _check_response_owner(response, state)
    batch_id = response.get("batch_id")
    events = response.get("events") or []
    if batch_id is None:
        if not events:
            return False
        raise WatcherError("服务返回事件但缺少 batch_id，无法归属，已终止")
    # 先原子落盘 pending，再由调用方打印（崩溃后可用 wait 重放）
    state["pending"] = {
        "batch_id": batch_id,
        "events": events,
        "through_seq": response.get("through_seq"),
    }
    _save_state(state_dir, state)
    return True


def cmd_wait(args, state_dir: str) -> None:
    reject_shared_token_env()
    with _state_lock(state_dir):
        state = _load_state(state_dir)
        _reject_legacy_state(state)
        consumer_id = state.get("consumer_id")
        if not consumer_id:
            raise WatcherError("尚未初始化，请先运行 init")
        base = _default_base()
        _check_server_binding(state, base)
        pending = state.get("pending")
        if pending and pending.get("batch_id"):
            _print_batch(pending.get("events") or [], pending["batch_id"],
                         replay=True)
            return
        # 只读已保存凭据：wait 绝不调用 gh，也绝不自动签发
        token, owner = resolve_saved_credentials(
            base, "wait", credentials_file=getattr(args, "credentials_file", None))
        # 本地先校验：凭据 owner 与状态 owner 一致，不依赖远端报错
        if str(owner["github_user_id"]) != str(state["owner_github_user_id"]):
            raise WatcherError(
                "凭据 owner(%s)与状态 owner(%s)不相符，拒绝 wait；请重新 "
                "login 或用新状态目录"
                % (owner["github_user_id"], state["owner_github_user_id"]),
                EXIT_AUTH)
        while True:
            response = poll_once(base, consumer_id, token, state=state)
            batch_id = response.get("batch_id")
            events = response.get("events") or []
            if batch_id is None and not events:
                continue  # 空批次：内部继续长轮询
            persist_pending(state_dir, state, response)
            _print_batch(events, batch_id, replay=False)
            return


def ack_impl(state_dir: str, batch_id: str, base: str, token: str,
             credential_owner: dict) -> None:
    """ack 的可复用实现（持原 webhook 锁代理确认；CLI 与 task_watch 复用）。

    credential_owner 必须有效（与 init_impl 同一约束）；credential/state/
    response 三方 owner 一致才清除 pending。
    """
    if credential_owner is None:
        raise WatcherError(
            "ack 缺少凭据 owner（须来自本地已保存的个人凭据）", EXIT_AUTH)
    cred_owner = _validate_owner(credential_owner)
    with _state_lock(state_dir):
        state = _load_state(state_dir)
        _reject_legacy_state(state)
        pending = state.get("pending")
        if not pending or not pending.get("batch_id"):
            raise WatcherError("本地没有待确认批次（可运行 wait 获取）")
        if batch_id != pending["batch_id"]:
            raise WatcherError(
                "--batch-id 与本地 pending 不匹配（本地: %s）" % pending["batch_id"])
        consumer_id = state.get("consumer_id")
        if not consumer_id:
            raise WatcherError("状态文件缺少 consumer_id")
        owner_id = state["owner_github_user_id"]  # _reject_legacy_state 保证合法
        cred_id = cred_owner["github_user_id"]
        if str(cred_id) != str(owner_id):
            raise WatcherError(
                "凭据 owner(%s)与状态 owner(%s)不相符，拒绝 ack；请重新 "
                "login 或用新状态目录" % (cred_id, owner_id), EXIT_AUTH)
        _check_server_binding(state, base)
        response = _api_request(
            "POST", _consumer_url(base, consumer_id, "/ack"), token,
            {"batch_id": batch_id})
        _check_response_owner(response, state)  # owner 不符保留 pending
        if not response.get("ok"):
            # 服务未确认成功：保留 pending，可重试（不回显响应内容）
            raise WatcherError(
                "ack 未被服务确认（服务未返回确认标志），pending 已保留，可重试",
                EXIT_NETWORK)
        state["pending"] = None  # 服务确认成功后才清除 pending
        _save_state(state_dir, state)
        print("已确认批次 %s" % batch_id)


def cmd_ack(args, state_dir: str) -> None:
    reject_shared_token_env()
    base = _default_base()
    token, owner = resolve_saved_credentials(
        base, "ack", credentials_file=getattr(args, "credentials_file", None))
    ack_impl(state_dir, args.batch_id, base, token, credential_owner=owner)


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
                    "凭据: 本地已保存个人 Token → 仅 init 首次自动签发"
                    "（需显式 --trust-server）；wait/ack 不调用 gh，不读取 "
                    "MESSAGE_TOKEN。服务地址缺省 https://githubwebhook."
                    "madebynight.top。")
    parser.add_argument("--state-dir", dest="state_dir_main",
                        help="状态目录（覆盖环境变量 WATCH_DIR，也可放在子命令之后）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser(
        "login", help="向服务申请个人 message Token 并保存到本机用户配置目录"
                      "（无需状态目录；显式调用会重新签发并覆盖本地 Token）")
    p_login.add_argument("--trust-server", required=True,
                         help="显式确认目标服务地址（规范化后须与服务地址完全一致）")
    p_login.add_argument("--credentials-file", default=None,
                         help="凭据 JSON 覆盖路径（与 task_watch 绑定的路径一致，"
                              "用于把新凭据写回原绑定路径）")
    p_login.set_defaults(func=cmd_login)

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
    p_init.add_argument("--trust-server", default=None,
                        help="仅首次签发需要: 显式确认目标服务地址"
                             "（规范化后须与服务地址完全一致）")
    p_init.add_argument("--credentials-file", default=None,
                        help="凭据 JSON 覆盖路径（如与 task_watch 绑定路径一致时使用）")
    p_init.add_argument("--state-dir", dest="state_dir_sub")
    p_init.set_defaults(func=cmd_init)

    p_wait = sub.add_parser(
        "wait", help="长轮询事件；本地 pending 优先重放；拿到批次先落盘再打印后退出")
    p_wait.add_argument("--credentials-file", default=None,
                        help="个人凭据 JSON 覆盖路径（须与 init/login 所用路径一致）")
    p_wait.add_argument("--state-dir", dest="state_dir_sub")
    p_wait.set_defaults(func=cmd_wait)

    p_ack = sub.add_parser(
        "ack", help="向服务确认批次（成功后清除本地 pending，失败保留可重试）")
    p_ack.add_argument("--batch-id", required=True, help="wait 输出的 batch_id")
    p_ack.add_argument("--credentials-file", default=None,
                       help="个人凭据 JSON 覆盖路径（须与 init/login 所用路径一致）")
    p_ack.add_argument("--state-dir", dest="state_dir_sub")
    p_ack.set_defaults(func=cmd_ack)
    return parser


_HANDLERS = {"init": cmd_init, "wait": cmd_wait, "ack": cmd_ack}


def main(argv: list | None = None) -> int:
    args = _build_parser().parse_args(argv)  # argparse 错误自行 SystemExit(2)
    try:
        if args.command == "login":
            # login 不需要状态目录，也不读取/写入 Run 状态
            cmd_login(args)
            return EXIT_OK
        state_dir = (args.state_dir_sub or args.state_dir_main
                     or os.environ.get("WATCH_DIR") or "")
        if not state_dir:
            raise WatcherError("未指定状态目录：使用 --state-dir 或设置 WATCH_DIR")
        os.makedirs(state_dir, exist_ok=True)
        _HANDLERS[args.command](args, state_dir)
    except WatcherError as exc:
        print("错误: %s" % exc, file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return EXIT_INTERRUPTED
    except OSError as exc:
        print("错误: 文件/系统错误: %s" % exc, file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())