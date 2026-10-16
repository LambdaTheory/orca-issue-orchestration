#!/usr/bin/env python3
"""webhook_watch 的单元测试（仅标准库，mock HTTP）。

- 多数用例通过 FakeOpener 直接把请求交给测试函数，不走真实 socket
  （Windows 上高频短连接会因临时端口复用出现 WinError 10048 抖动）。
- 保留一个真实回环 socket 用例验证 opener 构造（重定向拒绝）。
运行方式: python tests/test_watcher.py
"""
import contextlib
import io
import json
import os
import shutil
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


def base_path(rec):
    """请求路径去掉查询串。"""
    return rec.path.split("?", 1)[0]


def consumer_payload(cid="c-1", boundary=5):
    return {"consumer_id": cid, "initial_boundary_seq": boundary,
            "cursor_seq": boundary}


def sample_event(seq=1, kind="issue", number=42):
    return {"seq": seq, "event": "issue_comment", "action": "created",
            "repository_id": 1001, "repository": "octo/hello",
            "subject": {"kind": kind, "number": number}, "actor": "alice"}


def batch_payload(batch_id="b1", events=None):
    return {"consumer_id": "c-1", "batch_id": batch_id,
            "through_seq": (events[-1]["seq"] if events else 0),
            "events": events or []}


class FakeOpener:
    """替代 ww._OPENER：直接把请求交给 fn(record)->(status, payload)。"""

    def __init__(self, fn):
        self.fn = fn
        self.requests = []

    def open(self, req, timeout=None):
        split = urlsplit(req.full_url)
        path = split.path + ("?" + split.query if split.query else "")
        rec = SimpleNamespace(method=req.get_method(), path=path,
                              body=req.data)
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
        self._old_env = {k: os.environ.get(k)
                         for k in ("MESSAGE_TOKEN", "WATCH_URL", "WATCH_DIR")}
        os.environ["MESSAGE_TOKEN"] = "test-token"
        os.environ["WATCH_DIR"] = self.sdir
        os.environ.pop("WATCH_URL", None)
        self.addCleanup(self._restore_env)
        sleeper = mock.patch.object(ww, "RETRY_SLEEP", 0)
        sleeper.start()
        self.addCleanup(sleeper.stop)

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
        argv += sum((["--" + k, str(v)] for k, v in kwargs.items()), [])
        rc, out, err = self.run_cli(argv)
        self.assertEqual(rc, 0, "init 失败: %s" % err)
        return out

    def _init_with(self, url, fn):
        self.install(fn)
        self.do_init(url)


class InitTests(WatcherCase):
    def test_init_success_and_idempotent_offline(self):
        ok = lambda rec: (200, consumer_payload()) \
            if rec.method == "POST" and rec.path == "/v1/consumers" \
            else (404, ERR_BODY)  # noqa: E731
        self.install(ok)
        out = self.do_init("https://fake.invalid", key="run-abc")
        # 已初始化：再次 init 离线幂等跳过，不发起任何请求
        rc, out, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "run-abc"])
        self.assertEqual(rc, 0, err)
        self.assertIn("跳过重复创建", out)
        state = self.state()
        self.assertEqual(state["consumer_id"], "c-1")
        self.assertEqual(state["subscription_key"], "run-abc")

    def test_init_records_post_body(self):
        fake = self.install(lambda rec: (200, consumer_payload()))
        out = self.do_init("https://fake.invalid", key="run-abc")
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(json.loads(fake.requests[0].body),
                         {"subscription_key": "run-abc",
                          "repository_id": "1001", "start": "now"})
        self.assertIn("consumer_id=c-1", out)

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
        # init：同参数但服务端不同也被拒
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
        # 失败但稳定参数已落盘且 consumer_id 为空
        state = self.state()
        self.assertEqual(state["subscription_key"], "s1")
        self.assertEqual(state["repository_id"], "1001")
        self.assertEqual(state["start"], "now")
        self.assertIsNone(state["consumer_id"])
        # 重试复用同一参数
        rc, _, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(c["subscription_key"] == "s1" for c in calls))

    def test_init_missing_token(self):
        self.install(lambda rec: (200, consumer_payload()))
        os.environ.pop("MESSAGE_TOKEN")
        rc, _, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1"])
        self.assertEqual(rc, ww.EXIT_AUTH)
        self.assertIn("MESSAGE_TOKEN", err)

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
                return 200, {"ok": True, "batch_id": acks[-1]["batch_id"]}
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
                return (200, {"ok": True, "batch_id": "b1"}) \
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
        self.install(lambda rec: (200, {"ok": False}))
        rc, _, err = self.run_cli(["ack", "--batch-id", "b1"])
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.state()["pending"]["batch_id"], "b1")

    def test_ack_without_pending(self):
        self._init_with("https://fake.invalid",
                        lambda rec: (200, consumer_payload()))
        os.environ["WATCH_URL"] = "https://fake.invalid"
        fake = self.install(lambda rec: (404, ERR_BODY))
        rc, _, err = self.run_cli(["ack", "--batch-id", "b1"])
        self.assertNotEqual(rc, 0)
        self.assertIn("待确认批次", err)
        self.assertEqual(fake.requests, [])

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
        with contextlib.redirect_stdout(io.StringIO()) as buf2:
            with self.assertRaises(SystemExit) as cm:
                ww.main(["init", "--help"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("--repository-id", buf2.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as buf3:
            with self.assertRaises(SystemExit) as cm:
                ww.main(["wait", "--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_missing_subcommand(self):
        with self.assertRaises(SystemExit) as cm:
            ww.main([])
        self.assertEqual(cm.exception.code, 2)


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
        rc, out, err = self.run_cli(
            ["init", "--repository-id", "1001", "--subscription-key", "s1"])
        self.assertEqual(rc, 0, err)
        rc, out, err = self.run_cli(["wait"])
        self.assertNotEqual(rc, 0)
        self.assertIn("重定向", err)
        # 令牌仍在环境里（CLI 不应打印或清除它）且未被输出
        self.assertEqual(os.environ["MESSAGE_TOKEN"], "test-token")
        self.assertNotIn("test-token", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)