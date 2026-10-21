#!/usr/bin/env python3
"""webhook_watch 的单元测试（仅标准库，全部 fake，无真实网络/子进程）。

- 多数用例通过 FakeOpener 直接把请求交给测试函数，不走真实 socket
  （Windows 上高频短连接会因临时端口复用出现 WinError 10048 抖动）。
- 保留一个真实回环 socket 用例验证 opener 构造（重定向拒绝）。
- gh 子进程默认禁止（setUp patch ww.subprocess.run → AssertionError）；
  需要签发的用例用 patch_gh 安装固定输出假件。
- 服务端协议含 owner（create/events/ack 响应），状态含 schema_version=1 +
  owner_github_user_id；MESSAGE_TOKEN 环境变量已停用（检测到即拒绝）。
运行方式: python webhook_watch_test.py
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # webhook_watch.py 与本测试同在 scripts 目录

import webhook_watch as ww  # noqa: E402

ERR_BODY = {"error": {"code": "test", "message": "test error"}}

# 服务端签发的个人 Token 对应的固定 owner（所有 create/events/ack 响应携带）
OWNER = {"github_user_id": "100", "github_login": "octo"}
GH_TOKEN = "gh-test-credential"
SRV_TOKEN = "srv-message-token"
TOKEN_VALUE = SRV_TOKEN  # 兼容旧命名

# setUp 会把 ww._credentials_root patch 到临时目录；导入时捕获真实实现用于
# 平台目录解析断言
_REAL_CREDENTIALS_ROOT = ww._credentials_root


def base_path(rec):
    """请求路径去掉查询串。"""
    return rec.path.split("?", 1)[0]


def consumer_payload(cid="c-1", boundary=5, owner=None):
    return {"consumer_id": cid, "initial_boundary_seq": boundary,
            "cursor_seq": boundary, "owner": owner or OWNER}


def sample_event(seq=1, kind="issue", number=42):
    return {"seq": seq, "event": "issue_comment", "action": "created",
            "repository_id": 1001, "repository": "octo/hello",
            "subject": {"kind": kind, "number": number}, "actor": "alice"}


def batch_payload(batch_id="b1", events=None, owner=None):
    return {"consumer_id": "c-1", "batch_id": batch_id,
            "through_seq": (events[-1]["seq"] if events else 0),
            "events": events or [], "owner": owner or OWNER}


class FakeOpener:
    """替代 ww._OPENER：直接把请求交给 fn(record)->(status, payload)。"""

    def __init__(self, fn):
        self.fn = fn
        self.requests = []

    def open(self, req, timeout=None):
        split = urlsplit(req.full_url)
        path = split.path + ("?" + split.query if split.query else "")
        rec = SimpleNamespace(method=req.get_method(), path=path,
                              body=req.data,
                              headers={k.lower(): v
                                       for k, v in req.header_items()})
        self.requests.append(rec)
        status, payload = self.fn(rec)
        data = json.dumps(payload).encode("utf-8") \
            if not isinstance(payload, bytes) else payload
        fp = io.BytesIO(data)
        if 300 <= status < 400:
            raise urllib.error.HTTPError(req.full_url, status, "redirect",
                                         None, fp)
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status,
                                         "http %d" % status, None, fp)
        return fp


class WatcherCase(unittest.TestCase):
    def setUp(self):
        self.sdir = tempfile.mkdtemp(prefix="ww_test_")
        self.addCleanup(shutil.rmtree, self.sdir, ignore_errors=True)
        self.cdir = tempfile.mkdtemp(prefix="ww_cred_")
        self.addCleanup(shutil.rmtree, self.cdir, ignore_errors=True)
        self._old_env = {k: os.environ.get(k)
                         for k in ("MESSAGE_TOKEN", "WATCH_URL", "WATCH_DIR")}
        os.environ["WATCH_DIR"] = self.sdir
        os.environ.pop("MESSAGE_TOKEN", None)  # 共享令牌已停用，默认不设
        os.environ.pop("WATCH_URL", None)
        self.addCleanup(self._restore_env)
        sleeper = mock.patch.object(ww, "RETRY_SLEEP", 0)
        sleeper.start()
        self.addCleanup(sleeper.stop)
        # 凭据目录指向临时目录，避免触碰真实用户配置
        cred_root = mock.patch.object(ww, "_credentials_root", lambda: self.cdir)
        cred_root.start()
        self.addCleanup(cred_root.stop)
        # gh 子进程默认禁止；需要签发的用例用 patch_gh 安装假件
        self.forbid_gh()
        # 默认注入一份已保存个人凭据（模拟此前 login 成功）；首次申领类
        # 用例用 clear_creds() 删除后再走签发路径
        self.save_creds()

    def _restore_env(self):
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def install(self, fn):
        """安装 FakeOpener，返回它（用 .requests 查看请求记录）。"""
        fake = FakeOpener(fn)
        patcher = mock.patch.object(ww, "_OPENER", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = ww.main(argv)
            except SystemExit as exc:  # argparse
                rc = exc.code if isinstance(exc.code, int) else 1
        return rc, out.getvalue(), err.getvalue()

    def state(self):
        with open(os.path.join(self.sdir, "webhook-watch.json"),
                  encoding="utf-8") as f:
            return json.load(f)

    def do_init(self, url="https://fake.invalid", key="s1", **kwargs):
        os.environ["WATCH_URL"] = url
        argv = ["init", "--repository-id", "1001", "--subscription-key", key]
        argv += sum((["--" + k.replace("_", "-"), str(v)]
                     for k, v in kwargs.items()), [])
        rc, out, err = self.run_cli(argv)
        self.assertEqual(rc, 0, "init 失败: %s" % err)
        return out

    def _init_with(self, url, fn):
        self.install(fn)
        self.do_init(url)

    # ---- 凭据相关 helper ----

    def cred_path(self, base="https://fake.invalid"):
        return ww.default_credentials_file(base)

    def save_creds(self, token=SRV_TOKEN, owner=None,
                   base="https://fake.invalid"):
        """直接写一份已保存凭据（模拟此前 login 成功），返回路径。

        base 会先规范化（与运行时一致，凭据路径/记录值都以规范化 URL 为键）。
        """
        base = ww._check_base(base)
        return ww.save_credentials_file(self.cred_path(base), base, token,
                                        owner or OWNER)

    def clear_creds(self, base="https://fake.invalid"):
        try:
            os.unlink(self.cred_path(base))
        except FileNotFoundError:
            pass

    def patch_gh(self, returncode=0, stdout=GH_TOKEN, stderr=""):
        """mock gh auth token 子进程；返回调用记录列表。"""
        calls = []

        def run(args, **kwargs):
            calls.append({"args": list(args), "kwargs": kwargs})
            return SimpleNamespace(returncode=returncode, stdout=stdout,
                                   stderr=stderr)

        patcher = mock.patch.object(ww.subprocess, "run", new=run)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def forbid_gh(self):
        """断言 gh 子进程绝不被调用。"""

        def run(*args, **kwargs):
            raise AssertionError("测试禁止真实 gh 子进程")

        patcher = mock.patch.object(ww.subprocess, "run", new=run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def set_message_token(self, value="expired-token"):
        os.environ["MESSAGE_TOKEN"] = value

    def write_state(self, state):
        with open(os.path.join(self.sdir, "webhook-watch.json"), "w",
                  encoding="utf-8") as f:
            json.dump(state, f)


class InitTests(WatcherCase):
    def test_init_success_and_idempotent_offline(self):
        ok = lambda rec: (200, consumer_payload()) \
            if rec.method == "POST" and rec.path == "/v1/consumers" \
            else (404, ERR_BODY)  # noqa: E731
        self.install(ok)
        out = self.do_init("https://fake.invalid", key="run-abc")
        self.assertIn("initial boundary seq=5", out)
        self.assertIn("当前业务状态", out)
        self.assertIn("无需人工核对事件序号", out)
        # 已初始化：再次 init 离线幂等跳过，不发起任何请求
        rc, out, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "run-abc"])
        self.assertEqual(rc, 0, err)
        self.assertIn("跳过重复创建", out)
        state = self.state()
        self.assertEqual(state["consumer_id"], "c-1")
        self.assertEqual(state["subscription_key"], "run-abc")
        self.assertEqual(state["schema_version"], ww.STATE_SCHEMA_VERSION)
        self.assertEqual(state["owner_github_user_id"], "100")
        self.assertNotIn("message_token", json.dumps(state))

    def test_init_records_post_body(self):
        fake = self.install(lambda rec: (200, consumer_payload()))
        out = self.do_init("https://fake.invalid", key="run-abc")
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(json.loads(fake.requests[0].body),
                         {"subscription_key": "run-abc",
                          "repository_id": "1001", "start": "now"})
        self.assertIn("consumer_id=c-1", out)

    def test_init_uses_saved_credential_token(self):
        fake = self.install(lambda rec: (200, consumer_payload()))
        self.do_init("https://fake.invalid", key="s1")
        self.assertEqual(fake.requests[0].headers.get("authorization"),
                         "Bearer " + SRV_TOKEN)

    def test_init_start_earliest(self):
        fake = self.install(lambda rec: (200, consumer_payload()))
        self.do_init("https://fake.invalid", start="earliest_retained")
        self.assertEqual(json.loads(fake.requests[0].body)["start"],
                         "earliest_retained")

    def test_init_conflicting_params_refused(self):
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        fake = self.install(lambda rec: (404, ERR_BODY))
        rc, _, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "other"])
        self.assertNotEqual(rc, 0)
        self.assertIn("不同 subscription_key", err)
        self.assertEqual(fake.requests, [])  # 未再次 POST

    def test_init_binds_watch_url_and_refuses_server_switch(self):
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        os.environ["WATCH_URL"] = "https://other.invalid"
        fake = self.install(lambda rec: (404, ERR_BODY))
        # init：同参数但服务端不同也被拒（先拒绝，不签发任何凭据）
        rc, _, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1"])
        self.assertNotEqual(rc, 0)
        self.assertIn("不同 watch_url", err)
        # wait：不得拿绑定在旧服务端的 consumer 换服务器继续
        rc, _, err = self.run_cli(["wait"])
        self.assertNotEqual(rc, 0)
        self.assertIn("已绑定其他服务端", err)
        self.assertEqual(fake.requests, [])

    def test_init_retry_rejects_param_change_after_failed_post(self):
        os.environ["WATCH_URL"] = "https://fake.invalid"
        self.install(lambda rec: (500, ERR_BODY))
        rc, _, _ = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1"])
        self.assertEqual(rc, ww.EXIT_NETWORK)
        # 响应丢失后换订阅键重试：拒绝（已登记的稳定参数必须一致）
        fake = self.install(lambda rec: (404, ERR_BODY))
        rc, _, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s2"])
        self.assertNotEqual(rc, 0)
        self.assertIn("不同 subscription_key", err)
        self.assertEqual(fake.requests, [])

    def test_does_not_overwrite_legacy_state_json(self):
        legacy = os.path.join(self.sdir, "state.json")
        with open(legacy, "w", encoding="utf-8") as f:
            json.dump({"legacy": "old skill state"}, f)
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        with open(legacy, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"legacy": "old skill state"})
        self.assertEqual(self.state()["consumer_id"], "c-1")

    def test_corrupt_state_file_reports_clearly(self):
        with open(os.path.join(self.sdir, "webhook-watch.json"), "w",
                  encoding="utf-8") as f:
            f.write("{not json")
        rc, _, err = self.run_cli(["wait"])
        self.assertEqual(rc, ww.EXIT_ERROR)
        self.assertIn("损坏", err)
        self.assertNotIn("Traceback", err)

    def test_watch_url_validation(self):
        cases = [
            ("https://user:pass@example.com", "用户名/密码"),
            ("https://example.com/?x=1", "query"),
            ("http://127.evil.example.com:8000", "https"),
        ]
        for url, expect in cases:
            os.environ["WATCH_URL"] = url
            rc, _, err = self.run_cli(
                ["init", "--repository-id", "1001", "--subscription-key", "s1"])
            self.assertNotEqual(rc, 0, url)
            self.assertIn(expect, err)
        # 报错不应带出凭据或原 URL
        os.environ["WATCH_URL"] = "https://user:pass@example.com"
        rc, _, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1"])
        self.assertNotIn("user:pass", err)

    def test_init_default_watch_url_when_env_unset(self):
        """未设置 WATCH_URL 时使用默认服务地址并绑定进状态。"""
        self.save_creds(base=ww.DEFAULT_WATCH_URL)
        fake = self.install(lambda rec: (200, consumer_payload()))
        rc, _, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.state()["watch_url"], ww.DEFAULT_WATCH_URL)
        self.assertEqual(fake.requests[0].headers.get("authorization"),
                         "Bearer " + SRV_TOKEN)

    def test_init_persists_params_before_post_then_retry(self):
        calls = []

        def fn(rec):
            if rec.method == "POST" and rec.path == "/v1/consumers":
                calls.append(json.loads(rec.body))
                # 前 3 次（首轮的 3 次重试）都失败，之后才成功
                return (200, consumer_payload()) if len(calls) >= 4 \
                    else (500, ERR_BODY)
            return 404, ERR_BODY

        os.environ["WATCH_URL"] = "https://fake.invalid"
        install = self.install(fn)
        rc, _, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1"])
        self.assertEqual(rc, ww.EXIT_NETWORK)
        self.assertEqual(len(install.requests), 3)
        # 失败但稳定参数已落盘且 consumer_id 为空；owner 已登记
        state = self.state()
        self.assertEqual(state["subscription_key"], "s1")
        self.assertEqual(state["repository_id"], "1001")
        self.assertEqual(state["start"], "now")
        self.assertIsNone(state["consumer_id"])
        self.assertEqual(state["owner_github_user_id"], "100")
        self.assertEqual(state["schema_version"], 1)
        # 重试复用同一参数
        rc, _, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(c["subscription_key"] == "s1" for c in calls))

    def test_http_non_loopback_rejected(self):
        os.environ["WATCH_URL"] = "http://203.0.113.1:8000"  # TEST-NET
        rc, _, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1"])
        self.assertNotEqual(rc, 0)
        self.assertIn("loopback", err)

    def test_init_401(self):
        os.environ["WATCH_URL"] = "https://fake.invalid"
        self.install(lambda rec: (401, ERR_BODY))
        rc, _, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1"])
        self.assertEqual(rc, ww.EXIT_AUTH)
        self.assertIn("401", err)


class WaitTests(WatcherCase):
    def _init_ok_fn(self, rec):
        """共用的 init POST 分支，返回 consumer 响应。"""
        if rec.method == "POST" and rec.path == "/v1/consumers":
            return 200, consumer_payload()
        return None  # 交给调用方继续判断

    def test_wait_batch_then_pending_replay(self):
        gets = []
        init_ok = self._init_ok_fn

        def fn(rec):
            r = init_ok(rec)
            if r:
                return r
            if rec.method == "GET" and base_path(rec).endswith("/events"):
                gets.append(rec.path)
                if len(gets) == 1:
                    return 200, batch_payload("b1", [sample_event()])
                return 500, ERR_BODY  # 后续轮询不应再发生
            return 404, ERR_BODY

        self.install(fn)
        self.do_init("https://fake.invalid")
        rc, out, err = self.run_cli(["wait"])
        self.assertEqual(rc, 0, err)
        self.assertIn("changed: octo/hello issue42 issue_comment/created", out)
        self.assertIn("by alice", out)
        self.assertIn("batch_id: b1", out)
        self.assertEqual(len(gets), 1)
        # 第二次 wait 只重放本地 pending，不再请求服务端
        rc, out, err = self.run_cli(["wait"])
        self.assertEqual(rc, 0, err)
        self.assertIn("重放", out)
        self.assertIn("batch_id: b1", out)
        self.assertEqual(len(gets), 1)
        state = self.state()
        self.assertEqual(state["pending"]["batch_id"], "b1")
        self.assertEqual(state["pending"]["events"][0]["seq"], 1)

    def test_wait_empty_polls_then_batch(self):
        script = [batch_payload(None, []), batch_payload(None, []),
                  batch_payload("b2", [sample_event(seq=2)])]
        gets = []

        def fn(rec):
            r = self._init_ok_fn(rec)
            if r:
                return r
            if rec.method == "GET" and base_path(rec).endswith("/events"):
                gets.append(rec.path)
                return 200, script[len(gets) - 1] if len(gets) <= 3 \
                    else (500, ERR_BODY)
            return 404, ERR_BODY

        self.install(fn)
        self.do_init("https://fake.invalid")
        rc, out, err = self.run_cli(["wait"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(gets), 3)
        self.assertIn("changed: octo/hello issue42", out)
        self.assertIn("batch_id: b2", out)
        self.assertEqual(self.state()["pending"]["batch_id"], "b2")

    def test_edited_comment_same_id_is_delivered_replayed_and_acked(self):
        created = dict(sample_event(seq=6), comment_id=123)
        edited = dict(created, seq=7, action="edited")
        batches = [batch_payload("created-batch", [created]),
                   batch_payload("edited-batch", [edited])]
        gets, acks = [], []

        def fn(rec):
            initialized = self._init_ok_fn(rec)
            if initialized:
                return initialized
            if rec.method == "GET" and base_path(rec).endswith("/events"):
                gets.append(rec.path)
                return 200, batches[len(gets) - 1]
            if rec.method == "POST" and base_path(rec).endswith("/ack"):
                acks.append(json.loads(rec.body)["batch_id"])
                return 200, {"ok": True, "batch_id": acks[-1], "owner": OWNER}
            return 404, ERR_BODY

        self.install(fn)
        self.do_init("https://fake.invalid")
        rc, _, err = self.run_cli(["wait"])
        self.assertEqual(rc, 0, err)
        rc, _, err = self.run_cli(["ack", "--batch-id", "created-batch"])
        self.assertEqual(rc, 0, err)

        rc, out, err = self.run_cli(["wait"])
        self.assertEqual(rc, 0, err)
        self.assertIn("issue42 issue_comment/edited", out)
        self.assertIn("comment_id=123", out)
        self.assertEqual(self.state()["pending"]["events"], [edited])
        self.assertEqual(acks, ["created-batch"])  # 编辑事件不能自动确认
        rc, out, err = self.run_cli(["wait"])
        self.assertEqual(rc, 0, err)
        self.assertIn("重放", out)
        self.assertIn("issue_comment/edited", out)
        self.assertEqual(len(gets), 2)

        rc, _, err = self.run_cli(["ack", "--batch-id", "edited-batch"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(acks, ["created-batch", "edited-batch"])
        self.assertIsNone(self.state()["pending"])

    def test_wait_pr_event_fields(self):
        ev = {"seq": 3, "event": "push", "action": "", "repository_id": 1001,
              "repository": "octo/hello", "subject": {"kind": "pr",
                                                      "number": 7},
              "actor": "bob", "head_sha": "abc123"}
        gets = []

        def fn(rec):
            r = self._init_ok_fn(rec)
            if r:
                return r
            if rec.method == "GET" and base_path(rec).endswith("/events"):
                gets.append(rec.path)
                return 200, batch_payload("b3", [ev])
            return 404, ERR_BODY

        self.install(fn)
        self.do_init("https://fake.invalid")
        rc, out, err = self.run_cli(["wait"])
        self.assertEqual(rc, 0, err)
        self.assertIn("changed: octo/hello pr7 push/", out)
        self.assertIn("head_sha=abc123", out)

    def test_wait_requires_init(self):
        rc, _, err = self.run_cli(["wait"])
        self.assertNotEqual(rc, 0)
        self.assertIn("init", err)

    def test_wait_403(self):
        def fn(rec):
            if rec.method == "POST" and rec.path == "/v1/consumers":
                return 200, consumer_payload()
            if base_path(rec).endswith("/events"):
                return 403, ERR_BODY
            return 404, ERR_BODY

        self.install(fn)
        self.do_init("https://fake.invalid")
        rc, _, err = self.run_cli(["wait"])
        self.assertEqual(rc, ww.EXIT_AUTH)
        self.assertIn("403", err)

    def test_wait_atomic_save_failure_keeps_old_state(self):
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        os.environ["WATCH_URL"] = "https://fake.invalid"
        self.install(lambda rec: (200, batch_payload("b1", [sample_event()])))
        with mock.patch.object(ww.os, "replace", side_effect=OSError("boom")):
            rc, _, err = self.run_cli(["wait"])
        self.assertNotEqual(rc, 0)
        self.assertIn("boom", err)
        # 落盘失败：state.json 仍为旧完整性内容，无 pending，且无残留事件输出
        state = self.state()
        self.assertEqual(state["consumer_id"], "c-1")
        self.assertIsNone(state.get("pending"))

    def test_wait_rejects_redirect(self):
        def fn(rec):
            if rec.method == "POST" and rec.path == "/v1/consumers":
                return 200, consumer_payload()
            if base_path(rec).endswith("/events"):
                return 302, {"Location": "/evil"}
            return 404, ERR_BODY

        self.install(fn)
        self.do_init("https://fake.invalid")
        rc, _, err = self.run_cli(["wait"])
        self.assertNotEqual(rc, 0)
        self.assertIn("重定向", err)


class AckTests(WatcherCase):
    def _prepare_pending(self):
        gets = []

        def fn(rec):
            if base_path(rec).endswith("/events"):
                gets.append(rec.path)
                return 200, batch_payload("b1", [sample_event()])
            return 404, ERR_BODY

        self.install(fn)
        rc, _, err = self.run_cli(["wait"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(gets[-1].startswith("/v1/consumers/c-1/"), True)

    def test_ack_mismatch_then_success(self):
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        os.environ["WATCH_URL"] = "https://fake.invalid"
        self._prepare_pending()
        acks = []

        def fn(rec):
            if rec.method == "POST" and base_path(rec).endswith("/ack"):
                acks.append(json.loads(rec.body))
                return 200, {"ok": True, "batch_id": acks[-1]["batch_id"],
                             "owner": OWNER}
            return 404, ERR_BODY

        self.install(fn)
        # 不匹配的 batch_id：失败且不请求服务端
        rc, _, err = self.run_cli(["ack", "--batch-id", "nope"])
        self.assertNotEqual(rc, 0)
        self.assertIn("不匹配", err)
        self.assertEqual(acks, [])
        # 正确 batch_id：请求 /ack 并清除 pending
        rc, out, err = self.run_cli(["ack", "--batch-id", "b1"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(acks, [{"batch_id": "b1"}])
        self.assertIn("已确认", out)
        self.assertIsNone(self.state()["pending"])

    def test_ack_server_failure_keeps_pending(self):
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        os.environ["WATCH_URL"] = "https://fake.invalid"
        self._prepare_pending()
        self.install(lambda rec: (500, ERR_BODY))
        rc, _, err = self.run_cli(["ack", "--batch-id", "b1"])
        self.assertEqual(rc, ww.EXIT_NETWORK, err)
        self.assertEqual(self.state()["pending"]["batch_id"], "b1")
        # 响应丢失场景：服务端先失败后成功，重试可恢复
        acks = []

        def fn(rec):
            if base_path(rec).endswith("/ack"):
                acks.append(rec)
                return (200, {"ok": True, "batch_id": "b1", "owner": OWNER}) \
                    if len(acks) >= 2 else (500, ERR_BODY)
            return 404, ERR_BODY

        self.install(fn)
        rc, _, err = self.run_cli(["ack", "--batch-id", "b1"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(acks), 2)
        self.assertIsNone(self.state()["pending"])

    def test_ack_not_ok_response_keeps_pending(self):
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        os.environ["WATCH_URL"] = "https://fake.invalid"
        self._prepare_pending()
        self.install(lambda rec: (200, {"ok": False, "owner": OWNER}))
        rc, _, err = self.run_cli(["ack", "--batch-id", "b1"])
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.state()["pending"]["batch_id"], "b1")

    def test_ack_without_pending(self):
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        os.environ["WATCH_URL"] = "https://fake.invalid"
        # 清除本地 pending（保留已初始化状态）后 ack：本地拒绝，不请求服务端
        with open(os.path.join(self.sdir, "webhook-watch.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"subscription_key": "s1", "repository_id": "1001",
                       "start": "now", "watch_url": "https://fake.invalid",
                       "schema_version": 1, "consumer_id": "c-1",
                       "initial_boundary_seq": 5, "cursor_seq": 5,
                       "owner_github_user_id": "100", "pending": None}, f)
        self.install(lambda rec: (404, ERR_BODY))
        rc, _, err = self.run_cli(["ack", "--batch-id", "b1"])
        self.assertNotEqual(rc, 0)
        self.assertIn("待确认批次", err)

    def test_ack_401(self):
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        os.environ["WATCH_URL"] = "https://fake.invalid"
        self._prepare_pending()
        self.install(lambda rec: (401, ERR_BODY))
        rc, _, err = self.run_cli(["ack", "--batch-id", "b1"])
        self.assertEqual(rc, ww.EXIT_AUTH)
        self.assertIn("401", err)
        self.assertEqual(self.state()["pending"]["batch_id"], "b1")


class MiscTests(WatcherCase):
    def test_state_lock_blocks_concurrent_run(self):
        with ww._state_lock(self.sdir):
            rc, _, err = self.run_cli(["wait", "--state-dir", self.sdir])
            self.assertEqual(rc, ww.EXIT_ERROR)
            self.assertIn("webhook-watch.lock", err)
        # 释放锁后同一 CLI 能通过锁检查（这里因未初始化而报 init 错误）
        rc, _, err = self.run_cli(["wait", "--state-dir", self.sdir])
        self.assertEqual(rc, ww.EXIT_ERROR)
        self.assertIn("init", err)
        self.assertNotIn("state.lock", err)

    def test_missing_state_dir(self):
        os.environ.pop("WATCH_DIR", None)
        rc, _, err = self.run_cli(["wait"])
        self.assertNotEqual(rc, 0)
        self.assertIn("状态目录", err)

    def test_help(self):
        with contextlib.redirect_stdout(io.StringIO()) as buf1:
            with self.assertRaises(SystemExit) as cm:
                ww.main(["--help"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("init", buf1.getvalue())
        self.assertIn("login", buf1.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as buf2:
            with self.assertRaises(SystemExit) as cm:
                ww.main(["init", "--help"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("--repository-id", buf2.getvalue())
        self.assertIn("--trust-server", buf2.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as buf3:
            with self.assertRaises(SystemExit) as cm:
                ww.main(["wait", "--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_missing_subcommand(self):
        with self.assertRaises(SystemExit) as cm:
            ww.main([])
        self.assertEqual(cm.exception.code, 2)


# ------------------------------------------------- 首次申领 / login / trust

class ClaimAndTrustTests(WatcherCase):
    """首次申领必须显式 --trust-server（规范化后精确匹配）且 https；
    已保存凭据的消费路径不重复确认、绝不 gh。"""

    def test_login_requires_explicit_trust_server(self):
        os.environ["WATCH_URL"] = "https://fake.invalid"
        rc, _, err = self.run_cli(["login"])
        self.assertNotEqual(rc, 0)
        self.assertIn("--trust-server", err)

    def test_login_trust_mismatch_refused(self):
        self.clear_creds()  # setUp 注入的默认凭据会被显式 login 覆盖签发
        os.environ["WATCH_URL"] = "https://fake.invalid"
        self.install(lambda rec: (200, {"message_token": SRV_TOKEN,
                                        "owner": OWNER}))
        rc, _, err = self.run_cli(["login", "--trust-server", "https://other.invalid"])
        self.assertNotEqual(rc, 0)
        self.assertIn("--trust-server", err)
        self.assertIn("拒绝发送 GitHub 凭据", err)
        self.assertFalse(os.path.exists(self.cred_path()))

    def test_login_trust_matches_after_normalization(self):
        """trust 值规范化后精确匹配（大小写/结尾斜杠/默认端口）即可签发。"""
        self.clear_creds()
        calls = self.patch_gh()
        os.environ["WATCH_URL"] = "https://fake.invalid"
        fake = self.install(lambda rec: (200, {"message_token": SRV_TOKEN,
                                               "owner": OWNER})
                            if rec.method == "POST" and rec.path == "/v1/auth/token"
                            else (404, ERR_BODY))
        rc, _, err = self.run_cli(["login", "--trust-server", "https://Fake.invalid/"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(fake.requests[0].headers.get("authorization"),
                         "Bearer " + GH_TOKEN)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["args"],
                         ["gh", "auth", "token", "--hostname", "github.com"])
        self.assertEqual(
            json.load(open(self.cred_path(), encoding="utf-8"))["message_token"],
            SRV_TOKEN)
        self.assertEqual(os.stat(self.cred_path()).st_mode & 0o777, 0o600)

    def test_first_claim_never_on_http_even_loopback(self):
        self.clear_creds()
        self.patch_gh()
        os.environ["WATCH_URL"] = "http://127.0.0.1:8000"
        self.install(lambda rec: (200, {"message_token": SRV_TOKEN,
                                        "owner": OWNER}))
        rc, _, err = self.run_cli(["login", "--trust-server", "http://127.0.0.1:8000"])
        self.assertNotEqual(rc, 0)
        self.assertIn("https", err)
        self.assertFalse(os.path.exists(self.cred_path("http://127.0.0.1:8000")))

    def test_init_first_auto_claim_without_trust_refused(self):
        """init 无已保存凭据时自动签发必须 --trust-server。"""
        self.clear_creds()
        os.environ["WATCH_URL"] = "https://fake.invalid"
        self.install(lambda rec: (200, {"message_token": SRV_TOKEN,
                                        "owner": OWNER}))
        rc, _, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1"])
        self.assertEqual(rc, ww.EXIT_AUTH)
        self.assertIn("--trust-server", err)

    def test_init_first_auto_claim_success_flow(self):
        """init 申领: 固定参数 gh 子进程、单次签发请求无请求体、绑定账号落盘。"""
        self.clear_creds()
        calls = self.patch_gh()
        state_tokens = []

        def fn(rec):
            if rec.method == "POST" and rec.path == "/v1/auth/token":
                state_tokens.append(rec.headers.get("authorization"))
                return 200, {"message_token": SRV_TOKEN, "owner": OWNER}
            if rec.method == "POST" and rec.path == "/v1/consumers":
                assert rec.headers.get("authorization") == "Bearer " + SRV_TOKEN
                return 200, consumer_payload()
            return 404, ERR_BODY

        fake = self.install(fn)
        out = self.do_init("https://fake.invalid", key="s1",
                           trust_server="https://fake.invalid")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["args"],
                         ["gh", "auth", "token", "--hostname", "github.com"])
        self.assertTrue(calls[0]["kwargs"].get("capture_output"))
        self.assertEqual(calls[0]["kwargs"].get("timeout"), ww.GH_TOKEN_TIMEOUT)
        auth_reqs = [r for r in fake.requests if r.path == "/v1/auth/token"]
        self.assertEqual(len(auth_reqs), 1)
        self.assertIsNone(auth_reqs[0].body)
        self.assertEqual(state_tokens, ["Bearer " + GH_TOKEN])
        # 输出与状态不含任何凭据
        self.assertNotIn(GH_TOKEN, out)
        self.assertNotIn(SRV_TOKEN, out)
        state = self.state()
        self.assertEqual(state["owner_github_user_id"], "100")
        self.assertEqual(state["schema_version"], ww.STATE_SCHEMA_VERSION)
        # 凭据落盘在用户配置目录
        self.assertEqual(ww._load_credentials_or_none(
            "https://fake.invalid")["message_token"], SRV_TOKEN)

    def test_init_claim_gh_failure_redacted_and_no_reissue(self):
        """gh 不可用时 init 报认证错误；gh stderr 不进输出。"""
        self.clear_creds()
        self.patch_gh(returncode=1, stdout="", stderr="gh: noise LEAK-XYZ")
        os.environ["WATCH_URL"] = "https://fake.invalid"
        rc, _, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1",
             "--trust-server", "https://fake.invalid"])
        self.assertEqual(rc, ww.EXIT_AUTH)
        self.assertIn("gh", err)
        self.assertNotIn("LEAK-XYZ", err)

    def test_cached_credentials_never_confirm_or_gh(self):
        """已保存凭据：init/wait/ack 直接复用，不要求 trust、不调用 gh。"""
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        os.environ["WATCH_URL"] = "https://fake.invalid"
        def fn(rec):
            if base_path(rec).endswith("/events"):
                return 200, batch_payload("b1", [sample_event()])
            return 200, {"ok": True, "batch_id": "b1", "owner": OWNER}

        self.install(fn)
        rc, _, err = self.run_cli(["wait"])   # wait 不需要 --trust-server
        self.assertEqual(rc, 0, err)
        rc, _, err = self.run_cli(["ack", "--batch-id", "b1"])
        self.assertEqual(rc, 0, err)

    def test_wait_no_saved_credentials_never_runs_gh(self):
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        self.clear_creds()
        fake = self.install(lambda rec: (404, ERR_BODY))
        rc, _, err = self.run_cli(["wait"])
        self.assertEqual(rc, ww.EXIT_AUTH)
        self.assertIn("login", err)
        self.assertIn("init", err)
        self.assertEqual(fake.requests, [])

    def test_expired_saved_token_no_silent_reissue_on_wait(self):
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        self.install(lambda rec: (401, ERR_BODY))
        rc, _, _ = self.run_cli(["wait"])
        self.assertEqual(rc, ww.EXIT_AUTH)
        # 本地凭据未被重签/覆盖
        with open(self.cred_path(), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["message_token"], SRV_TOKEN)

    def test_message_token_env_is_retired_and_refused(self):
        """MESSAGE_TOKEN 已停用：绝不静默采用，也不覆盖缓存，给升级提示。"""
        self.set_message_token()
        fake = self.install(lambda rec: (200, consumer_payload()))
        for argv in (
            ["init", "--repository-id", "1001", "--subscription-key", "s1",
             "--credentials-file", self.cred_path()],
            ["wait"],
            ["ack", "--batch-id", "b1"],
            ["login", "--trust-server", "https://fake.invalid"],
            ["login", "--trust-server", "https://fake.invalid",
             "--credentials-file", self.cred_path()],
        ):
            rc, _, err = self.run_cli(argv)
            self.assertEqual(rc, ww.EXIT_AUTH, argv)
            self.assertIn("MESSAGE_TOKEN", err)
            self.assertIn("unset", err)
            self.assertIn("停用", err)
            self.assertFalse(os.path.exists(
                os.path.join(self.sdir, "custom-cred.json")))
        self.assertEqual(fake.requests, [])
        with open(self.cred_path(), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["message_token"], SRV_TOKEN)  # 未覆盖

    def test_credential_lock_blocks_concurrent_claim(self):
        """持锁期间（如另一进程在签发），首次申领路径拒绝并发签发。"""
        self.clear_creds()
        path, lock_path = ww._credential_paths("https://fake.invalid")
        root = os.path.dirname(lock_path)
        os.makedirs(root, exist_ok=True)
        with ww._lock_file(root, os.path.basename(lock_path)):
            self.install(lambda rec: (200, {"message_token": SRV_TOKEN,
                                            "owner": OWNER}))
            os.environ["WATCH_URL"] = "https://fake.invalid"
            rc, _, err = self.run_cli(
                ["init", "--repository-id", "1001", "--subscription-key", "s1",
                 "--trust-server", "https://fake.invalid"])
            self.assertNotEqual(rc, 0)
            self.assertIn("被占用", err)

    def test_credentials_isolated_per_server(self):
        self.save_creds(token="token-a", base="https://a.invalid")
        self.save_creds(token="token-b", base="https://b.invalid")
        self.assertEqual(ww._load_credentials_or_none(
            "https://a.invalid")["message_token"], "token-a")
        self.assertEqual(ww._load_credentials_or_none(
            "https://b.invalid")["message_token"], "token-b")
        self.assertNotEqual(self.cred_path("https://a.invalid"),
                            self.cred_path("https://b.invalid"))

    def test_credential_file_private_permissions(self):
        if os.name == "nt":
            self.skipTest("Windows 依赖用户目录 ACL，跳过 POSIX 权限断言")
        self.assertEqual(os.stat(self.cred_path()).st_mode & 0o777, 0o600)
        self.assertEqual(
            os.stat(ww._credentials_root()).st_mode & 0o777, 0o700)

    def test_corrupt_credential_file_rejects_without_reissue(self):
        """已初始化 + 凭据损坏：wait 拒绝，不当作无凭据重签发/调用 gh。"""
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        with open(self.cred_path(), "w", encoding="utf-8") as f:
            f.write("{broken")
        fake = self.install(lambda rec: (404, ERR_BODY))
        rc, _, err = self.run_cli(["wait"])
        self.assertNotEqual(rc, 0)
        self.assertIn("凭据文件", err)
        self.assertEqual(fake.requests, [])  # 不触发签发

    def test_credential_file_wrong_watch_url_rejected(self):
        self.save_creds()
        path = self.cred_path()
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data["watch_url"] = "https://other.invalid"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.environ["WATCH_URL"] = "https://fake.invalid"
        self.install(lambda rec: (200, consumer_payload()))
        rc, _, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1"])
        self.assertNotEqual(rc, 0)
        self.assertIn("其他服务端", err)

    def test_login_reissues_and_overwrites_saved_token(self):
        self.save_creds(token="old-token")
        self.patch_gh()
        os.environ["WATCH_URL"] = "https://fake.invalid"
        self.install(lambda rec: (200, {"message_token": SRV_TOKEN,
                                        "owner": OWNER})
                     if rec.method == "POST" and rec.path == "/v1/auth/token"
                     else (404, ERR_BODY))
        rc, out, err = self.run_cli(["login", "--trust-server", "https://fake.invalid"])
        self.assertEqual(rc, 0, err)
        with open(self.cred_path(), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["message_token"], SRV_TOKEN)
        self.assertIn("覆盖", out)
        self.assertNotIn("old-token", out)

    def test_login_issuance_single_attempt_and_redacted_errors(self):
        """签发 HTTP 仅 1 次；错误不打印响应正文。"""
        self.clear_creds()
        self.patch_gh()
        os.environ["WATCH_URL"] = "https://fake.invalid"
        n = {"count": 0}

        def fn(rec):
            if rec.method == "POST" and rec.path == "/v1/auth/token":
                n["count"] += 1
                return 500, {"error": {"message": "boom SNEAKY-BODY-xyz"}}
            return 404, ERR_BODY

        self.install(fn)
        rc, _, err = self.run_cli(["login", "--trust-server", "https://fake.invalid"])
        self.assertEqual(rc, ww.EXIT_NETWORK)
        self.assertEqual(n["count"], 1)  # 不自动重试
        self.assertNotIn("SNEAKY-BODY-xyz", err)
        self.assertNotIn("boom", err)

    def test_login_malformed_response_not_echoed(self):
        """响应缺少/非法字段：报错不包含响应内容（可能含 Token）。"""
        cases = [
            ({"message_token": "LEAKY-TOKEN"}, "owner"),                 # 缺 owner
            ({"message_token": "LEAKY-TOKEN",
              "owner": {"github_user_id": 100,
                        "github_login": "octo"}}, "owner"),              # 非字符串
            ({"owner": OWNER}, "message_token"),                         # 缺 message_token
        ]
        for payload, keyword in cases:
            self.clear_creds()
            self.patch_gh()
            os.environ["WATCH_URL"] = "https://fake.invalid"
            self.install(lambda rec, p=payload: (200, p))
            rc, _, err = self.run_cli(
                ["login", "--trust-server", "https://fake.invalid"])
            self.assertNotEqual(rc, 0, payload)
            self.assertNotIn("LEAKY-TOKEN", err)
            self.assertIn(keyword, err)
            self.assertFalse(os.path.exists(self.cred_path()))

    def test_login_network_error_no_retry(self):
        self.clear_creds()
        self.patch_gh()
        os.environ["WATCH_URL"] = "https://fake.invalid"
        n = {"count": 0}

        def fn(rec):
            n["count"] += 1
            raise urllib.error.URLError("conn refused")

        self.install(fn)
        rc, _, err = self.run_cli(["login", "--trust-server", "https://fake.invalid"])
        self.assertEqual(rc, ww.EXIT_NETWORK)
        self.assertEqual(n["count"], 1)
        self.assertIn("重新运行 login", err)

    def test_ensure_claim_recheck_with_explicit_trust(self):
        """锁内重新检查：已有凭据时自动路径复用，不重复签发（trust 可缺省）。"""
        self.forbid_gh()
        token, owner = ww.claim_credentials("https://fake.invalid")
        self.assertEqual(token, SRV_TOKEN)
        self.assertEqual(owner, OWNER)


class OwnerAndStateTests(WatcherCase):
    def test_init_create_response_missing_owner_rejected(self):
        os.environ["WATCH_URL"] = "https://fake.invalid"
        self.install(lambda rec: (200, {
            "consumer_id": "c-1", "initial_boundary_seq": 5,
            "cursor_seq": 5}))  # 无 owner
        rc, _, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1"])
        self.assertNotEqual(rc, 0)
        self.assertIn("owner", err)
        self.assertIsNone(self.state()["consumer_id"])

    def test_init_owner_mismatch_with_saved_credentials_rejected(self):
        self.save_creds(token=SRV_TOKEN,
                        owner={"github_user_id": "200", "github_login": "eve"})
        os.environ["WATCH_URL"] = "https://fake.invalid"
        self.install(lambda rec: (200, consumer_payload())  # owner=100
                     if rec.method == "POST" and rec.path == "/v1/consumers"
                     else (404, ERR_BODY))
        rc, _, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1"])
        self.assertNotEqual(rc, 0)
        self.assertIn("owner", err)
        self.assertIsNone(self.state()["consumer_id"])

    def test_wait_owner_mismatch_not_saved(self):
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        bad = {**OWNER, "github_user_id": "200"}

        def fn(rec):
            if base_path(rec).endswith("/events"):
                payload = batch_payload("b1", [sample_event()], owner=bad)
                return 200, payload
            return 404, ERR_BODY

        self.install(fn)
        rc, _, err = self.run_cli(["wait"])
        self.assertNotEqual(rc, 0)
        self.assertIn("owner", err)
        self.assertIsNone(self.state().get("pending"))

    def test_wait_missing_owner_rejected(self):
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))

        def fn(rec):
            if base_path(rec).endswith("/events"):
                payload = batch_payload("b1", [sample_event()])
                payload.pop("owner")
                return 200, payload
            return 404, ERR_BODY

        self.install(fn)
        rc, _, err = self.run_cli(["wait"])
        self.assertNotEqual(rc, 0)
        self.assertIn("owner", err)
        self.assertIsNone(self.state().get("pending"))

    def test_wait_empty_batch_with_bad_owner_rejected(self):
        """空批次响应同样校验 owner（不落盘、不静默继续）。"""
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        bad = {**OWNER, "github_user_id": "200"}

        def fn(rec):
            if base_path(rec).endswith("/events"):
                return 200, batch_payload(None, [], owner=bad)
            return 404, ERR_BODY

        self.install(fn)
        rc, _, err = self.run_cli(["wait"])
        self.assertNotEqual(rc, 0)
        self.assertIn("owner", err)
        self.assertIsNone(self.state().get("pending"))

    def test_ack_owner_mismatch_keeps_pending(self):
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        os.environ["WATCH_URL"] = "https://fake.invalid"
        self.install(lambda rec: (200, batch_payload("b1", [sample_event()])))
        rc, _, err = self.run_cli(["wait"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.state()["pending"]["batch_id"], "b1")

        def fn(rec):
            if base_path(rec).endswith("/ack"):
                return 200, {"ok": True, "batch_id": "b1",
                             "owner": {**OWNER, "github_user_id": "200"}}
            return 404, ERR_BODY

        self.install(fn)
        rc, _, err = self.run_cli(["ack", "--batch-id", "b1"])
        self.assertNotEqual(rc, 0)
        self.assertIn("owner", err)
        self.assertEqual(self.state()["pending"]["batch_id"], "b1")

    def test_ack_credential_owner_mismatch_keeps_pending(self):
        """凭据 owner 与状态 owner 不一致（如换号再 login）→ 拒绝 ack。"""
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        os.environ["WATCH_URL"] = "https://fake.invalid"
        self.install(lambda rec: (200, batch_payload("b1", [sample_event()])))
        rc, _, _ = self.run_cli(["wait"])
        self.assertEqual(rc, 0)
        # 换成另一账号的凭据（重新 login 到同一状态目录的 URL）
        self.save_creds(token="other-token",
                        owner={"github_user_id": "200", "github_login": "eve"})
        self.forbid_gh()
        self.install(lambda rec: (404, ERR_BODY))
        rc, _, err = self.run_cli(["ack", "--batch-id", "b1"])
        self.assertNotEqual(rc, 0)
        self.assertIn("owner", err)
        self.assertEqual(self.state()["pending"]["batch_id"], "b1")

    def test_initialized_init_with_owner_change_refused(self):
        """已初始化状态的 init 也校验 owner：凭据 owner 变化 → 拒绝。"""
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        self.save_creds(token="other-token",
                        owner={"github_user_id": "200", "github_login": "eve"})
        self.install(lambda rec: (404, ERR_BODY))
        rc, _, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1"])
        self.assertNotEqual(rc, 0)
        self.assertIn("owner", err)
        self.assertEqual(self.state()["consumer_id"], "c-1")  # 状态不被破坏


def legacy_state(**extra):
    """旧格式（个人 Token 升级前）已初始化状态（无 schema_version）。"""
    state = {"subscription_key": "s1", "repository_id": "1001",
             "start": "now", "watch_url": "https://fake.invalid",
             "consumer_id": "c-old", "initial_boundary_seq": 5,
             "cursor_seq": 5}
    state.update(extra)
    return state


class LegacyAndSchemaTests(WatcherCase):
    def _seed_pending(self):
        return {"batch_id": "b-old", "events": [sample_event()],
                "through_seq": 1}

    def test_legacy_initialized_state_rejected_before_init_skip(self):
        self.write_state(legacy_state())
        fake = self.install(lambda rec: (200, consumer_payload()))
        os.environ["WATCH_URL"] = "https://fake.invalid"
        rc, _, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1"])
        self.assertNotEqual(rc, 0)
        self.assertIn("重新 init", err)
        self.assertEqual(fake.requests, [])  # 幂等跳过被拒，未复用旧 consumer

    def test_legacy_state_rejected_before_wait_pending_replay(self):
        self.write_state(legacy_state(pending=self._seed_pending()))
        self.install(lambda rec: (404, ERR_BODY))
        rc, out, err = self.run_cli(["wait"])
        self.assertNotEqual(rc, 0)
        self.assertIn("重新 init", err)
        self.assertNotIn("重放", out)  # 未离线重放旧批次
        self.assertNotIn("batch_id: b-old", out)
        # pending 不丢
        self.assertEqual(self.state()["pending"]["batch_id"], "b-old")

    def test_legacy_state_rejected_before_ack(self):
        self.write_state(legacy_state(pending=self._seed_pending()))
        os.environ["WATCH_URL"] = "https://fake.invalid"
        self.install(lambda rec: (404, ERR_BODY))
        rc, _, err = self.run_cli(["ack", "--batch-id", "b-old"])
        self.assertNotEqual(rc, 0)
        self.assertIn("重新 init", err)
        self.assertEqual(self.state()["pending"]["batch_id"], "b-old")

    def test_legacy_unfinished_state_continues_and_upgrades(self):
        """仅登记参数、未创建 consumer 的旧状态可继续（升级为当前格式）。"""
        self.write_state({"subscription_key": "s1", "repository_id": "1001",
                          "start": "now",
                          "watch_url": "https://fake.invalid",
                          "consumer_id": None})
        self.install(lambda rec: (200, consumer_payload())
                     if rec.method == "POST" and rec.path == "/v1/consumers"
                     else (404, ERR_BODY))
        out = self.do_init("https://fake.invalid", key="s1")
        self.assertIn("consumer_id=c-1", out)
        state = self.state()
        self.assertEqual(state["schema_version"], ww.STATE_SCHEMA_VERSION)
        self.assertEqual(state["owner_github_user_id"], "100")

    def test_new_state_initialized_without_owner_rejected(self):
        """schema_version=1 但已初始化且 owner 缺失：重放/init 跳过/ack 均拒绝。"""
        self.write_state(legacy_state(schema_version=1, pending=self._seed_pending()))
        self.install(lambda rec: (404, ERR_BODY))
        os.environ["WATCH_URL"] = "https://fake.invalid"
        # wait 不能离线重放
        rc, out, err = self.run_cli(["wait"])
        self.assertNotEqual(rc, 0)
        self.assertIn("重新 init", err)
        self.assertNotIn("重放", out)
        # init 幂等跳过路径同拒
        rc, _, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1"])
        self.assertNotEqual(rc, 0)
        self.assertIn("重新 init", err)
        # ack 路径同拒
        rc, _, err = self.run_cli(["ack", "--batch-id", "b-old"])
        self.assertNotEqual(rc, 0)
        # pending 不丢
        self.assertEqual(self.state()["pending"]["batch_id"], "b-old")

    def test_new_state_initialized_with_invalid_owner_rejected(self):
        for uid in ("0", "abc", "-1", "01", "", 101):
            with self.subTest(uid=uid):
                self.write_state(legacy_state(
                    schema_version=1, pending=self._seed_pending(),
                    owner_github_user_id=uid))
                self.install(lambda rec: (404, ERR_BODY))
                rc, _, err = self.run_cli(["wait"])
                self.assertNotEqual(rc, 0, repr(uid))
                self.assertIn("owner", err)

    def test_state_future_schema_version_rejected(self):
        self.write_state(legacy_state(schema_version=2))
        os.environ["WATCH_URL"] = "https://fake.invalid"
        rc, _, err = self.run_cli(["wait"])
        self.assertNotEqual(rc, 0)
        self.assertIn("schema_version", err)
        rc, _, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1"])
        self.assertNotEqual(rc, 0)

    def test_new_state_pending_replay_still_offline(self):
        """新格式状态保留已下载 pending 的离线重放。"""
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        os.environ["WATCH_URL"] = "https://fake.invalid"
        self.install(lambda rec: (200, batch_payload("b1", [sample_event()])))
        rc, _, _ = self.run_cli(["wait"])
        self.assertEqual(rc, 0)
        # 离线重放：不再请求服务端
        fake = self.install(lambda rec: (404, ERR_BODY))
        rc, out, _ = self.run_cli(["wait"])
        self.assertEqual(rc, 0)
        self.assertIn("重放", out)
        self.assertIn("batch_id: b1", out)
        self.assertEqual(fake.requests, [])

    def test_state_file_never_contains_token_or_gh_credential(self):
        """状态与凭据分离：状态目录不含令牌，凭据目录不在状态目录。"""
        self.clear_creds()
        calls = self.patch_gh()

        def fn(rec):
            if rec.method == "POST" and rec.path == "/v1/auth/token":
                return 200, {"message_token": SRV_TOKEN, "owner": OWNER}
            if rec.method == "POST" and rec.path == "/v1/consumers":
                return 200, consumer_payload()
            return 404, ERR_BODY

        self.install(fn)
        self.do_init("https://fake.invalid", key="s1",
                     trust_server="https://fake.invalid")
        with open(os.path.join(self.sdir, "webhook-watch.json"),
                  encoding="utf-8") as f:
            contents = f.read()
        self.assertNotIn(SRV_TOKEN, contents)
        self.assertNotIn(GH_TOKEN, contents)
        cred = self.cred_path()
        self.assertFalse(cred.startswith(self.sdir))


class InitOwnerPreservationTests(WatcherCase):
    def test_retry_preserves_owner_despite_credential_switch(self):
        base = "https://fake.invalid"
        self.save_creds(token=SRV_TOKEN, owner=OWNER)  # owner 100
        os.environ["WATCH_URL"] = base
        calls = []

        def fn(rec):
            if rec.method == "POST" and rec.path == "/v1/consumers":
                calls.append(rec)
                return 500, ERR_BODY  # 响应丢失
            return 404, ERR_BODY

        self.install(fn)
        rc, _, _ = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1"])
        self.assertEqual(rc, ww.EXIT_NETWORK)
        self.assertEqual(int(self.state()["owner_github_user_id"]), 100)
        self.assertIsNone(self.state()["consumer_id"])
        # 用户切换到另一个用户的凭据重试：
        # 状态已登记 owner 100 必须保留并用于校验，不得静默换 owner
        self.save_creds(token="token-of-102",
                        owner={"github_user_id": "102", "github_login": "bob"})

        def fn2(rec):
            if rec.method == "POST" and rec.path == "/v1/consumers":
                return 200, consumer_payload(cid="c-2", owner={
                    "github_user_id": "102", "github_login": "bob"})
            return 404, ERR_BODY

        self.install(fn2)
        rc, _, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1"])
        self.assertNotEqual(rc, 0)
        self.assertIn("不一致", err)
        self.assertIsNone(self.state()["consumer_id"])
        self.assertEqual(int(self.state()["owner_github_user_id"]), 100)
        # 响应 owner 与状态登记一致（用户切回原账号凭据）时可以继续
        self.save_creds(token=SRV_TOKEN, owner=OWNER)
        self.install(lambda rec: (200, consumer_payload(cid="c-2"))
                     if rec.method == "POST" and rec.path == "/v1/consumers"
                     else (404, ERR_BODY))
        rc, out, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.state()["consumer_id"], "c-2")
        self.assertEqual(int(self.state()["owner_github_user_id"]), 100)

    def test_login_keyboard_interrupt_exits_130(self):
        os.environ["WATCH_URL"] = "https://fake.invalid"
        patcher = mock.patch.object(ww, "_gh_token",
                                    side_effect=KeyboardInterrupt)
        patcher.start()
        self.addCleanup(patcher.stop)
        rc, _, err = self.run_cli(["login", "--trust-server", "https://fake.invalid"])
        self.assertEqual(rc, ww.EXIT_INTERRUPTED)
        self.assertIn("已中断", err)
        self.assertNotIn("Traceback", err)


class ErrorRedactionTests(WatcherCase):
    def test_unknown_error_code_body_not_echoed(self):
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        os.environ["WATCH_URL"] = "https://fake.invalid"
        self.install(lambda rec: (400, {"error": {"code": "totally_unknown",
                                                  "message": "SECRET-XYZ"}}))
        rc, _, err = self.run_cli(["wait"])
        self.assertNotEqual(rc, 0)
        self.assertIn("HTTP 400", err)
        self.assertNotIn("SECRET-XYZ", err)
        self.assertNotIn("totally_unknown", err)
        self.assertNotIn("Traceback", err)

    def test_known_error_code_fixed_local_text(self):
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        os.environ["WATCH_URL"] = "https://fake.invalid"
        self.install(lambda rec: (401, {"error": {"code": "unauthorized",
                                                  "message": "SECRET-XYZ"}}))
        rc, _, err = self.run_cli(["wait"])
        self.assertEqual(rc, ww.EXIT_AUTH)
        self.assertIn("令牌无效或未授权", err)
        self.assertNotIn("SECRET-XYZ", err)

    def test_network_error_not_echoed(self):
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        os.environ["WATCH_URL"] = "https://fake.invalid"

        def fn(rec):
            raise urllib.error.URLError("conn refused LEAK-XYZ")

        self.install(fn)
        rc, _, err = self.run_cli(["wait"])
        self.assertEqual(rc, ww.EXIT_NETWORK)
        self.assertIn("网络错误", err)
        self.assertNotIn("LEAK-XYZ", err)
        self.assertNotIn("Traceback", err)

    def test_non_dict_response_rejected(self):
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        os.environ["WATCH_URL"] = "https://fake.invalid"
        self.install(lambda rec: (200, b'[1, 2]')
                     if base_path(rec).endswith("/events")
                     else (404, ERR_BODY))
        rc, _, err = self.run_cli(["wait"])
        self.assertNotEqual(rc, 0)
        self.assertIn("格式异常", err)
        self.assertNotIn("Traceback", err)
        self.assertIsNone(self.state().get("pending"))

    def test_ack_not_ok_no_response_echo(self):
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        os.environ["WATCH_URL"] = "https://fake.invalid"
        self.install(lambda rec: (200, batch_payload("b1", [sample_event()])))
        rc, _, _ = self.run_cli(["wait"])
        self.assertEqual(rc, 0)
        self.install(lambda rec: (200, {"ok": False, "owner": OWNER,
                                        "secret_reflection": "ACK-SECRET"}))
        rc, _, err = self.run_cli(["ack", "--batch-id", "b1"])
        self.assertNotEqual(rc, 0)
        self.assertNotIn("ACK-SECRET", err)
        self.assertNotIn("{", err)
        self.assertEqual(self.state()["pending"]["batch_id"], "b1")


class BaseNormalizationTests(WatcherCase):
    def _init_with_url(self, url):
        os.environ["WATCH_URL"] = url
        self.install(lambda rec: (200, consumer_payload())
                     if rec.method == "POST" and rec.path.endswith("/v1/consumers")
                     else (404, ERR_BODY))
        rc, out, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1"])
        return rc, err

    def test_canonical_lowercase_host(self):
        rc, err = self._init_with_url("https://Fake.invalid/")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.state()["watch_url"], "https://fake.invalid")

    def test_default_https_port_dropped(self):
        self.save_creds(base="https://fake.invalid:443")  # 规范化后同一路径
        rc, err = self._init_with_url("https://fake.invalid:443")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.state()["watch_url"], "https://fake.invalid")

    def test_path_preserved(self):
        self.save_creds(base="https://fake.invalid/api/hook/")
        rc, err = self._init_with_url("https://fake.invalid/api/hook/")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.state()["watch_url"],
                         "https://fake.invalid/api/hook")

    def test_invalid_port_fixed_error_no_echo(self):
        rc, err = self._init_with_url("https://example.invalid:99999")
        self.assertNotEqual(rc, 0)
        self.assertIn("端口", err)
        self.assertNotIn("99999", err)

    def test_control_characters_rejected(self):
        rc, err = self._init_with_url("https://example.invalid/a\x0bb")
        self.assertNotEqual(rc, 0)
        self.assertIn("控制字符", err)

    def test_http_default_port_loopback_canonical(self):
        self.save_creds(base="http://127.0.0.1:80")
        rc, err = self._init_with_url("http://127.0.0.1:80")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.state()["watch_url"], "http://127.0.0.1")


class OwnerStrictTests(WatcherCase):
    def test_validate_owner_unit(self):
        owner = ww._validate_owner({"github_user_id": "101",
                                    "github_login": "alice",
                                    "extra_field": "drop-me"})
        self.assertEqual(owner, {"github_user_id": "101",
                                 "github_login": "alice"})
        # 单字符与 39 字符 login 合法（- 不能在首尾）
        self.assertEqual(ww._validate_owner({"github_user_id": "101",
                                             "github_login": "a"})
                         ["github_login"], "a")
        self.assertEqual(ww._validate_owner({"github_user_id": "101",
                                             "github_login": "a" * 39})
                         ["github_login"], "a" * 39)
        # id 边界: ASCII 正整数 1..30 位
        self.assertTrue(ww._valid_owner_id("1" * 30))
        for uid in ("1" * 31, "0", "01", "-1", "10-1", 101, " 1", "1.0",
                    "١٠", "²", None, ""):
            self.assertFalse(ww._valid_owner_id(uid), repr(uid))
            with self.assertRaises(ww.WatcherError, msg=uid):
                ww._validate_owner({"github_user_id": uid,
                                    "github_login": "al"})
        bad = [
            {}, "x",
            {"github_user_id": "0", "github_login": "al"},
            {"github_user_id": "-1", "github_login": "al"},
            {"github_user_id": "01", "github_login": "al"},
            {"github_user_id": 101, "github_login": "al"},
            {"github_user_id": "10-1", "github_login": "al"},
            {"github_user_id": "101", "github_login": "bad name"},
            {"github_user_id": "101", "github_login": "-al"},
            {"github_user_id": "101", "github_login": "al-"},
            {"github_user_id": "101", "github_login": "a" * 40},
            {"github_user_id": "101", "github_login": "octo\n"},
        ]
        for case in bad:
            with self.assertRaises(ww.WatcherError, msg=case):
                ww._validate_owner(case)

    def test_login_owner_extra_fields_not_persisted(self):
        self.clear_creds()
        self.patch_gh()
        os.environ["WATCH_URL"] = "https://fake.invalid"
        self.install(lambda rec: (200, {"message_token": SRV_TOKEN,
                                        "owner": {**OWNER,
                                                  "scopes": "repo"}})
                     if rec.method == "POST" and rec.path == "/v1/auth/token"
                     else (404, ERR_BODY))
        rc, _, err = self.run_cli(["login", "--trust-server", "https://fake.invalid"])
        self.assertEqual(rc, 0, err)
        with open(self.cred_path(), encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["owner"], OWNER)

    def test_issued_token_control_chars_rejected(self):
        self.clear_creds()
        self.patch_gh()
        os.environ["WATCH_URL"] = "https://fake.invalid"
        for token in ("tok\r\nen", "tok en", "tok\ten"):
            self.install(lambda rec, tok=token: (200, {"message_token": tok,
                                                       "owner": OWNER})
                         if rec.method == "POST"
                         and rec.path == "/v1/auth/token"
                         else (404, ERR_BODY))
            rc, _, err = self.run_cli(
                ["login", "--trust-server", "https://fake.invalid"])
            self.assertNotEqual(rc, 0, repr(token))
            self.assertIn("非法字符", err)
            self.assertFalse(os.path.exists(self.cred_path()))

    def test_saved_token_control_chars_rejected(self):
        self.save_creds(token=SRV_TOKEN)
        path = self.cred_path()
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data["message_token"] = "bad\ntoken"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.environ["WATCH_URL"] = "https://fake.invalid"
        self.install(lambda rec: (200, consumer_payload()))
        rc, _, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1"])
        self.assertNotEqual(rc, 0)
        self.assertIn("非法字符", err)

    def test_credential_schema_version_checked(self):
        """凭据文件缺 schema_version 或版本不符：固定错误拒绝，不重签发。"""
        self.save_creds(token=SRV_TOKEN)
        path = self.cred_path()
        for mutate in (lambda d: d.pop("schema_version"),
                       lambda d: d.update(schema_version=2)):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            mutate(data)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.environ["WATCH_URL"] = "https://fake.invalid"
            self.install(lambda rec: (200, consumer_payload()))
            rc, _, err = self.run_cli(
                ["init", "--repository-id", "1001", "--subscription-key", "s1"])
            self.assertNotEqual(rc, 0)
            self.assertIn("schema_version", err)
            self.assertIn("凭据文件", err)

    def test_read_credentials_symlink_rejected(self):
        """凭据文件是 symlink：拒绝（不跟随），不当无凭据处理。"""
        if os.name == "nt":
            self.skipTest("symlink 创建需特权，POSIX 断言")
        real = os.path.join(self.sdir, "real-cred.json")
        with open(real, "w", encoding="utf-8") as f:
            json.dump({"schema_version": 1, "watch_url": "https://fake.invalid",
                       "message_token": SRV_TOKEN, "owner": OWNER}, f)
        os.chmod(real, 0o600)
        link = self.cred_path()
        self.clear_creds()  # setUp 注入的默认凭据会占位，先移除再放 symlink
        os.symlink(real, link)
        os.environ["WATCH_URL"] = "https://fake.invalid"
        self.install(lambda rec: (200, consumer_payload()))
        rc, _, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1"])
        self.assertNotEqual(rc, 0)
        self.assertIn("symlink", err)
        self.assertNotIn(SRV_TOKEN, err)

    def test_read_credentials_rejects_wrong_owner_file(self):
        """凭据文件属主/权限不符：拒绝（防读取劫持与放宽权限）。"""
        if os.name == "nt":
            self.skipTest("POSIX 权限断言")
        path = self.cred_path()
        self.save_creds()
        os.chmod(path, 0o640)
        os.environ["WATCH_URL"] = "https://fake.invalid"
        self.install(lambda rec: (200, consumer_payload()))
        rc, _, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1"])
        self.assertNotEqual(rc, 0)
        self.assertIn("chmod 600", err)
        self.assertNotIn(SRV_TOKEN, err)

    def test_login_credentials_file_passthrough(self):
        """login 支持 --credentials-file：把新凭据写回绑定/自定义路径。"""
        custom = os.path.join(self.sdir, "custom-cred.json")
        self.patch_gh()
        os.environ["WATCH_URL"] = "https://fake.invalid"
        self.install(lambda rec: (200, {"message_token": SRV_TOKEN,
                                        "owner": OWNER})
                     if rec.method == "POST" and rec.path == "/v1/auth/token"
                     else (404, ERR_BODY))
        rc, _, err = self.run_cli(["login", "--trust-server",
                                   "https://fake.invalid",
                                   "--credentials-file", custom])
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.exists(custom))
        with open(custom, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["message_token"], SRV_TOKEN)
        # init 同样透传：该自定义路径作为凭据源
        self.install(lambda rec: (200, consumer_payload())
                     if rec.method == "POST" and rec.path == "/v1/consumers"
                     else (404, ERR_BODY))
        rc, _, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1",
             "--credentials-file", custom])
        self.assertEqual(rc, 0, err)

    def test_wait_local_owner_mismatch_refused_without_remote(self):
        """wait：本地凭据 owner 与状态 owner 不符先拒绝（不靠远端 404）。"""
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        self.save_creds(token="token-eve",
                        owner={"github_user_id": "200", "github_login": "eve"})
        fake = self.install(lambda rec: (404, ERR_BODY))
        rc, _, err = self.run_cli(["wait"])
        self.assertEqual(rc, ww.EXIT_AUTH)
        self.assertIn("owner", err)
        self.assertEqual(fake.requests, [])  # 未发起网络请求

    def test_ack_impl_strict_owner_contract(self):
        """ack_impl：无 owner 参数属旧契约，拒绝（无静默绕过）。"""
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        with self.assertRaisesRegex(ww.WatcherError, "owner"):
            ww.ack_impl(self.sdir, "whatever", "https://fake.invalid",
                        SRV_TOKEN, None)  # 无 owner（旧契约）必须拒绝

    def test_message_token_refusal_requests_unset_first(self):
        """提示必须明确先 unset，否则 login 也会再次被拒。"""
        self.set_message_token()
        os.environ["WATCH_URL"] = "https://fake.invalid"
        rc, _, err = self.run_cli(["login", "--trust-server",
                                   "https://fake.invalid"])
        self.assertNotEqual(rc, 0)
        self.assertIn("unset MESSAGE_TOKEN", err)

    def test_wait_and_ack_credentials_file_passthrough(self):
        """自定义 credentials 路径：init→wait→ack 全链路读同一文件；
        默认路径不参与（无默认缓存时不误用）。"""
        custom = os.path.join(self.sdir, "run-cred.json")
        self.clear_creds()  # 默认路径保持为空，防止被误用
        self.patch_gh()
        os.environ["WATCH_URL"] = "https://fake.invalid"

        def fn(rec):
            if rec.method == "POST" and rec.path == "/v1/auth/token":
                return 200, {"message_token": SRV_TOKEN, "owner": OWNER}
            if rec.method == "POST" and rec.path == "/v1/consumers":
                return 200, consumer_payload()
            if base_path(rec).endswith("/events"):
                return 200, batch_payload("b1", [sample_event()])
            if base_path(rec).endswith("/ack"):
                return 200, {"ok": True, "batch_id": "b1", "owner": OWNER}
            return 404, ERR_BODY

        self.install(fn)
        rc, _, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1",
             "--trust-server", "https://fake.invalid",
             "--credentials-file", custom])
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.exists(custom))
        # wait 用同一路径（默认路径为空也能继续）
        rc, out, err = self.run_cli(
            ["wait", "--credentials-file", custom])
        self.assertEqual(rc, 0, err)
        self.assertIn("batch_id: b1", out)
        rc, _, err = self.run_cli(
            ["ack", "--batch-id", "b1", "--credentials-file", custom])
        self.assertEqual(rc, 0, err)
        self.assertIsNone(self.state()["pending"])
        # 默认路径上没有任何凭据（全程未使用）
        self.assertFalse(os.path.exists(self.cred_path()))

    def test_wait_default_path_missing_even_when_custom_used_for_init(self):
        """init 用了自定义路径、wait 未传同一路径：明确认证错误（不是静默
        回落到默认路径或其他凭据）。"""
        self.clear_creds()
        self.patch_gh()
        os.environ["WATCH_URL"] = "https://fake.invalid"
        custom = os.path.join(self.sdir, "run-cred.json")

        def fn(rec):
            if rec.method == "POST" and rec.path == "/v1/auth/token":
                return 200, {"message_token": SRV_TOKEN, "owner": OWNER}
            if rec.method == "POST" and rec.path == "/v1/consumers":
                return 200, consumer_payload()
            return 404, ERR_BODY

        self.install(fn)
        rc, _, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1",
             "--trust-server", "https://fake.invalid",
             "--credentials-file", custom])
        self.assertEqual(rc, 0, err)
        fake = self.install(lambda rec: (404, ERR_BODY))
        rc, _, err = self.run_cli(["wait"])
        self.assertEqual(rc, ww.EXIT_AUTH)
        self.assertIn("没有已保存的个人凭据", err)
        self.assertEqual(fake.requests, [])

    def test_credentials_root_uses_platform_config_dir(self):
        # setUp 已将 _credentials_root patch 到临时目录；用导入时捕获的
        # 真实实现验证平台目录解析逻辑
        if os.name == "nt":
            old, env = os.environ.get("APPDATA"), {}
            old_xdg = os.environ.get("XDG_CONFIG_HOME")
        else:
            old, env = None, None
        tmp = tempfile.mkdtemp(prefix="ww_root_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        if os.name == "nt":
            self.addCleanup(self._restore_env_key, "APPDATA", old)
            os.environ["APPDATA"] = tmp
            root = _REAL_CREDENTIALS_ROOT()
            self.assertTrue(root.startswith(tmp))
            self.assertTrue(root.endswith(os.path.join("webhook-watch")))
        else:
            self.addCleanup(self._restore_env_key, "XDG_CONFIG_HOME", old)
            os.environ["XDG_CONFIG_HOME"] = tmp
            root = _REAL_CREDENTIALS_ROOT()
            self.assertEqual(root, os.path.join(tmp, "webhook-watch"))

    @staticmethod
    def _restore_env_key(key, value):
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class SaveTmpTests(WatcherCase):
    def test_preexisting_open_tmp_is_tightened_before_write(self):
        if os.name == "nt":
            self.skipTest("POSIX 权限断言")
        path = self.cred_path()
        self.save_creds(token=SRV_TOKEN)
        self.assertFalse(os.path.exists(path + ".tmp"))  # 未用固定 .tmp 名
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(path)).st_mode & 0o777, 0o700)

    def test_tmp_removed_when_save_fails(self):
        self.clear_creds()  # 起始为无凭据，验证失败不留任何文件
        path = self.cred_path()
        self.assertFalse(os.path.exists(path))
        with mock.patch.object(ww.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                ww.save_credentials_file(path, "https://fake.invalid",
                                         SRV_TOKEN, OWNER)
        # 失败时临时文件（唯一名，按前缀）不留明文 Token
        leftovers = [name for name in os.listdir(os.path.dirname(path))
                     if name.startswith(os.path.basename(path) + ".")
                     and name.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        self.assertFalse(os.path.exists(path))

    def test_save_does_not_follow_preplaced_tmp_symlink(self):
        if os.name == "nt":
            self.skipTest("symlink 创建需特权，POSIX 断言")
        path = self.cred_path()
        # 攻击者预置固定 .tmp 名 symlink：写入须用唯一临时名，不跟随
        bad_target = os.path.join(os.path.dirname(path), "victim.txt")
        with open(bad_target, "w", encoding="utf-8") as f:
            f.write("innocent")
        os.symlink(bad_target, path + ".tmp")
        self.addCleanup(os.unlink, path + ".tmp")
        self.addCleanup(os.unlink, bad_target)
        self.save_creds(token=SRV_TOKEN)
        self.assertTrue(os.path.islink(path + ".tmp"))  # 未被跟随/复用
        with open(bad_target, encoding="utf-8") as f:
            self.assertEqual(f.read(), "innocent")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["message_token"], SRV_TOKEN)


class ReuseImplTests(WatcherCase):
    """webhook_watch 供 task_watch 复用的实现层（个人凭据/owner 协议）。"""

    def test_init_impl_with_explicit_base_token_and_owner(self):
        fake = self.install(lambda rec: (200, consumer_payload()))
        state = ww.init_impl(self.sdir, "https://fake.invalid", SRV_TOKEN,
                             "1001", "run-x", "now", credential_owner=OWNER)
        self.assertEqual(state["consumer_id"], "c-1")
        self.assertEqual(state["schema_version"], 1)
        self.assertEqual(state["owner_github_user_id"], "100")
        self.assertEqual(self.state()["watch_url"], "https://fake.invalid")
        self.assertEqual(len(fake.requests), 1)
        # 幂等：重复调用（离线）跳过
        fake2 = self.install(lambda rec: (404, ERR_BODY))
        state = ww.init_impl(self.sdir, "https://fake.invalid", SRV_TOKEN,
                             "1001", "run-x", "now", credential_owner=OWNER)
        self.assertEqual(state["consumer_id"], "c-1")
        self.assertEqual(fake2.requests, [])

    def test_init_impl_without_owner_rejected(self):
        """新契约：init_impl 的 credential_owner 必须有效（无 None 绕过）。"""
        with self.assertRaisesRegex(ww.WatcherError, "owner"):
            ww.init_impl(self.sdir, "https://fake.invalid", SRV_TOKEN,
                         "1001", "run-x", "now")
        with self.assertRaisesRegex(ww.WatcherError, "owner"):
            ww.init_impl(self.sdir, "https://fake.invalid", SRV_TOKEN,
                         "1001", "run-x", "now", credential_owner={
                             "github_user_id": "invalid-id",
                             "github_login": "octo"})

    def test_poll_once_and_persist_pending_match_cli_wait(self):
        # 先用 CLI init
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        os.environ["WATCH_URL"] = "https://fake.invalid"
        self.install(
            lambda rec: (200, batch_payload("b9", [sample_event(seq=2)])))
        state = self.state()
        resp = ww.poll_once("https://fake.invalid", "c-1", SRV_TOKEN,
                            state=state)
        self.assertEqual(resp["batch_id"], "b9")
        got = ww.persist_pending(self.sdir, state, resp)
        self.assertTrue(got)
        saved = self.state()
        self.assertEqual(saved["pending"]["batch_id"], "b9")
        self.assertEqual(saved["pending"]["events"][0]["seq"], 2)
        # 空批次：不落盘
        self.assertFalse(ww.persist_pending(self.sdir, state,
                                            batch_payload(None, [])))
        # 缺 batch_id 却有事件：报错防归属不明
        with self.assertRaises(ww.WatcherError):
            ww.persist_pending(self.sdir, state,
                               {"batch_id": None, "events": [sample_event()],
                                "owner": OWNER})
        # 无 owner 的响应：防御校验拒绝落盘
        bad = batch_payload("bX", [sample_event(seq=3)])
        bad.pop("owner")
        with self.assertRaisesRegex(ww.WatcherError, "owner"):
            ww.persist_pending(self.sdir, state, bad)
        self.assertEqual(self.state()["pending"]["batch_id"], "b9")  # 不丢

    def test_ack_impl_with_explicit_base_token_and_owner(self):
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        os.environ["WATCH_URL"] = "https://fake.invalid"
        self.install(lambda rec: (200, batch_payload("b1", [sample_event()])))
        rc, _, err = self.run_cli(["wait"])
        self.assertEqual(rc, 0, err)
        acks = []

        def fn(rec):
            if rec.method == "POST" and rec.path.endswith("/ack"):
                acks.append(rec)
                return 200, {"ok": True, "batch_id": "b1", "owner": OWNER}
            return 404, ERR_BODY

        self.install(fn)
        ww.ack_impl(self.sdir, "b1", "https://fake.invalid", SRV_TOKEN,
                    credential_owner=OWNER)
        self.assertIsNone(self.state()["pending"])
        self.assertEqual(
            (acks[0].body and json.loads(acks[0].body))["batch_id"], "b1")

    def test_impl_rejects_owner_mismatch_across_impl_layer(self):
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        other = {"github_user_id": "200", "github_login": "eve"}
        with self.assertRaisesRegex(ww.WatcherError, "owner"):
            ww.init_impl(self.sdir, "https://fake.invalid", SRV_TOKEN,
                         "1001", "s1", "now", credential_owner=other)
        # persist_pending：响应 owner 与状态不符 → 不落盘
        resp = batch_payload("b1", [sample_event()], owner=other)
        with self.assertRaisesRegex(ww.WatcherError, "owner"):
            ww.persist_pending(self.sdir, self.state(), resp)
        self.assertIsNone(self.state().get("pending"))
        # ack_impl：凭据 owner 与状态 owner 不一致 → 拒绝（造出 pending 后）
        self.install(lambda rec: (200, batch_payload("b1", [sample_event()])))
        rc, _, err = self.run_cli(["wait"])
        self.assertEqual(rc, 0, err)
        self.save_creds(token="other-token", owner=other)
        with self.assertRaisesRegex(ww.WatcherError, "owner"):
            ww.ack_impl(self.sdir, "b1", "https://fake.invalid", "other-token",
                        credential_owner=other)
        self.assertEqual(self.state()["pending"]["batch_id"], "b1")  # pending 不丢

    def test_lock_file_rejects_other_name_busy(self):
        # 同一目录两把不同名字的锁互不阻塞；同名锁则互斥
        with ww._lock_file(self.sdir, "other.lock"):
            with ww._lock_file(self.sdir, ww.LOCK_FILE):
                pass  # webhook 锁可独立持有
        with ww._lock_file(self.sdir, ww.LOCK_FILE):
            with self.assertRaises(ww.LockBusy):
                with ww._lock_file(self.sdir, ww.LOCK_FILE):
                    pass

    def test_non_object_state_is_rejected(self):
        path = os.path.join(self.sdir, ww.STATE_FILE)
        with open(path, "w", encoding="utf-8") as stream:
            json.dump([], stream)
        with self.assertRaisesRegex(ww.WatcherError, "JSON 对象"):
            ww._load_state(self.sdir)

    def test_lock_filesystem_error_is_not_busy(self):
        with mock.patch.object(ww.os, "open", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                with ww._lock_file(self.sdir, "denied.lock"):
                    self.fail("lock should not be acquired")


# ------------------------------------------------- 真实回环 socket 用例

def _handler_for(fn):
    class Handler(BaseHTTPRequestHandler):
        log_message = lambda *a, **k: None  # noqa: E731

        def _run(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            rec = SimpleNamespace(method=self.command, path=self.path,
                                  body=body)
            try:
                status, payload = fn(rec)
            except Exception as exc:
                status, payload = (500, {"error": {"code": "mock",
                                                   "message": repr(exc)}})
            if status in (301, 302, 307, 308):
                self.send_response(status)
                for key, value in payload.items():
                    self.send_header(key, str(value))
                self.end_headers()
                return
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = _run
        do_POST = _run

    return Handler


class RealHttpTests(WatcherCase):
    """单个真实 socket 用例：验证 opener 构造与真实回环请求。"""

    def test_real_loopback_init_and_redirect_rejected(self):
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), _handler_for(
                lambda rec: (200, consumer_payload())
                if rec.method == "POST" and rec.path == "/v1/consumers"
                else ((302, {"Location": "/evil"})
                      if rec.path.split("?")[0].endswith("/events")
                      else (404, ERR_BODY))))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)   # LIFO：shutdown 先于 close
        self.addCleanup(server.shutdown)
        os.environ["WATCH_URL"] = "http://127.0.0.1:%d" \
            % server.server_address[1]
        # 消费类请求不需要签发：为该回环地址注入已保存凭据（wait 不需要 gh）
        self.save_creds(base=os.environ["WATCH_URL"])
        rc, out, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1"])
        self.assertEqual(rc, 0, err)
        rc, out, err = self.run_cli(["wait"])
        self.assertNotEqual(rc, 0)
        self.assertIn("重定向", err)
        # 无凭据泄漏进输出
        self.assertNotIn(SRV_TOKEN, out)
        self.assertNotIn(GH_TOKEN, out)
        self.assertNotIn("srv-message-token", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)