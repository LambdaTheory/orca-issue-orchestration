#!/usr/bin/env python3
"""task_watch 单元测试（仅标准库；对应当前 task_watch.py 契约）。

默认禁止真实 gh 子进程 / 真实 HTTP / 真实系统托管：
- tw.subprocess.run → AssertionError：gh 子查询必须安装 case 级 tw._run_gh 假件；
  （gh_path 用 sys.executable 指向已存在文件，仅用于通过绑定校验）
- ww._OPENER.open → AssertionError：HTTP 必须用 install_http安装 FakeHttp；
- tw.TaskService → FakeTaskService：prepare 只探测计数；ensure 注册并写匹配
  generation 的心跳；status 未注册返回 registered=False；remove 清状态。
  core 的 _wait_ready / collect_status / stop 走真实逻辑，不 mock 成功。
- FakeTime：time/monotonic/sleep 各有有限调用预算，越界抛 AssertionError
  （防死循环）；sleep 触发 on_sleep(seconds) 的钩子，长循环 case 用它改
  desired 结束（不使用生产型 MAX_STEPS 类测试开关）。
- 凭据文件 chmod 600；线程 case（真实 webhook 锁竞争/stop 遇在途批次）
  用真实时间，时长有界，不触真实服务。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time as real_time
import unittest
import urllib.error
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import task_watch as tw  # noqa: E402
import webhook_watch as ww  # noqa: E402

TOKEN_VALUE = "secret-token-123"
ERR_BODY = {"error": {"code": "t", "message": "t error"}}


def consumer_payload(cid="c-1", boundary=5):
    return {"consumer_id": cid, "initial_boundary_seq": boundary,
            "cursor_seq": boundary}


def sample_event(seq=1, number=42):
    return {"seq": seq, "event": "issue_comment", "action": "created",
            "repository_id": 1001, "repository": "octo/hello",
            "subject": {"kind": "issue", "number": number}, "actor": "alice"}


def batch_payload(batch_id="b1", events=None):
    return {"consumer_id": "c-1", "batch_id": batch_id,
            "through_seq": (events[-1]["seq"] if events else 0),
            "events": events or []}


# ---------------------------------------------------------------- 假件

class FakeTime:
    """可控时间：time/monotonic 返回可控值，越预算抛 AssertionError；
    sleep 前进时间并触发 on_sleep(seconds) 钩子（注入用）。"""

    def __init__(self, t=1_700_000_000.0):
        self.t = float(t)
        self.m = 0.0
        self.time_calls = 0
        self.monotonic_calls = 0
        self.sleep_calls = 0
        self.time_budget = 2000
        self.monotonic_budget = 2000
        self.sleep_budget = 32
        self.on_sleep = None

    def _check(self, used, budget, what):
        if used > budget:
            raise AssertionError(
                "time.%s 被调用 %d 次超过预算 %d（疑似无限循环）"
                % (what, used, budget))

    def time(self):
        self.time_calls += 1
        self._check(self.time_calls, self.time_budget, "time")
        return self.t

    def monotonic(self):
        self.monotonic_calls += 1
        self._check(self.monotonic_calls, self.monotonic_budget, "monotonic")
        return self.m

    def sleep(self, seconds):
        self.sleep_calls += 1
        self._check(self.sleep_calls, self.sleep_budget, "sleep")
        self.t += seconds
        self.m += seconds
        if self.on_sleep:
            self.on_sleep(seconds)


class FakeTaskService:
    """TaskService 假件：类级 state 平台状态。ensure 注册并写匹配
    generation 心跳；status 未注册→registered=False；remove 清状态。"""

    state = {}
    instances = []

    @classmethod
    def reset(cls):
        cls.state = {"registered": False, "running": False,
                     "prepare_calls": 0, "ensure_calls": 0, "remove_calls": 0,
                     "ensure_error": None, "status_error": None,
                     "remove_error": None, "last_ensure_binding": None}
        cls.instances = []

    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.calls = []
        type(self).instances.append(self)

    def prepare(self):
        self.calls.append("prepare")
        self.state["prepare_calls"] += 1
        return {"platform": "fake", "name": "orca-task-watch-fake",
                "state_dir": self.state_dir}

    @staticmethod
    def _write_heartbeat(state_dir):
        gen = tw._load_control(state_dir).get("generation", 0)
        tw._save_runtime(state_dir, {"pid": os.getpid(), "generation": gen,
                                     "heartbeat_ts": tw.time.time(),
                                     "desired_seen": "running",
                                     "quiesced": False})

    def ensure(self, binding):
        self.calls.append("ensure")
        self.state["ensure_calls"] += 1
        if self.state["ensure_error"]:
            raise tw.ServiceError(self.state["ensure_error"])
        self.state["registered"] = True
        self.state["running"] = True
        self.state["last_ensure_binding"] = dict(binding or {})
        self._write_heartbeat(self.state_dir)

    def status(self, _binding):
        self.calls.append("status")
        if self.state["status_error"]:
            raise tw.ServiceError(self.state["status_error"])
        running = self.state["registered"] and self.state["running"]
        return {"registered": self.state["registered"], "running": running,
                "detail": "fake"}

    def remove(self, _binding):
        self.calls.append("remove")
        self.state["remove_calls"] += 1
        if self.state["remove_error"]:
            raise tw.ServiceError(self.state["remove_error"])
        self.state["registered"] = False
        self.state["running"] = False


class NotRunningTaskService(FakeTaskService):
    """注册成功但平台进程未 running、也不写心跳：验证真实 ready 门。"""

    def ensure(self, binding):
        self.calls.append("ensure")
        self.state["ensure_calls"] += 1
        self.state["registered"] = True
        self.state["running"] = False

    def status(self, _binding):
        self.calls.append("status")
        if self.state["status_error"]:
            raise tw.ServiceError(self.state["status_error"])
        return {"registered": True, "running": False,
                "detail": "fake-not-running"}


class FakeHttp:
    """替代 ww._OPENER：请求交给 fn(record)->(status, payload)，全程记录。"""

    def __init__(self, fn=None):
        self.fn = fn or (lambda rec: (_ for _ in ()).throw(
            AssertionError("默认禁止真实 HTTP")))
        self.requests = []

    def open(self, req, timeout=None):
        split = urlsplit(req.full_url)
        path = split.path + ("?" + split.query if split.query else "")
        rec = SimpleNamespace(method=req.get_method(), path=path,
                              body=req.data)
        self.requests.append(rec)
        status, payload = self.fn(rec)
        data = (payload if isinstance(payload, bytes)
                else json.dumps(payload).encode("utf-8"))
        fp = io.BytesIO(data)
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "http", None, fp)
        return fp


# ---------------------------------------------------------------- 基类

class GWCase(unittest.TestCase):

    def setUp(self):
        self.sdir = tempfile.mkdtemp(prefix="tw_test_")
        self.addCleanup(shutil.rmtree, self.sdir, ignore_errors=True)
        self.clock = FakeTime()
        self._patches = []
        self._old_secrets = set(tw._SECRETS)
        tw._SECRETS = set()
        FakeTaskService.reset()
        self._old_watch_dir = os.environ.pop("WATCH_DIR", None)
        # 时间可控（越预算断言）；真实 gh 子进程与真实 HTTP 默认禁止
        self.patch(tw, "time", self.clock)
        self.patch(tw, "TaskService", FakeTaskService)
        self.patch(tw, "subprocess", SimpleNamespace(
            run=self._no_real_subprocess,
            TimeoutExpired=subprocess.TimeoutExpired))
        self.patch(ww, "_OPENER", FakeHttp())
        self.patch(ww, "RETRY_SLEEP", 0)
        # 受限凭据文件（0600，内容即令牌）
        self.token_file = os.path.join(self.sdir, "mkt.token")
        with open(self.token_file, "w", encoding="utf-8") as f:
            f.write(TOKEN_VALUE + "\n")
        os.chmod(self.token_file, 0o600)
        self.token_path = os.path.realpath(self.token_file)
        # gh 可执行占位：指向已存在文件（sys.executable）；子进程默认被禁止
        self.gh_path = os.path.realpath(sys.executable)
        self.gh_calls = []
        self.binding = {
            "repo": "octo/hello", "repository_id": "1001",
            "task_label": "task-t1", "subscription_key": "run-1",
            "watch_url": "https://fake.invalid",
            "token_file": self.token_path, "gh_path": self.gh_path,
            "start": "now"}
        self.default_service_binding = {
            "platform": "fake", "name": "orca-task-watch-fake",
            "state_dir": self.sdir}

    def tearDown(self):
        for patcher in reversed(self._patches):
            patcher.stop()
        tw._SECRETS = self._old_secrets
        if self._old_watch_dir is None:
            os.environ.pop("WATCH_DIR", None)
        else:
            os.environ["WATCH_DIR"] = self._old_watch_dir

    def _no_real_subprocess(self, argv, *a, **k):
        raise AssertionError(
            "测试禁止真实子进程 %r：请安装 case 级 gh 假件" % (argv,))

    # -- 工具

    def patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value, create=True)
        self._patches.append(patcher)
        patcher.start()
        return patcher

    def use_real_time(self):
        """线程 case：换回真实 time 模块（时长有界）。"""
        self.patch(tw, "time", real_time)

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = tw.main(argv)
            except SystemExit as exc:  # argparse
                rc = exc.code if isinstance(exc.code, int) else 1
        return rc, out.getvalue(), err.getvalue()

    def control(self):
        return tw._load_control(self.sdir)

    def webhook_state(self):
        return ww._load_state(self.sdir)

    def runtime(self):
        return tw._load_runtime(self.sdir)

    def write_control(self, **fields):
        control = tw._load_control(self.sdir)
        control.update(fields)
        tw._save_control(self.sdir, control)
        return control

    def write_webhook_state(self, **fields):
        state = ww._load_state(self.sdir)
        state.update(fields)
        ww._save_state(self.sdir, state)

    def heartbeat(self, generation=5, quiesced=False, fresh=0.0,
                  desired_seen="running"):
        tw._save_runtime(self.sdir, {
            "pid": 4242, "generation": generation,
            "heartbeat_ts": self.clock.t - fresh,
            "quiesced": quiesced, "desired_seen": desired_seen})

    def install_gh(self, repo_id=1001, entries=None, scope_fail=False,
                   missing=None):
        """gh 假件：按 endpoint 分发（范围/单票/仓库），记录 endpoint；
        返回值与真实 _run_gh 一致（已解析 JSON/分页列表），失败抛 TaskWatchError。"""
        items = [{"number": n, "state": s} for n, s in (entries or [])]
        missing = dict(missing or {})
        repo = self.binding["repo"]

        def fake_gh(binding, endpoint, paginate=False):
            self.gh_calls.append(endpoint)
            if endpoint.startswith("repos/%s/issues?" % repo):
                if scope_fail:
                    raise tw.TaskWatchError("GitHub 查询失败: gh: api 500 boom")
                mid = max(1, (len(items) + 1) // 2)
                return [items[:mid], items[mid:]]
            m = re.fullmatch(r"repos/%s/issues/(\d+)" % re.escape(repo),
                             endpoint)
            if m:
                number, state = int(m.group(1)), missing.get(int(m.group(1)),
                                                             "open")
                if state is None:  # 单票查询失败（404/网络错误）
                    raise tw.TaskWatchError("GitHub 查询失败: gh: Not Found")
                return {"number": number, "state": state}
            if endpoint == "repos/" + repo:
                return {"id": repo_id}
            raise tw.TaskWatchError("GitHub 查询失败: gh: 404")

        self.patch(tw, "_run_gh", fake_gh)
        return fake_gh

    def install_http(self, fn):
        fake = FakeHttp(fn)
        self.patch(ww, "_OPENER", fake)
        return fake

    def start_argv(self, sdir=None, **over):
        argv = ["start", "--repo", self.binding["repo"],
                "--repository-id", self.binding["repository_id"],
                "--task-label", self.binding["task_label"],
                "--subscription-key", self.binding["subscription_key"],
                "--token-file", self.token_file,
                "--watch-url", self.binding["watch_url"],
                "--gh-path", self.gh_path,
                "--state-dir", sdir or self.sdir]
        for key, value in over.items():
            argv += ["--" + key.replace("_", "-"), value]
        return argv

    def seeded_control(self, **over):
        """已绑定、desired=running、ready 且已对账的控制状态。"""
        control = {"binding": dict(self.binding),
                   "service_binding": dict(self.default_service_binding),
                   "desired": "running", "phase": "ready", "generation": 5,
                   "scope_check": {"version": 2, "ts": self.clock.t,
                                   "issues": [{"number": 7, "state": "open"}]},
                   "reconcile": {"version": 2, "ts": self.clock.t},
                   "known_issues": [7], "scope_ops": {}}
        control.update(over)
        tw._save_control(self.sdir, control)
        return control

    def stop_seeded(self, known=(7,), states=(("7", "closed"),)):
        """stop 专用种子：范围已在最新一轮查询中（closed），避免版本假升级。"""
        control = self.seeded_control(
            scope_check={"version": 2, "ts": self.clock.t,
                         "issues": [{"number": int(n), "state": s}
                                    for n, s in states]},
            known_issues=list(known))
        self.init_consumer()
        return control

    def init_consumer(self, cid="c-1"):
        self.write_webhook_state(
            subscription_key="run-1", repository_id="1001", start="now",
            watch_url="https://fake.invalid", consumer_id=cid,
            initial_boundary_seq=5, cursor_seq=5, pending=None)


# ---------------------------------------------------------------- start

class StartTests(GWCase):

    def test_start_missing_args_lists_required(self):
        rc, _, err = self.run_cli(["start", "--state-dir", self.sdir])
        self.assertNotEqual(rc, 0)
        self.assertIn("首次 start 需要", err)
        self.assertNotIn("Traceback", err)
        self.assertIsNone(self.control().get("binding"))

    def test_start_requires_real_ticket(self):
        self.install_gh(entries=[])
        self.install_http(lambda rec: (200, consumer_payload()))
        rc, out, err = self.run_cli(self.start_argv())
        self.assertEqual(rc, 1)
        self.assertIn("没有真实 Issue", err)
        self.assertNotIn("Traceback", err)
        # 未伪造范围：系统托管不得已注册/启动，绑定未写入
        self.assertEqual(FakeTaskService.state["ensure_calls"], 0)
        self.assertIsNone(self.control().get("binding"))
        self.assertIsNone(self.control().get("desired"))

    def test_start_repo_id_mismatch_refused(self):
        self.install_gh(repo_id=2002, entries=[(7, "open")])
        self.install_http(lambda rec: (200, consumer_payload()))
        rc, _, err = self.run_cli(self.start_argv())
        self.assertNotEqual(rc, 0)
        self.assertIn("不匹配", err)
        self.assertIsNone(self.control().get("binding"))
        self.assertEqual(FakeTaskService.state["ensure_calls"], 0)

    def test_start_rebind_refused_but_same_binding_ok(self):
        self.seeded_control()
        # 换仓库 / 换标签均拒绝
        rc, _, err = self.run_cli(self.start_argv(repo="octo/other"))
        self.assertNotEqual(rc, 0)
        self.assertIn("已绑定其他参数", err)
        rc, _, err = self.run_cli(self.start_argv(task_label="task-other"))
        self.assertNotEqual(rc, 0)
        self.assertIn("已绑定其他参数", err)
        self.assertEqual(FakeTaskService.state["ensure_calls"], 0)
        # 同一绑定可重新执行（后续用例覆盖幂等路径）

    def test_start_webhook_binding_mismatch_refused(self):
        self.seeded_control()
        self.install_gh(entries=[(7, "open")])
        self.install_http(lambda rec: (200, consumer_payload()))
        self.init_consumer(cid="c-old")
        self.write_webhook_state(repository_id="2002")
        rc, _, err = self.run_cli(["start", "--state-dir", self.sdir])
        self.assertNotEqual(rc, 0)
        self.assertIn("订阅绑定不匹配", err)
        self.assertIn("repository_id", err)
        self.assertEqual(FakeTaskService.state["ensure_calls"], 0)

    def test_start_gh_path_must_exist(self):
        rc, _, err = self.run_cli(
            self.start_argv(gh_path=os.path.join(self.sdir, "nowhere")))
        self.assertNotEqual(rc, 0)
        self.assertIn("可执行文件不存在", err)
        self.assertEqual(FakeTaskService.state["ensure_calls"], 0)

    def test_start_success_full_flow(self):
        self.install_gh(entries=[(7, "open"), (9, "open")])
        fake = self.install_http(lambda rec: (200, consumer_payload()))
        rc, out, err = self.run_cli(self.start_argv())
        self.assertEqual(rc, 0, err)
        self.assertNotIn(TOKEN_VALUE, out + err)
        data = json.loads(out.strip().splitlines()[-1])  # 末行为状态 JSON
        self.assertEqual(data["desired"], "running")
        self.assertTrue(data["service"]["running"])
        self.assertTrue(data["reconcile_required"])  # 启动后须协调者对账
        control = self.control()
        self.assertEqual(control["phase"], "ready")
        self.assertEqual(control["generation"], 1)
        self.assertEqual(control["scope_check"]["version"], 1)
        self.assertEqual(control["known_issues"], [7, 9])
        # 绑定只存凭据路径；令牌内容不落盘
        self.assertEqual(control["binding"]["token_file"], self.token_path)
        self.assertNotIn(TOKEN_VALUE, json.dumps(control))
        # init POST 参数正确
        posts = [r for r in fake.requests if r.method == "POST"]
        self.assertEqual([r.path for r in posts], ["/v1/consumers"])
        self.assertEqual(json.loads(posts[0].body),
                         {"subscription_key": "run-1",
                          "repository_id": "1001", "start": "now"})
        runtime = self.runtime()
        self.assertEqual(runtime["generation"], 1)
        self.assertEqual(runtime["desired_seen"], "running")

    def test_start_resume_reuses_service_binding_no_prepare(self):
        # 半完成恢复：service_binding 已写入 → 不得重新 prepare（不切换域）
        tw._save_control(self.sdir, {
            "binding": dict(self.binding),
            "service_binding": dict(self.default_service_binding)})
        self.install_gh(entries=[(7, "open")])
        self.install_http(lambda rec: (200, consumer_payload()))
        rc, _, err = self.run_cli(["start", "--state-dir", self.sdir])
        self.assertEqual(rc, 0, err)
        self.assertEqual(FakeTaskService.state["prepare_calls"], 0)
        self.assertEqual(FakeTaskService.state["last_ensure_binding"],
                         self.default_service_binding)
        self.assertEqual(self.control()["phase"], "ready")

    def test_start_idempotent_no_reset(self):
        self.seeded_control()
        self.heartbeat()
        FakeTaskService.state["registered"] = True
        FakeTaskService.state["running"] = True
        rc, out, _ = self.run_cli(["start", "--state-dir", self.sdir])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["state"], "running")
        control = self.control()
        self.assertEqual(control["generation"], 5)   # 不重置代次
        self.assertEqual(control["scope_check"]["version"], 2)
        self.assertEqual(control["reconcile"]["version"], 2)
        self.assertEqual(FakeTaskService.state["ensure_calls"], 0)
        self.assertEqual(FakeTaskService.state["prepare_calls"], 0)

    def test_start_ready_requires_fresh_generation_heartbeat(self):
        self.install_gh(entries=[(7, "open")])
        self.install_http(lambda rec: (200, consumer_payload()))
        self.patch(tw, "READY_BUDGET_SECONDS", 2)
        self.patch(tw, "TaskService", NotRunningTaskService)
        rc, _, err = self.run_cli(self.start_argv())
        self.assertNotEqual(rc, 0)
        self.assertIn("未收到当前代次", err)
        self.assertEqual(self.control()["phase"], "starting")
        self.assertEqual(self.control()["desired"], "running")
        self.assertEqual(self.control()["generation"], 1)
        # 修复后重试：ensure 写入匹配心跳 → 真实 _wait_ready 通过 → ready
        self.patch(tw, "TaskService", FakeTaskService)
        rc, _, err = self.run_cli(["start", "--state-dir", self.sdir])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.control()["phase"], "ready")
        self.assertEqual(self.runtime()["generation"], 2)
        self.assertFalse(self.runtime().get("quiesced"))

    def test_start_empty_second_query_refused(self):
        # init 前有票，订阅边界固定后标签范围为空 → 拒绝启动
        # （分页格式：第二轮返回两页全空 → entries=[]，而非分页不完整）
        responses = [[[{"number": 7, "state": "open"}]], [[], []]]
        calls = []

        def fake(binding, endpoint, paginate=False):
            calls.append(endpoint)
            if endpoint.startswith("repos/octo/hello/issues?"):
                return responses.pop(0) if responses else []
            return {"id": 1001}

        self.patch(tw, "_run_gh", fake)
        self.install_http(lambda rec: (200, consumer_payload()))
        rc, _, err = self.run_cli(self.start_argv())
        self.assertNotEqual(rc, 0)
        self.assertIn("范围为空", err)
        self.assertEqual(FakeTaskService.state["ensure_calls"], 0)

    def test_start_token_file_rules(self):
        # 存在但权限过宽（0644）→ 拒绝且不落任何状态
        loose = os.path.join(self.sdir, "loose.token")
        with open(loose, "w", encoding="utf-8") as f:
            f.write("other-token\n")
        os.chmod(loose, 0o644)
        rc, _, err = self.run_cli(self.start_argv(token_file=loose))
        self.assertNotEqual(rc, 0)
        self.assertIn("凭据文件必须属于当前用户", err)
        self.assertIsNone(self.control().get("binding"))
        self.assertEqual(FakeTaskService.state["ensure_calls"], 0)
        # 凭据文件不存在
        rc, _, err = self.run_cli(
            self.start_argv(token_file=os.path.join(self.sdir, "missing")))
        self.assertNotEqual(rc, 0)
        self.assertIn("No such file", err)
        self.assertIsNone(self.control().get("binding"))


# ---------------------------------------------------------------- gh 查询

class GhQueryTests(GWCase):

    def test_query_scope_excludes_pr_and_merges_pages(self):
        pages = [
            [{"number": 7, "state": "open"},
             {"number": 8, "state": "open", "pull_request": {"url": "x"}}],
            [{"number": 9, "state": "closed"}, {"number": 11, "state": "open"}]]

        def fake(binding, endpoint, paginate=False):
            self.gh_calls.append(endpoint)
            self.assertTrue(endpoint.startswith("repos/octo/hello/issues?"))
            # URL 校验移至真实 _run_gh 的 argv 断言；这里返回已解析分页
            return pages

        self.patch(tw, "_run_gh", fake)
        entries = tw.query_scope(self.binding)
        self.assertEqual([e["number"] for e in entries], [7, 9, 11])
        self.assertEqual([e["state"] for e in entries],
                         ["open", "closed", "open"])

    def test_query_scope_rejects_unknown_number_state_flips(self):
        def bad_number(binding, endpoint, paginate=False):
            return [[{"number": 0, "state": "open"}]]

        self.patch(tw, "_run_gh", bad_number)
        with self.assertRaises(tw.TaskWatchError):
            tw.query_scope(self.binding)

        def state_flip(binding, endpoint, paginate=False):
            return [[{"number": 7, "state": "open"}],
                    [{"number": 7, "state": "closed"}]]

        self.patch(tw, "_run_gh", state_flip)
        with self.assertRaises(tw.TaskWatchError) as cm:
            tw.query_scope(self.binding)
        self.assertIn("状态变化", str(cm.exception))

    def test_run_gh_argv_list_no_shell_bounded(self):
        calls = []

        responses = ["[[]]", '{"id": 1001}']

        def fake_run(argv, *a, **k):
            calls.append((argv, k))
            return SimpleNamespace(returncode=0, stdout=responses.pop(0),
                                   stderr="")

        self.patch(tw, "subprocess", SimpleNamespace(
            run=fake_run, TimeoutExpired=subprocess.TimeoutExpired))
        pages = tw.query_scope(self.binding)   # 真实 _run_gh（paginate=True）
        self.assertEqual(pages, [])            # --slurp 空页列表如实解析
        argv, kwargs = calls[0]
        self.assertIsInstance(argv, list)      # argv 数组，无 shell 插值
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs.get("timeout"), tw.GH_TIMEOUT)  # 限时
        self.assertEqual(argv[0], self.gh_path)
        self.assertEqual(argv[1:6],
                         ["api", "--method", "GET", "--paginate", "--slurp"])
        self.assertIn("labels=task-t1&per_page=100", argv[-1])
        self.assertIn("state=all", argv[-1])
        body = tw.query_repo_id(dict(self.binding))
        self.assertEqual(body, "1001")
        argv2, _ = calls[1]
        self.assertEqual(argv2, [self.gh_path, "api", "--method", "GET",
                                 "repos/octo/hello"])

    def test_run_gh_failure_sanitizes_secret_and_bad_json(self):
        tw._read_token(self.token_file)  # 注册 secret（_safe 覆盖）

        def fake_run(argv, *a, **k):
            raise subprocess.TimeoutExpired(argv, 30)

        self.patch(tw, "subprocess", SimpleNamespace(
            run=fake_run, TimeoutExpired=subprocess.TimeoutExpired))
        with self.assertRaises(tw.TaskWatchError) as cm:
            tw.query_scope(self.binding)
        self.assertIn("超时", str(cm.exception))

        def failing(argv, *a, **k):
            return SimpleNamespace(returncode=1, stdout="",
                                   stderr="auth failed: " + TOKEN_VALUE)

        self.patch(tw, "subprocess", SimpleNamespace(
            run=failing, TimeoutExpired=subprocess.TimeoutExpired))
        with self.assertRaises(tw.TaskWatchError) as cm:
            tw._run_gh(self.binding, "repos/octo/hello")
        self.assertIn("GitHub 查询失败", str(cm.exception))
        self.assertIn("[redacted]", str(cm.exception))
        self.assertNotIn(TOKEN_VALUE, str(cm.exception))
        self.assertNotIn(TOKEN_VALUE, cm.exception.args[0])

        def bad_json(argv, *a, **k):
            return SimpleNamespace(returncode=0, stdout="not json", stderr="")

        self.patch(tw, "subprocess", SimpleNamespace(
            run=bad_json, TimeoutExpired=subprocess.TimeoutExpired))
        with self.assertRaises(tw.TaskWatchError) as cm:
            tw._run_gh(self.binding, "repos/octo/hello")
        self.assertIn("无效 JSON", str(cm.exception))


class KnownIssuesTests(GWCase):

    def test_known_issues_retained_after_label_moves_two_rounds(self):
        control = {"known_issues": []}
        tw._record_scope_locked(self.sdir, control,
                                [{"number": 7, "state": "open"}])
        self.assertEqual(control["known_issues"], [7])
        scope2 = tw._record_scope_locked(
            self.sdir, control, [{"number": 9, "state": "open"}])
        self.assertEqual(scope2["version"], 2)       # 范围变化 → 版本升级
        self.assertEqual(sorted(control["known_issues"]), [7, 9])
        # 连续第二轮 7 依旧不在标签内：已纳管票不能被遗忘
        scope3 = tw._record_scope_locked(
            self.sdir, control, [{"number": 9, "state": "open"}])
        self.assertEqual(scope3["version"], 2)
        self.assertEqual(sorted(control["known_issues"]), [7, 9])


# ---------------------------------------------------------------- 通知桥

class BridgeTests(GWCase):

    def test_bridge_stopped_paused_and_quiescing_kinds(self):
        self.seeded_control(desired="stopped", phase="finalized")
        rc, out, _ = self.run_cli(["wait", "--state-dir", self.sdir])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["kind"], "stopped")
        self.seeded_control(desired="paused", pause_reason="用户授权 x")
        rc, out, _ = self.run_cli(["wait", "--state-dir", self.sdir])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["kind"], "paused")
        # quiescing：事务未结束必须显式报告，不许机械重挂桥
        self.seeded_control(desired="quiescing")
        rc, out, _ = self.run_cli(["wait", "--state-dir", self.sdir])
        self.assertEqual(rc, 0)
        notice = json.loads(out)
        self.assertEqual(notice["kind"], "quiescing")
        self.assertIn("核对事务结果", notice["message"])

    def test_bridge_idle_loop_sleep_then_pending_once_exit(self):
        self.seeded_control()
        self.init_consumer()
        self.heartbeat()
        fake = self.install_http(lambda rec: (404, ERR_BODY))
        sleeps = []

        def on_sleep(seconds):
            sleeps.append(seconds)
            self.write_webhook_state(
                pending={"batch_id": "b1",
                         "events": [sample_event(number=7)]})

        self.clock.on_sleep = on_sleep
        rc, out, _ = self.run_cli(["wait", "--state-dir", self.sdir])
        self.assertEqual(rc, 0)
        notice = json.loads(out)
        self.assertEqual(notice["kind"], "batch")
        self.assertEqual(notice["pending"]["batch_id"], "b1")
        self.assertEqual(len(sleeps), 1)
        self.assertEqual(fake.requests, [])          # 桥不访问远端

    def test_bridge_pending_delivered_even_with_large_after(self):
        self.seeded_control()
        self.init_consumer()
        self.heartbeat()
        self.write_webhook_state(
            pending={"batch_id": "b1", "events": [sample_event(number=7)]})
        rc, out, _ = self.run_cli(["wait", "--after", "999999999",
                                   "--state-dir", self.sdir])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["kind"], "batch")  # --after 不屏蔽批次
        self.assertEqual(json.loads(out)["pending"]["batch_id"], "b1")

    def test_bridge_error_filtered_by_after_but_new_batch_visible(self):
        self.seeded_control()
        self.heartbeat()
        runtime = self.runtime()
        runtime["last_error"] = {"version": 9, "message": "旧故障"}
        tw._save_runtime(self.sdir, runtime)
        # after=9：旧故障不再重报
        self.assertIsNone(tw.bridge_once(self.sdir, 9))
        # 新批次即便 --after 大仍交付
        self.write_webhook_state(pending={"batch_id": "b2", "events": []})
        notice = tw.bridge_once(self.sdir, 9)
        self.assertEqual(notice["kind"], "batch")
        self.assertEqual(notice["pending"]["batch_id"], "b2")

    def test_bridge_reconcile_notice(self):
        self.seeded_control()
        self.heartbeat()
        control = self.control()
        control.pop("reconcile", None)
        tw._save_control(self.sdir, control)
        notice = tw.bridge_once(self.sdir, 0)
        self.assertEqual(notice["kind"], "reconcile")
        self.assertEqual(notice["scope_version"], 2)
        self.assertEqual(notice["issues"], [{"number": 7, "state": "open"}])

    def test_bridge_recovering_notice_bounded_version(self):
        self.seeded_control()
        self.heartbeat(generation=4, fresh=9999)  # 心跳代次失联
        notice = tw.bridge_once(self.sdir, 0)
        self.assertEqual(notice["kind"], "recovering")
        self.assertEqual(notice["version"],
                         self.control()["generation"] * 1000000)
        self.assertEqual(notice["message"], "采集心跳失联，请 status 并 start 恢复")
        # after 屏蔽后不再重复
        self.assertIsNone(tw.bridge_once(self.sdir, notice["version"]))


# ---------------------------------------------------------------- status

class StatusTests(GWCase):

    def test_status_unbound_and_missing_dir(self):
        rc, out, _ = self.run_cli(["status", "--state-dir", self.sdir])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["state"], "unbound")
        self.assertFalse(data["service"]["registered"])  # 未注册如实返回
        self.assertEqual(data["desired"], "stopped")
        self.assertFalse(data["reconcile_required"])
        rc, _, err = self.run_cli(["status", "--state-dir",
                                   os.path.join(self.sdir, "nope")])
        self.assertNotEqual(rc, 0)
        self.assertIn("状态目录不存在", err)

    def test_status_running_shape(self):
        self.seeded_control()
        self.heartbeat()
        FakeTaskService.state["registered"] = True
        FakeTaskService.state["running"] = True
        rc, out, _ = self.run_cli(["status", "--state-dir", self.sdir])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["state"], "running")
        self.assertEqual(data["desired"], "running")
        self.assertEqual(data["generation"], 5)
        self.assertEqual(data["scope_version"], 2)
        self.assertFalse(data["reconcile_required"])
        self.assertTrue(data["service"]["registered"])
        self.assertTrue(data["service"]["running"])
        self.assertEqual(data["known_issues"], [7])
        self.assertIsNone(data["pending"])
        self.assertNotIn(TOKEN_VALUE, out)

    def test_status_processing_and_recovering(self):
        self.seeded_control()
        self.heartbeat()
        FakeTaskService.state["registered"] = True
        FakeTaskService.state["running"] = True
        self.write_webhook_state(pending={"batch_id": "b1", "events": []})
        _, out, _ = self.run_cli(["status", "--state-dir", self.sdir])
        data = json.loads(out)
        self.assertEqual(data["state"], "processing")
        self.assertEqual(data["pending"]["batch_id"], "b1")
        # 平台声称 running，但运行快照过期/代次不匹配 → 如实报 recovering
        FakeTaskService.state["running"] = True
        self.heartbeat(generation=4, fresh=9999)
        _, out, _ = self.run_cli(["status", "--state-dir", self.sdir])
        self.assertEqual(json.loads(out)["state"], "recovering")

    def test_status_service_error_blocked(self):
        self.seeded_control()
        FakeTaskService.state["status_error"] = "平台查询失败"
        _, out, _ = self.run_cli(["status", "--state-dir", self.sdir])
        data = json.loads(out)
        self.assertEqual(data["state"], "blocked")
        self.assertIn("平台查询失败", data["service"]["detail"])
        self.assertFalse(data["service"]["registered"])

    def test_status_cleanup_pending_and_stopped(self):
        self.seeded_control(desired="stopped", phase="finalized")
        FakeTaskService.state["registered"] = True   # 托管尚未注销
        _, out, _ = self.run_cli(["status", "--state-dir", self.sdir])
        self.assertEqual(json.loads(out)["state"], "cleanup_pending")
        FakeTaskService.state["registered"] = False
        _, out, _ = self.run_cli(["status", "--state-dir", self.sdir])
        self.assertEqual(json.loads(out)["state"], "stopped")

    def test_status_desired_states_reported(self):
        self.seeded_control(desired="quiescing")
        _, out, _ = self.run_cli(["status", "--state-dir", self.sdir])
        self.assertEqual(json.loads(out)["state"], "quiescing")
        self.seeded_control(desired="paused", pause_reason="用户主动暂停")
        _, out, _ = self.run_cli(["status", "--state-dir", self.sdir])
        data = json.loads(out)
        self.assertEqual(data["state"], "paused")
        self.assertEqual(data["detail"], "用户主动暂停")


class ReconcileTests(GWCase):

    def test_reconcile_version_gate(self):
        self.seeded_control()
        rc, _, err = self.run_cli(["reconcile", "--version", "1",
                                   "--state-dir", self.sdir])
        self.assertNotEqual(rc, 0)
        self.assertIn("版本已变化", err)
        self.assertEqual(self.control()["reconcile"]["version"], 2)
        rc, out, _ = self.run_cli(["reconcile", "--version", "2",
                                   "--state-dir", self.sdir])
        self.assertEqual(rc, 0)
        self.assertEqual(self.control()["reconcile"]["version"], 2)


# ---------------------------------------------------------------- scope

class ScopeTests(GWCase):

    def test_scope_begin_requires_running(self):
        self.seeded_control(desired="stopped")
        rc, _, err = self.run_cli(["scope", "begin", "--op-id", "add-1",
                                   "--state-dir", self.sdir])
        self.assertNotEqual(rc, 0)
        self.assertIn("先 start 恢复", err)
        self.assertEqual(self.control().get("scope_ops"), {})  # 不落操作记录

    def test_scope_end_unknown_op_refused(self):
        self.seeded_control()
        rc, _, err = self.run_cli(["scope", "end", "--op-id", "ghost",
                                   "--state-dir", self.sdir])
        self.assertNotEqual(rc, 0)
        self.assertIn("未找到该 scope 操作", err)

    def test_scope_roundtrip_success_bumps_version(self):
        self.seeded_control()
        self.install_gh(entries=[(7, "open"), (12, "open")])
        rc, _, _ = self.run_cli(["scope", "begin", "--op-id", "add-1",
                                 "--state-dir", self.sdir])
        self.assertEqual(rc, 0)
        control = self.control()
        self.assertIn("add-1", control["scope_ops"])
        # end 刷新：新票 #12 进入并集，版本升级，reconcile 失效，op 清除
        rc, out, _ = self.run_cli(["scope", "end", "--op-id", "add-1",
                                   "--state-dir", self.sdir])
        self.assertEqual(rc, 0, out)
        control = self.control()
        self.assertEqual(control["scope_check"]["version"], 3)
        self.assertEqual(sorted(control["known_issues"]), [7, 12])
        self.assertNotEqual(control["reconcile"]["version"],
                            control["scope_check"]["version"])
        self.assertEqual(control["scope_ops"], {})
        # 未对账前 stop 拒绝
        rc, _, err = self.run_cli(["stop", "--state-dir", self.sdir])
        self.assertNotEqual(rc, 0)
        self.assertIn("对账", err)
        self.assertEqual(self.control()["desired"], "running")

    def test_scope_end_refresh_failure_keeps_op_open(self):
        self.seeded_control()
        self.run_cli(["scope", "begin", "--op-id", "add-2",
                      "--state-dir", self.sdir])
        self.install_gh(scope_fail=True)
        rc, _, err = self.run_cli(["scope", "end", "--op-id", "add-2",
                                   "--state-dir", self.sdir])
        self.assertNotEqual(rc, 0)
        self.assertIn("GitHub 查询失败", err)
        self.assertIn("add-2", self.control()["scope_ops"])
        # 修复 gh 后重试 end 成功
        self.install_gh(entries=[(7, "open")])
        rc, _, _ = self.run_cli(["scope", "end", "--op-id", "add-2",
                                 "--state-dir", self.sdir])
        self.assertEqual(rc, 0)
        self.assertEqual(self.control()["scope_ops"], {})


# ---------------------------------------------------------------- stop

class StopTests(GWCase):

    def test_stop_success_full_flow(self):
        self.stop_seeded()
        self.install_gh(entries=[(7, "closed")])
        fake = self.install_http(lambda rec: (200, {}))
        FakeTaskService.state["registered"] = True
        rc, out, _ = self.run_cli(["stop", "--state-dir", self.sdir])
        self.assertEqual(rc, 0, out)
        control = self.control()
        self.assertEqual(control["desired"], "stopped")
        self.assertEqual(control["phase"], "finalized")
        self.assertEqual(FakeTaskService.state["remove_calls"], 1)
        self.assertFalse(FakeTaskService.state["registered"])  # 已注销
        # 清空前一次真实长轮询（wait_seconds=0），且未落 pending
        self.assertEqual([r.path for r in fake.requests],
                         ["/v1/consumers/c-1/events?wait_seconds=0"])
        self.assertIsNone(self.webhook_state()["pending"])
        # 停机前有最终范围复查（完整标签查询两次）
        self.assertGreaterEqual(
            len([c for c in self.gh_calls if "labels=" in c]), 2)

    def test_stop_idempotent_after_stopped_finish_only(self):
        self.seeded_control(desired="stopped", phase="finalized",
                            reconcile={"version": 2, "ts": self.clock.t})
        self.install_gh(entries=[(7, "closed")])
        FakeTaskService.state["registered"] = True
        rc, out, _ = self.run_cli(["stop", "--state-dir", self.sdir])
        self.assertEqual(rc, 0, out)
        self.assertEqual(FakeTaskService.state["remove_calls"], 1)
        self.assertEqual([c for c in self.gh_calls if "labels=" in c], [])
        self.assertEqual(self.control()["desired"], "stopped")

    def test_stop_blocked_pending_restores_running(self):
        self.stop_seeded()
        self.install_gh(entries=[(7, "closed")])
        fake = self.install_http(lambda rec: (200, {}))
        self.write_webhook_state(pending={"batch_id": "b9", "events": []})
        rc, _, err = self.run_cli(["stop", "--state-dir", self.sdir])
        self.assertNotEqual(rc, 0)
        self.assertIn("仍有未确认批次", err)
        self.assertEqual(self.control()["desired"], "running")  # 未放弃意图
        self.assertEqual(FakeTaskService.state["remove_calls"], 0)
        self.assertEqual(fake.requests, [])            # 不与远端接触

    def test_stop_remote_backlog_saves_pending_and_refuses(self):
        self.stop_seeded()
        self.install_gh(entries=[(7, "closed")])
        self.install_http(lambda rec: (200, batch_payload(
            "bE", [sample_event(number=7)])))
        rc, _, err = self.run_cli(["stop", "--state-dir", self.sdir])
        self.assertNotEqual(rc, 0)
        self.assertIn("积压", err)
        self.assertEqual(self.control()["desired"], "running")
        pending = self.webhook_state()["pending"]
        self.assertEqual(pending["batch_id"], "bE")   # 积压已保存不丢
        self.assertEqual(pending["events"][0]["subject"]["number"], 7)
        self.assertEqual(FakeTaskService.state["remove_calls"], 0)

    def test_stop_blocked_open_issue_restores_running(self):
        self.stop_seeded(states=(("7", "open"),))
        self.install_gh(entries=[(7, "open")])
        fake = self.install_http(lambda rec: (200, {}))
        rc, _, err = self.run_cli(["stop", "--state-dir", self.sdir])
        self.assertNotEqual(rc, 0)
        self.assertIn("仍有未关闭", err)
        self.assertEqual(self.control()["desired"], "running")
        self.assertEqual(FakeTaskService.state["remove_calls"], 0)
        self.assertEqual(fake.requests, [])

    def test_stop_missing_issue_verified_closed_passes(self):
        # 已知票 #12 已失去标签但已 CLOSED → 放行并注销
        self.stop_seeded(known=(7, 12))
        self.install_gh(entries=[(7, "closed")], missing={12: "closed"})
        self.install_http(lambda rec: (200, {}))
        FakeTaskService.state["registered"] = True
        rc, out, _ = self.run_cli(["stop", "--state-dir", self.sdir])
        self.assertEqual(rc, 0, out)
        self.assertTrue(any(c.endswith("/issues/12") for c in self.gh_calls))
        self.assertEqual(FakeTaskService.state["remove_calls"], 1)

    def test_stop_missing_issue_open_or_ghfail_blocks(self):
        cases = [("open", ("#12", "仍未关闭")),
                 (None, ("GitHub 查询失败",))]
        for missing, needles in cases:
            with self.subTest(missing=missing):
                self.stop_seeded(known=(7, 12))
                self.install_gh(entries=[(7, "closed")],
                                missing={12: missing})
                self.install_http(lambda rec: (200, {}))
                rc, _, err = self.run_cli(["stop", "--state-dir", self.sdir])
                self.assertNotEqual(rc, 0)
                for needle in needles:
                    self.assertIn(needle, err)
                self.assertEqual(self.control()["desired"], "running")
                self.assertEqual(FakeTaskService.state["remove_calls"], 0)

    def test_stop_blocked_by_pending_reconcile(self):
        self.stop_seeded()
        self.install_gh(entries=[(7, "closed")])
        self.write_control(reconcile={"version": 1, "ts": 0})
        rc, _, err = self.run_cli(["stop", "--state-dir", self.sdir])
        self.assertNotEqual(rc, 0)
        self.assertIn("对账", err)
        self.assertEqual(self.control()["desired"], "running")

    def test_stop_blocked_by_open_scope_op(self):
        self.stop_seeded(states=(("7", "open"),))
        self.install_gh(entries=[(7, "open")])
        rc, _, _ = self.run_cli(["scope", "begin", "--op-id", "wip",
                                 "--state-dir", self.sdir])
        self.assertEqual(rc, 0)
        rc, _, err = self.run_cli(["stop", "--state-dir", self.sdir])
        self.assertNotEqual(rc, 0)
        self.assertIn("scope", err)
        self.assertEqual(self.control()["desired"], "running")

    def test_stop_second_refresh_new_issue_bumps_version_and_refuses(self):
        self.stop_seeded()
        FakeTaskService.state["registered"] = True
        calls = []

        def fake(binding, endpoint, paginate=False):
            self.gh_calls.append(endpoint)
            if endpoint.startswith("repos/octo/hello/issues?"):
                n = len(calls)
                calls.append(endpoint)
                pages = [[{"number": 7, "state": "closed"}]]
                if n >= 1:  # 清空复查阶段才发现新票
                    pages = [[{"number": 7, "state": "closed"},
                              {"number": 13, "state": "open"}]]
                return pages
            return {"id": 1001}

        self.patch(tw, "_run_gh", fake)
        self.install_http(lambda rec: (200, {}))
        rc, _, err = self.run_cli(["stop", "--state-dir", self.sdir])
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.control()["desired"], "running")
        control = self.control()
        # 新票已入账并升级版本；reconcile 失效，须先对账
        self.assertEqual(control["scope_check"]["version"], 3)
        self.assertNotEqual(control["reconcile"]["version"],
                            control["scope_check"]["version"])
        self.assertIn(13, control["known_issues"])
        self.assertEqual(FakeTaskService.state["remove_calls"], 0)

    def test_stop_remove_failure_desired_stopped_then_retry(self):
        self.stop_seeded()
        self.install_gh(entries=[(7, "closed")])
        self.install_http(lambda rec: (200, {}))
        FakeTaskService.state["registered"] = True
        FakeTaskService.state["remove_error"] = "平台命令失败"
        rc, _, err = self.run_cli(["stop", "--state-dir", self.sdir])
        self.assertNotEqual(rc, 0)
        # desired=stopped 已保存：不再被恢复成 running，可重试收尾
        self.assertEqual(self.control()["desired"], "stopped")
        FakeTaskService.state["remove_error"] = None
        rc, out, _ = self.run_cli(["stop", "--state-dir", self.sdir])
        self.assertEqual(rc, 0, out)
        self.assertFalse(FakeTaskService.state["registered"])

    def test_stop_paused_refused_not_auto_resumed(self):
        self.seeded_control(desired="paused", pause_reason="用户授权暂停")
        self.install_gh(entries=[])
        rc, _, err = self.run_cli(["stop", "--state-dir", self.sdir])
        self.assertNotEqual(rc, 0)
        self.assertIn("已暂停", err)
        self.assertEqual(self.control()["desired"], "paused")  # 不自动恢复
        self.assertEqual(FakeTaskService.state["remove_calls"], 0)

    # -- 真实线程锁竞争（有界）

    def test_stop_waits_for_real_webhook_lock_thread(self):
        self.use_real_time()
        self.stop_seeded()
        self.install_gh(entries=[(7, "closed")])
        self.install_http(lambda rec: (200, {}))
        acquired = threading.Event()

        def holder():
            with ww._state_lock(self.sdir):
                acquired.set()
                real_time.sleep(0.2)               # 模拟在途短临界区

        t = threading.Thread(target=holder)
        t.start()
        self.assertTrue(acquired.wait(5))
        started = real_time.monotonic()
        rc, out, _ = self.run_cli(["stop", "--state-dir", self.sdir])
        t.join(5)
        self.assertEqual(rc, 0, out)               # 等到锁后正常收尾
        self.assertGreaterEqual(real_time.monotonic() - started, 0.15)
        self.assertEqual(self.control()["desired"], "stopped")
        self.assertEqual(FakeTaskService.state["remove_calls"], 1)

    def test_stop_sees_inflight_pending_and_refuses_thread(self):
        self.use_real_time()
        self.stop_seeded(states=(("7", "closed"),))
        self.install_gh(entries=[(7, "closed")])
        self.install_http(lambda rec: (200, {}))
        acquired = threading.Event()

        def inflight():
            with ww._state_lock(self.sdir):
                acquired.set()
                real_time.sleep(0.2)               # 采集在途：批次随后落盘
                state = ww._load_state(self.sdir)
                state["pending"] = {"batch_id": "bZ",
                                    "events": [sample_event(number=7)],
                                    "through_seq": 3}
                ww._save_state(self.sdir, state)

        t = threading.Thread(target=inflight)
        t.start()
        self.assertTrue(acquired.wait(5))
        rc, _, err = self.run_cli(["stop", "--state-dir", self.sdir])
        t.join(5)
        # 在途批先落盘；stop 拿到锁后发现 pending → 拒绝并保持 running
        self.assertNotEqual(rc, 0)
        self.assertIn("批次", err)
        self.assertEqual(self.control()["desired"], "running")
        self.assertEqual(self.webhook_state()["pending"]["batch_id"], "bZ")
        self.assertEqual(FakeTaskService.state["remove_calls"], 0)


# ---------------------------------------------------------------- serve

class ServeTests(GWCase):

    def _step(self):
        return tw.serve_step(self.sdir)

    def test_serve_stopped_no_network_exits(self):
        self.seeded_control(desired="stopped", phase="finalized")
        self.init_consumer()
        rc, _, _ = self.run_cli(["serve", "--state-dir", self.sdir])
        self.assertEqual(rc, 0)
        runtime = self.runtime()
        self.assertTrue(runtime["quiesced"])
        self.assertEqual(runtime["generation"], 5)
        self.assertEqual(runtime["desired_seen"], "stopped")
        self.assertIsNone(self.webhook_state()["pending"])

    def test_serve_backpressure_no_poll(self):
        self.seeded_control()
        self.heartbeat()
        self.init_consumer()
        self.write_webhook_state(pending={"batch_id": "b1",
                                          "events": [sample_event()]})
        fake = self.install_http(lambda rec: (200, batch_payload("b2")))
        self.assertEqual(self._step(), "backpressure")
        self.assertEqual(self._step(), "backpressure")
        self.assertEqual(fake.requests, [])          # 有 pending 不热取批
        self.assertEqual(self.webhook_state()["pending"]["batch_id"], "b1")
        runtime = self.runtime()
        self.assertEqual(runtime["generation"], 5)   # 心跳仍被维护
        self.assertTrue(runtime["heartbeat_ts"] <= self.clock.t)

    def test_serve_poll_inside_webhook_lock_and_persists(self):
        self.seeded_control()
        self.heartbeat()
        self.init_consumer()
        held = []

        def fn(rec):
            # 断言 poll 发生在 webhook 锁内：此时再取锁必须 LockBusy
            try:
                with ww._lock_file(self.sdir, ww.LOCK_FILE):
                    held.append("free")
            except ww.LockBusy:
                held.append("busy")
            return (200, {"batch_id": "b1",
                          "events": [sample_event(number=7)],
                          "through_seq": 9})

        fake = self.install_http(fn)
        self.assertEqual(self._step(), "consumed")
        self.assertEqual(held, ["busy"])             # 长轮询全程持锁
        pending = self.webhook_state()["pending"]
        self.assertEqual(pending["batch_id"], "b1")  # 先落盘 pending
        self.assertEqual(pending["through_seq"], 9)
        runtime = self.runtime()
        self.assertEqual(runtime["generation"], 5)
        self.assertEqual(runtime["last_poll_ts"], self.clock.t)
        self.assertNotIn("last_error", runtime)
        # 下一轮：pending 未确认 → 背压
        self.assertEqual(self._step(), "backpressure")
        self.assertEqual(len(fake.requests), 1)

    def test_serve_error_redacted_backoff_no_http(self):
        self.seeded_control()
        self.heartbeat()
        self.init_consumer()
        fake = self.install_http(
            lambda rec: (500, {"error": {"code": "t",
                                         "message": "服务异常 " + TOKEN_VALUE}}))
        # 一轮故障：三次重试（RETRY_SLEEP=0）全部失败后记录并退避
        self.assertEqual(self._step(), "error")
        runtime = self.runtime()
        error = runtime["last_error"]
        self.assertIn("[redacted]", error["message"])
        self.assertNotIn(TOKEN_VALUE, error["message"])   # 令牌必须打码
        self.assertIn("服务异常", error["message"])
        self.assertNotIn(TOKEN_VALUE, json.dumps(self.webhook_state()))
        self.assertNotIn(TOKEN_VALUE, json.dumps(self.runtime()))
        self.assertEqual(runtime["failures"], 1)
        self.assertIn(runtime["blocked_note"], error["message"])
        self.assertEqual(runtime["retry_at"], self.clock.t + 10)  # 退避
        requests_after_error = len(fake.requests)
        self.assertEqual(self._step(), "backoff")     # 退避期不访问远端
        self.assertEqual(len(fake.requests), requests_after_error)
        # 退避期满后重试仍失败 → failures 累加、退避指数升级、错误版本不重复 bump
        self.clock.t += 11
        self.assertEqual(self._step(), "error")
        runtime = self.runtime()
        self.assertEqual(runtime["failures"], 2)
        self.assertEqual(runtime["last_error"]["version"], 1)  # 同因不换版
        self.assertEqual(runtime["retry_at"], self.clock.t + 20)

    def test_serve_reconcile_gate_blocks(self):
        self.seeded_control()
        self.heartbeat()
        self.init_consumer()
        control = self.control()
        control.pop("reconcile", None)          # start 后待对账的合法形态
        tw._save_control(self.sdir, control)
        fake = self.install_http(lambda rec: (200, batch_payload("b1")))
        self.assertEqual(self._step(), "reconcile")
        self.assertEqual(fake.requests, [])          # 未对账不消费
        rc, _, _ = self.run_cli(["reconcile", "--version", "2",
                                 "--state-dir", self.sdir])
        self.assertEqual(rc, 0)
        self.assertEqual(self._step(), "consumed")   # 对账后可恢复消费
        self.assertEqual(self.webhook_state()["pending"]["batch_id"], "b1")

    def test_serve_loop_sleep_hook_ends_by_desired(self):
        self.seeded_control()
        self.init_consumer()
        fake = self.install_http(lambda rec: (200, {}))
        sleeps = []

        def on_sleep(seconds):
            sleeps.append(seconds)
            control = tw._load_control(self.sdir)
            control.update(desired="stopped")
            tw._save_control(self.sdir, control)

        self.clock.on_sleep = on_sleep
        rc, _, _ = self.run_cli(["serve", "--state-dir", self.sdir])
        self.assertEqual(rc, 0)
        self.assertEqual(len(sleeps), 1)             # 注入 sleep 改 desired 结束
        self.assertEqual(len(fake.requests), 1)      # 正常轮询一次
        runtime = self.runtime()
        self.assertTrue(runtime["quiesced"])
        self.assertEqual(runtime["desired_seen"], "stopped")
        self.assertEqual(self.control()["desired"], "stopped")

    def test_serve_periodic_scope_refresh_no_change(self):
        self.seeded_control(scope_check={
            "version": 2, "ts": self.clock.t - 400,
            "issues": [{"number": 7, "state": "open"}]})
        self.heartbeat()
        self.init_consumer()
        self.install_gh(entries=[(7, "open")])
        fake = self.install_http(lambda rec: (200, {}))
        self.assertEqual(self._step(), "idle")
        self.assertGreaterEqual(
            len([c for c in self.gh_calls if "labels=" in c]), 1)
        control = self.control()
        self.assertEqual(control["scope_check"]["version"], 2)  # 未变化不升级
        self.assertEqual(control["scope_check"]["ts"], self.clock.t)
        self.assertEqual(self.runtime()["last_poll_ts"], self.clock.t)
        self.assertEqual(len(fake.requests), 1)

    def test_serve_refresh_stale_query_not_overwriting_newer_scope(self):
        # 真race回归：serve 的周期查询在锁外执行；查询期间另一进程
        # （如 scope end）已把新范围写入 control。旧查询返回后不得覆盖：
        # guard 条件 latest.scope_check==原scope 不满足时跳过记录。
        self.seeded_control()                       # v2 issues=[7 open], reconcile=2
        self.heartbeat()
        self.init_consumer()

        def fake(binding, endpoint, paginate=False):
            self.gh_calls.append(endpoint)
            if endpoint.startswith("repos/octo/hello/issues?"):
                # 查询执行期间：scope end 提交新范围（升版本 3，纳入新票 #9）
                with tw._lock(self.sdir, tw.CONTROL_LOCK):
                    latest = tw._load_control(self.sdir)
                    tw._record_scope_locked(
                        self.sdir, latest,
                        [{"number": 7, "state": "open"},
                         {"number": 9, "state": "open"}])
                return [[{"number": 7, "state": "open"}]]  # 旧查询才返回
            return {"id": 1001}

        self.patch(tw, "_run_gh", fake)
        fake_http = self.install_http(lambda rec: (200, {}))
        control = self.control()                    # 触发周期刷新（ts 过期）
        control["scope_check"]["ts"] = self.clock.t - 400
        tw._save_control(self.sdir, control)

        result = tw.serve_step(self.sdir)
        # 新范围 v3 的 reconcile 仍指 v2 → 按新范围给对账提示，而不是消费
        self.assertEqual(result, "reconcile")
        scope = self.control()["scope_check"]
        self.assertEqual(scope["version"], 3)
        self.assertEqual([e["number"] for e in scope["issues"]], [7, 9])
        self.assertEqual(self.control()["known_issues"], [7, 9])
        self.assertEqual(self.control()["reconcile"]["version"], 2)
        # 代次与 desired 未被刷新触碰
        self.assertEqual(self.control()["generation"], 5)
        self.assertEqual(self.control()["desired"], "running")
        self.assertEqual(fake_http.requests, [])    # 未对账不 poll

    def test_serve_pause_quiesces_without_consuming(self):
        self.seeded_control(desired="paused", pause_reason="用户授权 x")
        self.init_consumer()
        fake = self.install_http(lambda rec: (200, batch_payload("b1")))
        # serve 读到 desired=paused 即静音并退出（平台不再拉起，宿主恢复后再注册）
        self.assertEqual(self._step(), "stopped")
        self.assertEqual(fake.requests, [])
        runtime = self.runtime()
        self.assertTrue(runtime["quiesced"])
        self.assertEqual(runtime["desired_seen"], "paused")


# ---------------------------------------------------------------- pause

class PauseTests(GWCase):

    def test_pause_saves_pending_and_removes_service(self):
        self.seeded_control()
        self.init_consumer()
        self.install_gh(entries=[(7, "open")])
        self.install_http(lambda rec: (200, {}))
        FakeTaskService.state["registered"] = True
        self.write_webhook_state(pending={"batch_id": "b7", "events": []})
        rc, out, _ = self.run_cli(["pause", "--reason", "用户授权暂停窗口",
                                   "--state-dir", self.sdir])
        self.assertEqual(rc, 0, out)
        control = self.control()
        self.assertEqual(control["desired"], "paused")
        self.assertEqual(control["phase"], "paused")
        self.assertEqual(control["pause_reason"], "用户授权暂停窗口")
        # 批次保留，等待恢复后处理（pause 不冒充完成、不丢数据）
        self.assertEqual(self.webhook_state()["pending"]["batch_id"], "b7")
        self.assertEqual(FakeTaskService.state["remove_calls"], 1)
        self.assertFalse(FakeTaskService.state["registered"])

    def test_pause_requires_reason(self):
        self.seeded_control()
        rc, _, err = self.run_cli(["pause", "--reason", "",
                                   "--state-dir", self.sdir])
        self.assertNotEqual(rc, 0)
        self.assertIn("必须记录", err)
        self.assertEqual(self.control()["desired"], "running")
        self.assertEqual(FakeTaskService.state["remove_calls"], 0)


# ---------------------------------------------------------------- 新路径：
# LockBusy 精确 catch 与 stop/pause 回滚

class _StubLock:
    """替身锁对象：依次抛出 failures 中的异常，之后正常进入。"""

    def __init__(self, failures):
        self.failures = list(failures or [])

    def __enter__(self):
        if self.failures:
            raise self.failures.pop(0)

    def __exit__(self, *a):
        return False


class LockAndRollbackTests(GWCase):

    def test_wait_webhook_lock_retries_only_lockbusy_within_budget(self):
        self.patch(ww, "_state_lock",
                   lambda _dir: _StubLock([ww.LockBusy("占用")] * 99))
        self.patch(tw, "STOP_BUDGET_SECONDS", 1)
        with self.assertRaises(tw.TaskWatchError) as cm:
            with tw._wait_webhook_lock(self.sdir):
                self.fail("锁从未成功，不应进入临界区")
        self.assertIn("无法取得采集事务锁", str(cm.exception))
        self.assertGreater(self.clock.sleep_calls, 3)   # 预算内确实在有界重试

    def test_wait_webhook_lock_brief_busy_then_acquires(self):
        stub = _StubLock([ww.LockBusy("占用"), ww.LockBusy("占用")])
        self.patch(ww, "_state_lock", lambda _dir: stub)
        entered = []
        with tw._wait_webhook_lock(self.sdir):
            entered.append("in")
        self.assertEqual(entered, ["in"])
        self.assertEqual(self.clock.sleep_calls, 2)     # 只对锁占用重试

    def test_wait_webhook_lock_misclassified_watcher_error_propagates(self):
        # 非 LockBusy 的 WatcherError 不得被当锁竞争吞掉重试：立即向上抛
        self.patch(ww, "_state_lock",
                   lambda _dir: _StubLock([ww.WatcherError("模拟其他状态错误")]))
        with self.assertRaises(ww.WatcherError):
            with tw._wait_webhook_lock(self.sdir):
                self.fail("不应进入临界区")
        self.assertEqual(self.clock.sleep_calls, 0)     # 未重试、未吞错

    def test_stop_lock_busy_budget_refusal_rolls_back_running(self):
        self.stop_seeded()
        self.install_gh(entries=[(7, "closed")])
        fake = self.install_http(lambda rec: (200, {}))
        self.patch(ww, "_state_lock",
                   lambda _dir: _StubLock([ww.LockBusy("占用")] * 99))
        self.patch(tw, "STOP_BUDGET_SECONDS", 1)
        FakeTaskService.state["registered"] = True
        rc, _, err = self.run_cli(["stop", "--state-dir", self.sdir])
        self.assertNotEqual(rc, 0)
        self.assertIn("无法取得采集事务锁", err)
        self.assertNotIn("Traceback", err)
        # BaseException 回滚：quiescing 期间状态已被触碰（scope 已记录），但意图恢复
        control = self.control()
        self.assertEqual(control["desired"], "running")
        self.assertNotIn("stopped_ts", control)
        self.assertEqual(FakeTaskService.state["remove_calls"], 0)
        self.assertEqual(fake.requests, [])             # 锁都进不去，别想 poll

    def test_pause_rollback_to_running_on_lock_timeout(self):
        self.seeded_control()
        self.init_consumer()
        self.install_gh(entries=[(7, "open")])
        self.patch(ww, "_state_lock",
                   lambda _dir: _StubLock([ww.LockBusy("占用")] * 99))
        self.patch(tw, "STOP_BUDGET_SECONDS", 1)
        FakeTaskService.state["registered"] = True
        rc, _, err = self.run_cli(["pause", "--reason", "用户授权暂停",
                                   "--state-dir", self.sdir])
        self.assertNotEqual(rc, 0)
        self.assertIn("无法取得采集事务锁", err)
        self.assertEqual(self.control()["desired"], "running")  # 回滚非 paused
        self.assertEqual(FakeTaskService.state["remove_calls"], 0)

    def test_pause_remove_failure_desired_paused_then_retry_finish(self):
        self.seeded_control()
        self.init_consumer()
        self.install_gh(entries=[(7, "open")])
        self.install_http(lambda rec: (200, {}))
        FakeTaskService.state["registered"] = True
        self.write_webhook_state(pending={"batch_id": "b7", "events": []})
        # 第一次 pause：paused 已落盘，但 remove 平台失败
        FakeTaskService.state["remove_error"] = "平台命令失败"
        rc, _, err = self.run_cli(["pause", "--reason", "用户授权暂停",
                                   "--state-dir", self.sdir])
        self.assertNotEqual(rc, 0)
        self.assertIn("平台命令失败", err)
        # paused 不回滚成 running；pending 保留；平台仍旧注册
        control = self.control()
        self.assertEqual(control["desired"], "paused")
        self.assertEqual(control["phase"], "paused")
        self.assertEqual(self.webhook_state()["pending"]["batch_id"], "b7")
        self.assertTrue(FakeTaskService.state["registered"])
        # 重试 pause：paused → 幂等收尾（再试 remove），不重新 quiescing
        FakeTaskService.state["remove_error"] = None
        rc, out, _ = self.run_cli(["pause", "--reason", "用户授权暂停",
                                   "--state-dir", self.sdir])
        self.assertEqual(rc, 0, out)
        self.assertEqual(FakeTaskService.state["remove_calls"], 2)
        self.assertFalse(FakeTaskService.state["registered"])
        control = self.control()
        self.assertEqual(control["desired"], "paused")
        self.assertEqual(control["phase"], "paused")
        self.assertEqual(self.webhook_state()["pending"]["batch_id"], "b7")

    def test_pause_after_stopped_finish_only(self):
        self.seeded_control(desired="stopped", phase="finalized",
                            reconcile={"version": 2, "ts": self.clock.t})
        self.install_gh(entries=[])
        FakeTaskService.state["registered"] = True
        rc, out, _ = self.run_cli(["pause", "--reason", "顺手收尾",
                                   "--state-dir", self.sdir])
        self.assertEqual(rc, 0, out)
        self.assertEqual(FakeTaskService.state["remove_calls"], 1)
        self.assertEqual([c for c in self.gh_calls if "labels=" in c], [])
        self.assertEqual(self.control()["desired"], "stopped")

    def test_serve_scope_refresh_lock_busy_returns_busy(self):
        # serve 周期刷新阶段只把 LockBusy 记为 busy；不吞其他错误、不访问远端
        self.seeded_control(scope_check={
            "version": 2, "ts": self.clock.t - 400,
            "issues": [{"number": 7, "state": "open"}]})
        self.heartbeat()
        self.install_gh(entries=[(7, "open")])
        fake = self.install_http(lambda rec: (200, {}))
        self.patch(ww, "_lock_file",
                   lambda *a, **k: (_ for _ in ()).throw(
                       ww.LockBusy("control lock busy")))
        self.assertEqual(tw.serve_step(self.sdir), "busy")
        self.assertEqual(fake.requests, [])
        self.assertEqual(self.runtime()["desired_seen"], "running")  # 心跳仍维护

    def _interrupt_on_quiescing_write(self):
        """注入 Ctrl-C：_save_control 先真实落盘（quiescing 已写入），再抛中断。"""
        real_save = tw._save_control

        def saving(directory, control):
            real_save(directory, control)
            if control.get("desired") == "quiescing":
                raise KeyboardInterrupt()  # 真实写已完成后的用户中断

        self.patch(tw, "_save_control", saving)

    def test_stop_keyboard_interrupt_after_quiescing_write_rolls_back_running(self):
        self.stop_seeded()
        self.install_gh(entries=[(7, "closed")])
        fake = self.install_http(lambda rec: (200, {}))
        FakeTaskService.state["registered"] = True
        self._interrupt_on_quiescing_write()
        rc, _, err = self.run_cli(["stop", "--state-dir", self.sdir])
        self.assertEqual(rc, 130)                       # Ctrl-C 语义退出码
        self.assertIn("已中断", err)
        self.assertIn("状态保留", err)
        self.assertNotIn("Traceback", err)
        control = self.control()
        self.assertEqual(control["desired"], "running")  # quiescing 已回滚
        self.assertNotIn("stopped_ts", control)
        self.assertTrue(control.get("scope_check"))     # 中断前真实落盘过范围
        self.assertEqual(FakeTaskService.state["remove_calls"], 0)
        self.assertEqual(fake.requests, [])             # 未接触远端

    def test_pause_keyboard_interrupt_after_quiescing_write_rolls_back_running(self):
        self.seeded_control()
        self.init_consumer()
        self.install_gh(entries=[(7, "open")])
        real_save = tw._save_control
        self._interrupt_on_quiescing_write()
        FakeTaskService.state["registered"] = True
        self.write_webhook_state(pending={"batch_id": "b7", "events": []})
        rc, _, err = self.run_cli(["pause", "--reason", "用户授权暂停",
                                   "--state-dir", self.sdir])
        self.assertEqual(rc, 130)
        self.assertIn("已中断", err)
        control = self.control()
        self.assertEqual(control["desired"], "running")  # 不停在 quiescing/paused
        pending = self.webhook_state()["pending"]
        self.assertEqual(pending["batch_id"], "b7")     # 批次不丢
        self.assertEqual(FakeTaskService.state["remove_calls"], 0)


# ---------------------------------------------------------------- CLI 形式

class CliTests(GWCase):

    def test_cli_state_dir_and_label_aliases(self):
        # --state-dir 放子命令前
        self.install_gh(entries=[(7, "open")])
        self.install_http(lambda rec: (200, consumer_payload()))
        rc, _, err = self.run_cli(
            ["--state-dir", self.sdir] + self.start_argv(sdir=None))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.control()["binding"]["task_label"], "task-t1")
        # --state-dir 放子命令后 + --label 别名
        dir2 = os.path.join(self.sdir, "second")
        rc, _, err = self.run_cli(
            ["start", "--state-dir", dir2, "--repo", self.binding["repo"],
             "--repository-id", self.binding["repository_id"],
             "--label", self.binding["task_label"],
             "--subscription-key", "run-1",
             "--token-file", self.token_file,
             "--watch-url", self.binding["watch_url"],
             "--gh-path", self.gh_path])
        self.assertEqual(rc, 0, err)
        control2 = tw._load_control(dir2)
        self.assertEqual(control2["binding"]["task_label"], "task-t1")
        self.assertEqual(control2["phase"], "ready")


if __name__ == "__main__":
    unittest.main(verbosity=2)