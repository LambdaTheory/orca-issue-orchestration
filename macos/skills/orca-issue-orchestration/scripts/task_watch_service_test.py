#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""task_watch_service（重写版）的隔离单元测试（仅标准库）。

拦截策略（两层，杜绝任何真实系统调用）：
1. mock 掉 ``task_watch_service._run``：默认（未命中规则）抛 AssertionError，
   即默认拒绝执行 launchctl/systemctl/schtasks/PowerShell 等外部命令；
2. ``subprocess.run`` 全局替换为禁用桩，拦截任何绕过 _run 的路径。

PowerShell：只对模块级 ``_ps`` 做 EncodedCommand payload 与 ``_ps_data``
数据转义的独立测试；其余 Windows 场景直接 mock ``svc._ps`` 返回结构化
数据，不运行任何 PowerShell。测试不发 schtasks/systemctl 真实请求、不
注册/注销任何系统任务、不联网。
运行：python3 -B task_watch_service_test.py
"""
import base64
import json
import os
import re
import plistlib
import stat
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import task_watch_service as svc  # noqa: E402

TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
NAME_RE = re.compile(r"^orca-task-watch-[0-9a-f]{16}$")


def uid():
    try:
        return os.getuid()
    except AttributeError:  # pragma: no cover - 非 POSIX 主机回退
        return 0


class Proc(object):
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


class StrictRunner(object):
    """替代 svc._run：按规则分发；未命中规则一律 AssertionError（默认拒绝）。"""

    def __init__(self):
        self.calls = []
        self.rules = []

    def on(self, matcher, response):
        self.rules.append((matcher, response))

    def clear(self):
        self.calls = []
        self.rules = []

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        for matcher, response in self.rules:
            if matcher(argv):
                return response(argv) if callable(response) else response
        raise AssertionError("测试默认拒绝执行外部命令: %r" % (argv,))

    def calls_of(self, head, *rest):
        prefix = [head] + list(rest)
        return [c for c in self.calls if c[:len(prefix)] == prefix]

    def count_of(self, head, *rest):
        return len(self.calls_of(head, *rest))

    def only_calls(self):
        return self.calls


class ServiceCase(unittest.TestCase):
    """每个用例：临时 state_dir + 双层外部命令拦截。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="tws-test-")
        self.tmp = self._tmpdir.name
        self.fake = StrictRunner()
        run_patcher = mock.patch.object(svc, "_run", self.fake)
        run_patcher.start()
        self.addCleanup(run_patcher.stop)

        def forbidden(*argv, **kwargs):
            raise AssertionError("测试禁止真实 subprocess.run: %r" % (argv,))
        subprocess_patcher = mock.patch("subprocess.run", forbidden)
        subprocess_patcher.start()
        self.addCleanup(subprocess_patcher.stop)
        self.addCleanup(self._tmpdir.cleanup)

    def patch_platform(self, value):
        patcher = mock.patch.object(svc.sys, "platform", value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_service(self, state_dir=None, platform=None):
        if platform is not None:
            self.patch_platform(
                {"macos": "darwin", "windows": "win32"}.get(platform,
                                                            platform))
        return svc.TaskService(state_dir or self.tmp)

    # ---- 绑定构造器（与 prepare 的产物结构一致）

    def mac_binding(self, ts):
        return {"platform": "macos", "name": ts.name,
                "state_dir": ts.state_dir, "python": sys.executable,
                "script": ts.script, "domain": "gui/%d" % uid()}

    def linux_binding(self, ts):
        return {"platform": "linux", "name": ts.name,
                "state_dir": ts.state_dir, "python": sys.executable,
                "script": ts.script, "unit": ts.name + ".service"}

    def win_binding(self, ts, user="DESK\\alice",
                    sid="S-1-5-21-1004336348-1177238915-682003330-1000"):
        return {"platform": "windows", "name": ts.name,
                "state_dir": ts.state_dir, "python": sys.executable,
                "script": ts.script, "user": user, "sid": sid}

    # ---- mock svc._ps

    def install_ps(self, handler):
        """把 svc._ps 替换为 handler(script)；记录每次收到的脚本到 self.ps_calls。"""
        self.ps_calls = []

        def wrapper(script):
            self.ps_calls.append(script)
            return handler(script)
        patcher = mock.patch.object(svc, "_ps", wrapper)
        patcher.start()
        self.addCleanup(patcher.stop)
        return self.ps_calls

    # ---- macOS launchctl print fixture

    def mac_print(self, ts, b, state="running"):
        """真实 launchctl print 风格输出，arguments 块 = 本绑定确切命令。"""
        args = ts._args(b)
        body = "\n".join("\t\t%s," % a for a in args)
        return Proc(0, "\ttarget\t= %s\n"
                       "\targuments\t= {\n%s\n\t};\n"
                       "\tstate\t= %s\n"
                       % (b["domain"] + "/" + ts.name, body, state), "")

    def mac_not_found(self, ts, b):
        target = b["domain"] + "/" + ts.name
        return Proc(37, "",
                    'Could not find service: "%s" in %s' % (target, "gui"))

    def _print_rule(self, ts, b, outputs):
        """outputs: Proc 或 str（str 视为 state 字）；序列超出后复用最后一个。"""
        target = b["domain"] + "/" + ts.name
        procs = []
        for item in outputs:
            if isinstance(item, Proc):
                procs.append(item)
            elif item == "notfound":
                procs.append(self.mac_not_found(ts, b))
            else:
                procs.append(self.mac_print(ts, b, state=item))
        counter = {"n": 0}

        def handler(cmd):
            proc = procs[min(counter["n"], len(procs) - 1)]
            counter["n"] += 1
            return proc
        self.fake.on(lambda c: c[:3] == ["launchctl", "print", target],
                     handler)

    # ---- systemctl --user show fixture

    def show_proc(self, ts, b, load="loaded", active="active", sub="running",
                  transient="yes", workdir=None, argv=None,
                  include_exec=True, rc=0):
        workdir = workdir if workdir is not None else ts.state_dir
        lines = [("LoadState", load), ("ActiveState", active), ("SubState", sub)]
        if include_exec:
            args = list(argv) if argv is not None else ts._args(b)
            exec_start = "{ path=%s ; argv[]=%s ; ignore_errors=no ; }" % (
                ts._args(b)[0], " ".join(args))
            lines.append(("ExecStart", exec_start))
        lines.append(("WorkingDirectory", workdir))
        lines.append(("Transient", transient))
        return Proc(rc, "".join("%s=%s\n" % kv for kv in lines), "")

    def _show_rule(self, ts, b, specs):
        """specs: dict 序列（show_proc 的 kwargs）；超出后复用最后一个。"""
        procs = [self.show_proc(ts, b, **spec) for spec in specs]
        counter = {"n": 0}

        def handler(cmd):
            proc = procs[min(counter["n"], len(procs) - 1)]
            counter["n"] += 1
            return proc
        self.fake.on(
            lambda c: c[:3] == ["systemctl", "--user", "show"], handler)

    # ---- Windows 任务定义 fixture（COM 查询返回的 Xml 字符串，无声明头）

    def win_task_xml(self, ts, b, user=None, command=None, arguments=None,
                     workdir=None, logon="InteractiveToken"):
        default_args = subprocess.list2cmdline(
            ["-X", "utf8", ts.script, "serve", "--state-dir", ts.state_dir])
        entries = [
            ("t:Principals/t:Principal/t:UserId",
             user if user is not None else b["sid"]),
            ("t:Principals/t:Principal/t:LogonType", logon),
            ("t:Actions/t:Exec/t:Command",
             command if command is not None else b["python"]),
            ("t:Actions/t:Exec/t:Arguments",
             arguments if arguments is not None else default_args),
            ("t:Actions/t:Exec/t:WorkingDirectory",
             workdir if workdir is not None else ts.state_dir),
        ]
        root = ET.Element("{%s}Task" % TASK_NS, version="1.2")

        def descend(parent, path, text=None):
            node = parent
            for tag in path.split("/"):
                tag = tag.split(":")[-1]
                existing = node.find("{%s}%s" % (TASK_NS, tag))
                if existing is None:
                    existing = ET.SubElement(
                        node, "{%s}%s" % (TASK_NS, tag))
                node = existing
            if text is not None:
                node.text = text
            return node

        for path, text in entries:
            descend(root, path, text)
        return ET.tostring(root, encoding="unicode")


class TestDeriveTaskName(unittest.TestCase):
    def test_deterministic_hex_and_path_sensitive(self):
        first = svc.derive_task_name("/tmp/watch-a")
        self.assertEqual(first, svc.derive_task_name("/tmp/watch-a"))
        self.assertRegex(first, NAME_RE,
                         "任务名仅含确定性散列，天然 shell 安全")
        self.assertNotEqual(first, svc.derive_task_name("/tmp/watch-b"))
        self.assertNotEqual(first, svc.derive_task_name(
            '/tmp/$(rm -rf ~); & | "x'))


class TestPsShell(unittest.TestCase):
    """模块级 _ps：EncodedCommand 编码 payload 与数据通道转义。"""

    def setUp(self):
        self.calls = []
        self.response = Proc(0, '{"ok": true}', "")

        def fake_run(argv):
            self.calls.append(list(argv))
            return self.response
        patcher = mock.patch.object(svc, "_run", fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_encoded_command_payload(self):
        result = svc._ps("Write-Output hi")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(self.calls), 1)
        argv = self.calls[0]
        self.assertEqual(argv[:4], ["powershell.exe", "-NoProfile",
                                    "-NonInteractive", "-EncodedCommand"])
        script = base64.b64decode(argv[4]).decode("utf-16le")
        self.assertTrue(script.startswith("$ErrorActionPreference='Stop';"),
                        "固定安全前缀，禁止注入")
        self.assertIn("[Console]::OutputEncoding=", script)
        self.assertTrue(script.endswith("Write-Output hi"),
                        "用户脚本原样追加：路径/用户名不得插值为代码")

    def test_ps_data_value_channel_roundtrip(self):
        data = {"name": 'x"y\\z', "note": "中文 with\"quote",
                "path": "C:/a&b<c>\tsp"}
        prefix = svc._ps_data(data)
        self.assertTrue(prefix.startswith("$d=("))
        self.assertIn("ConvertFrom-Json", prefix)
        encoded = re.search(r"FromBase64String\('([^']+)'\)", prefix).group(1)
        self.assertEqual(json.loads(base64.b64decode(encoded).decode("utf-8")),
                         data, "含引号/反斜杠/中文的数据必须无损转义")

    def test_nonzero_exit_raises(self):
        self.response = Proc(1, "", "boom")
        with self.assertRaises(svc.ServiceError):
            svc._ps("x")

    def test_invalid_json_raises(self):
        self.response = Proc(0, "<not json>", "")
        with self.assertRaises(svc.ServiceError) as ctx:
            svc._ps("x")
        self.assertIn("PowerShell", str(ctx.exception))


class TestPrepare(ServiceCase):
    # ---- macOS

    def test_prepare_macos_prefers_gui_and_never_touches_state(self):
        ts = self.make_service(platform="macos")
        self.fake.on(
            lambda c: c[:3] == ["launchctl", "print", "gui/%d" % uid()],
            Proc(0, "", ""))
        binding = ts.prepare()
        self.assertEqual(binding["platform"], "macos")
        self.assertEqual(binding["name"], ts.name)
        self.assertEqual(binding["state_dir"], ts.state_dir)
        self.assertEqual(binding["python"], sys.executable)
        self.assertEqual(binding["script"], ts.script)
        self.assertEqual(binding["domain"], "gui/%d" % uid())
        self.assertEqual(self.fake.calls,
                         [["launchctl", "print", "gui/%d" % uid()]],
                         "prepare 只探测域，不注册/不写文件")
        dumped = json.dumps(binding, sort_keys=True)
        for token in ("bearer ", "ghp_", "password"):  # token 前缀凭据样例
            self.assertNotIn(token, dumped.lower())
        self.assertFalse({"token", "secret", "password"} &
                         {k.lower() for k in binding},
                         "凭据凭键名识别，不因路径/名字含子串而误判")

    def test_prepare_binding_survives_token_named_state_dir(self):
        state_dir = os.path.join(self.tmp, "token-just-a-dirname")
        ts = self.make_service(state_dir=state_dir, platform="macos")
        self.fake.on(lambda c: c[:2] == ["launchctl", "print"], Proc(0))
        binding = ts.prepare()
        json.dumps(binding)
        self.assertEqual(binding["state_dir"], ts.state_dir,
                         "state_dir 含 token 子串不得被误判为秘密")
        self.assertFalse({"token", "secret", "password"} &
                         {k.lower() for k in binding})

    def test_prepare_macos_falls_back_to_user_domain(self):
        ts = self.make_service(platform="macos")
        self.fake.on(lambda c: c[:3] == ["launchctl", "print",
                                         "gui/%d" % uid()],
                     Proc(37, "", 'Could not find domain "gui"'))
        self.fake.on(lambda c: c[:3] == ["launchctl", "print",
                                         "user/%d" % uid()], Proc(0))
        self.assertEqual(ts.prepare()["domain"], "user/%d" % uid())

    def test_prepare_macos_no_domain_raises(self):
        ts = self.make_service(platform="macos")
        self.fake.on(lambda c: c[:2] == ["launchctl", "print"],
                     Proc(37, "", "Could not find domain"))
        with self.assertRaises(svc.ServiceError) as ctx:
            ts.prepare()
        self.assertIn("launchd", str(ctx.exception))

    def test_prepare_macos_unknown_probe_error_raises(self):
        ts = self.make_service(platform="macos")
        self.fake.on(lambda c: c[:3] == ["launchctl", "print",
                                         "gui/%d" % uid()],
                     Proc(9, "", "Operation not permitted"))
        with self.assertRaises(svc.ServiceError) as ctx:
            ts.prepare()
        self.assertIn("launchd", str(ctx.exception))
        self.assertEqual(len(self.fake.calls), 1,
                         "非域缺失错误必须立刻失败，不得静默换域")

    # ---- Linux

    def test_prepare_linux_running_or_degraded_is_valid(self):
        for stdout, rc in (("running\n", 0), ("degraded\n", 1)):
            with self.subTest(stdout=stdout):
                ts = self.make_service(platform="linux")
                self.fake.clear()
                self.fake.on(
                    lambda c: c[:3] == ["systemctl", "--user",
                                        "is-system-running"],
                    Proc(rc, stdout, ""))
                binding = ts.prepare()
                self.assertEqual(binding["platform"], "linux")
                self.assertEqual(binding["unit"], ts.name + ".service")
                self.assertEqual(
                    [c[0] for c in self.fake.calls],
                    ["systemctl"], "degraded 同样可用；prepare 不启动任务")

    def test_prepare_linux_no_user_manager_raises(self):
        ts = self.make_service(platform="linux")
        self.fake.on(lambda c: c[:3] == ["systemctl", "--user",
                                         "is-system-running"],
                     Proc(1, "offline\n",
                          "System has not been booted with systemd"))
        with self.assertRaises(svc.ServiceError) as ctx:
            ts.prepare()
        self.assertIn("不自动提权", str(ctx.exception))
        self.assertIn("linger", str(ctx.exception))
        self.assertEqual(
            [c[:3] for c in self.fake.calls],
            [["systemctl", "--user", "is-system-running"]])

    # ---- Windows

    def test_prepare_windows_interactive_ok_and_ssh_rejected(self):
        ps_calls = self.install_ps(
            lambda script: {"user": "DESK\\alice",
                            "sid": "S-1-5-21-1004336348-1177238915-682003330"
                                   "-1000", "interactive": True})
        ts = self.make_service(platform="windows")
        binding = ts.prepare()
        self.assertEqual(binding["user"], "DESK\\alice")
        self.assertRegex(binding["sid"], r"^S-\d+(?:-\d+)+$")
        self.assertEqual(self.fake.calls, [], "_ps 已 mock：绝不运行外部进程")
        self.assertTrue(any("LogonType" in s for s in ps_calls),
                        "interactiveness 探测脚本需核对登录会话类型")

        self.install_ps(
            lambda script: {"user": "DESK\\alice",
                            "sid": "S-1-5-21-1004336348-1177238915-682003330"
                                   "-1000", "interactive": False})
        ts2 = self.make_service(platform="windows")
        with self.assertRaises(svc.ServiceError) as ctx:
            ts2.prepare()
        self.assertIn("SSH", str(ctx.exception))
        self.assertIn("不自动存密码", str(ctx.exception))


class TestBindingValidation(ServiceCase):
    def test_common_key_mismatches_rejected(self):
        builder = {"macos": self.mac_binding, "linux": self.linux_binding,
                   "windows": self.win_binding}
        for platform in ("macos", "linux", "windows"):
            ts = self.make_service(platform=platform)
            binding = builder[platform](ts)
            ts._validate(binding)  # 正例先通过
            for key, bad in (("platform", "other"), ("name", "other"),
                             ("state_dir", "/other/dir"),
                             ("script", "/other/script.py"),
                             ("python", "relative/python")):
                with self.subTest(platform=platform, key=key):
                    wrong = dict(binding)
                    wrong[key] = bad
                    with self.assertRaises(svc.ServiceError):
                        ts._validate(wrong)

    def test_platform_specific_ownership_mismatch_rejected(self):
        cases = []
        ts = self.make_service(platform="macos")
        mac = self.mac_binding(ts)
        mac["domain"] = "gui/99999999"
        cases.append(("mac domain 非本人", ts, mac))
        ts_l = self.make_service(platform="linux")
        lin = self.linux_binding(ts_l)
        lin["unit"] = "other.service"
        cases.append(("linux unit 不匹配", ts_l, lin))
        ts_w = self.make_service(platform="windows")
        win = self.win_binding(ts_w)
        win["sid"] = "not-a-sid"
        cases.append(("windows sid 非法", ts_w, win))
        for label, ts, binding in cases:
            with self.subTest(case=label):
                with self.assertRaises(svc.ServiceError):
                    ts._validate(binding)


class TestMacOs(ServiceCase):
    def make_service(self, state_dir=None, platform=None):
        return super().make_service(state_dir, platform or "macos")

    # ---- status

    def test_status_truthful_across_launchd_states(self):
        ts = self.make_service()
        b = self.mac_binding(ts)
        self._print_rule(ts, b, ["running"])
        result = ts.status(b)
        self.assertEqual(result["registered"], True)
        self.assertEqual(result["running"], True)
        self.assertEqual(result["detail"], "running")

        self.fake.clear()
        self._print_rule(ts, b, ["exited"])
        result = ts.status(b)
        self.assertEqual((result["registered"], result["running"]),
                         (True, False))
        self.assertEqual(result["detail"], "exited")

        self.fake.clear()
        self._print_rule(ts, b, ["notfound"])
        result = ts.status(b)
        self.assertEqual((result["registered"], result["running"],
                          result["detail"]), (False, False, "未注册"))

    def test_status_unknown_print_never_faked_as_missing(self):
        ts = self.make_service()
        b = self.mac_binding(ts)
        # 已注册但 print 输出无法解析参数 → 拒绝，不当作未注册/未运行
        self._print_rule(ts, b, [Proc(0, "\tstate\t= running\n", "")])
        with self.assertRaises(svc.ServiceError) as ctx:
            ts.status(b)
        self.assertIn("参数", str(ctx.exception))

        # 有参数块但缺 state → 拒绝
        self.fake.clear()
        args = ts._args(b)
        body = "\n".join("\t\t%s," % a for a in args)
        self._print_rule(ts, b, [Proc(0, "\targuments\t= {\n%s\n\t};\n"
                                      % body, "")])
        with self.assertRaises(svc.ServiceError) as ctx:
            ts.status(b)
        self.assertIn("状态", str(ctx.exception))

        # 查询本身失败（未知错误）→ 拒绝当作缺失
        self.fake.clear()
        self._print_rule(ts, b, [Proc(9, "", "IPC transport error")])
        with self.assertRaises(svc.ServiceError) as ctx:
            ts.status(b)
        self.assertIn("不能当作未注册", str(ctx.exception))

    # ---- ensure

    def test_ensure_bootstraps_new_task_with_state_dir_plist(self):
        ts = self.make_service()
        b = self.mac_binding(ts)
        self._print_rule(ts, b, ["notfound"])
        self.fake.on(lambda c: c[:2] == ["launchctl", "bootstrap"], Proc(0))
        self.assertIsNone(ts.ensure(b))
        plist_path = os.path.join(ts.state_dir, "task-watch.plist")
        boots = self.fake.calls_of("launchctl", "bootstrap")
        self.assertEqual(boots, [["launchctl", "bootstrap", b["domain"],
                                  plist_path]])
        self.assertEqual(self.fake.count_of("launchctl", "kickstart"), 0)

        with open(plist_path, "rb") as f:
            data = plistlib.load(f)
        self.assertEqual(data["Label"], ts.name)
        self.assertEqual(data["ProgramArguments"], ts._args(b))
        self.assertIs(data["KeepAlive"], True)
        self.assertEqual(data["ThrottleInterval"], 10)
        self.assertEqual(data["ProcessType"], "Background")
        self.assertEqual(data["WorkingDirectory"], ts.state_dir)
        self.assertEqual(data["StandardOutPath"],
                         os.path.join(ts.state_dir, "task-watch.out.log"))
        self.assertEqual(data["StandardErrorPath"],
                         os.path.join(ts.state_dir, "task-watch.err.log"))
        self.assertEqual(stat.S_IMODE(os.stat(plist_path).st_mode) & 0o077, 0,
                         "plist 必须 0600（其他用户不可读）")
        self.assertNotIn("LaunchAgents",
                         json.dumps(self.fake.calls),
                         "定义写在 state_dir，绝不写 LaunchAgents")

        # bootstrap 失败不可吞掉
        self.fake.clear()
        self._print_rule(ts, b, ["notfound"])
        self.fake.on(lambda c: c[:2] == ["launchctl", "bootstrap"],
                     Proc(5, "", "Bootstrap failed: 5: Input/output error"))
        with self.assertRaises(svc.ServiceError):
            ts.ensure(b)

    def test_ensure_idempotent_when_running(self):
        ts = self.make_service()
        b = self.mac_binding(ts)
        self._print_rule(ts, b, ["running"])
        self.assertIsNone(ts.ensure(b))
        self.assertIsNone(ts.ensure(b))
        self.assertEqual(self.fake.calls,
                         [["launchctl", "print",
                           b["domain"] + "/" + ts.name]] * 2,
                         "运行中只做查询，不得重启/重装")

    def test_ensure_kickstarts_registered_but_stopped_task(self):
        ts = self.make_service()
        b = self.mac_binding(ts)
        self._print_rule(ts, b, ["exited"])
        self.fake.on(lambda c: c[:2] == ["launchctl", "kickstart"], Proc(0))
        ts.ensure(b)
        kicks = self.fake.calls_of("launchctl", "kickstart")
        self.assertEqual(kicks, [["launchctl", "kickstart",
                                  b["domain"] + "/" + ts.name]])
        self.assertEqual(self.fake.count_of("launchctl", "bootstrap"), 0)
        self.assertFalse(os.path.exists(
            os.path.join(ts.state_dir, "task-watch.plist")),
            "已注册任务不被重写定义")

    def test_ensure_fail_closed_on_foreign_definition_or_missing_python(
            self):
        ts = self.make_service()
        b = self.mac_binding(ts)
        # 已注册同名任务但 arguments 与本绑定不同 → 拒绝接管/注销
        self._print_rule(ts, b, [Proc(
            0, "\ttarget\t= %s\n\targuments\t= {\n\t\t%s,\n\t\tserve,\n"
               "\t\t--state-dir,\n\t\t/elsewhere\n\t};\n\tstate\t= running\n"
               % (b["domain"] + "/" + ts.name, ts.script), "")])
        with self.assertRaises(svc.ServiceError) as ctx:
            ts.ensure(b)
        self.assertIn("不接管", str(ctx.exception))
        self.assertEqual(len(self.fake.calls), 1,
                         "定义不明时不得 bootstrap/kickstart")

        # 绑定的解释器文件不存在 → 直接失败，不做任何外部调用
        self.fake.clear()
        broken = dict(b)
        broken["python"] = "/nonexistent/python"
        with self.assertRaises(svc.ServiceError) as ctx:
            ts.ensure(broken)
        self.assertIn("托管脚本", str(ctx.exception))
        self.assertEqual(self.fake.calls, [])

    # ---- remove

    def test_remove_bootouts_verifies_and_deletes_plist(self):
        ts = self.make_service()
        b = self.mac_binding(ts)
        plist_path = os.path.join(ts.state_dir, "task-watch.plist")
        with open(plist_path, "wb") as f:
            f.write(b"<plist><dict/></plist>")

        # 场景 A：已注册 → bootout → 复查消失 → 删本地定义
        self._print_rule(ts, b, ["running", "notfound"])
        self.fake.on(lambda c: c[:2] == ["launchctl", "bootout"], Proc(0))
        ts.remove(b)
        self.assertEqual(self.fake.calls_of("launchctl", "bootout"),
                         [["launchctl", "bootout",
                           b["domain"] + "/" + ts.name]])
        self.assertFalse(os.path.exists(plist_path))

        # 场景 B：本就未注册 → 幂等，不 bootout
        self.fake.clear()
        self._print_rule(ts, b, ["notfound"])
        ts.remove(b)
        self.assertEqual(self.fake.count_of("launchctl", "bootout"), 0)

    def test_remove_failures_fail_closed(self):
        ts = self.make_service()
        b = self.mac_binding(ts)
        plist_path = os.path.join(ts.state_dir, "task-watch.plist")
        with open(plist_path, "wb") as f:
            f.write(b"<plist><dict/></plist>")

        # bootout 失败：诚实抛错，保留 plist
        self._print_rule(ts, b, ["running"])
        self.fake.on(lambda c: c[:2] == ["launchctl", "bootout"],
                     Proc(7, "", "Operation not permitted"))
        with self.assertRaises(svc.ServiceError):
            ts.remove(b)
        self.assertTrue(os.path.exists(plist_path),
                        "注销未成功不得删本地定义")

        # bootout 成功但复查发现任务还在 → 拒绝完成收尾
        self.fake.clear()
        self._print_rule(ts, b, ["running"])   # 每次 print 都是 running
        self.fake.on(lambda c: c[:2] == ["launchctl", "bootout"], Proc(0))
        with self.assertRaises(svc.ServiceError) as ctx:
            ts.remove(b)
        self.assertIn("未完成", str(ctx.exception))
        self.assertTrue(os.path.exists(plist_path))

        # 同名任务定义属于别人（参数不同）→ 不卸载
        self.fake.clear()
        self._print_rule(ts, b, [Proc(
            0, "\targuments\t= {\n\t\t/elsewhere/python,\n\t\tserve,\n"
               "\t\t--state-dir,\n\t\t/elsewhere\n\t};\n\tstate\t= running\n",
            "")])
        with self.assertRaises(svc.ServiceError) as ctx:
            ts.remove(b)
        self.assertIn("不接管", str(ctx.exception))
        self.assertEqual(self.fake.count_of("launchctl", "bootout"), 0)
        self.assertTrue(os.path.exists(plist_path))


class TestLinux(ServiceCase):
    def make_service(self, state_dir=None, platform=None):
        return super().make_service(state_dir, platform or "linux")

    def test_status_truthful_across_unit_states(self):
        ts = self.make_service()
        b = self.linux_binding(ts)
        cases = [
            ({}, True, True, "active"),
            ({"active": "inactive", "sub": "dead"}, True, False, "inactive"),
            ({"active": "failed", "sub": "failed"}, True, False, "failed"),
            ({"active": "activating", "sub": "start"}, True, False,
             "activating"),
            ({"load": "not-found", "active": "inactive", "sub": "dead",
              "rc": 1}, False, False, "未注册"),
            ({"load": "not-found", "rc": 0}, False, False, "未注册"),
        ]
        for spec, registered, running, detail in cases:
            with self.subTest(spec=spec):
                self.fake.clear()
                self._show_rule(ts, b, [spec])
                result = ts.status(b)
                self.assertEqual(set(result),
                                 {"registered", "running", "detail"})
                self.assertIsInstance(result["registered"], bool)
                self.assertIsInstance(result["running"], bool)
                self.assertEqual(result["registered"], registered)
                self.assertEqual(result["running"], running)
                self.assertEqual(result["detail"], detail)

    def test_status_rejects_foreign_or_unverifiable_definitions(self):
        ts = self.make_service()
        b = self.linux_binding(ts)
        foreign = [
            {"transient": "no"},
            {"workdir": "/elsewhere"},
            {"argv": [ts.script, "serve", "--state-dir", "/other"]},
            {"include_exec": False},
        ]
        for spec in foreign:
            with self.subTest(spec=spec):
                self.fake.clear()
                self._show_rule(ts, b, [spec])
                with self.assertRaises(svc.ServiceError) as ctx:
                    ts.status(b)
                self.assertIn("拒绝接管", str(ctx.exception))
        # show 失败（dbus/未知）不得当作缺失
        with self.subTest("show 失败"):
            self.fake.clear()
            self.fake.on(
                lambda c: c[:3] == ["systemctl", "--user", "show"],
                Proc(1, "", "Failed to connect to bus"))
            with self.assertRaises(svc.ServiceError) as ctx:
                ts.status(b)
            self.assertIn("状态未知", str(ctx.exception))
        # 未知 ActiveState → 拒绝
        with self.subTest("未知 ActiveState"):
            self.fake.clear()
            self._show_rule(ts, b, [{"active": "unknown", "sub": "??"}])
            with self.assertRaises(svc.ServiceError) as ctx:
                ts.status(b)
            self.assertIn("未知 systemd 活动状态", str(ctx.exception))

    def test_ensure_new_task_uses_exact_transient_unit_command(self):
        ts = self.make_service()
        b = self.linux_binding(ts)
        self._show_rule(ts, b, [{"load": "not-found", "active": "inactive",
                                 "sub": "dead", "rc": 1}])
        self.fake.on(lambda c: c[0] == "systemd-run", Proc(0))
        self.assertIsNone(ts.ensure(b))
        runs = self.fake.calls_of("systemd-run")
        self.assertEqual(runs, [[
            "systemd-run", "--user", "--unit", b["unit"],
            "--property=Type=exec", "--property=Restart=always",
            "--property=RestartSec=10s", "--property=StartLimitIntervalSec=0",
            "--property=CollectMode=inactive-or-failed",
            "--property=WorkingDirectory=" + ts.state_dir, "--",
        ] + ts._args(b)])
        self.assertFalse(
            any("token" in t.lower() for t in runs[0]),
            "命令中不得携带凭据（环境变量转交也禁止）")
        # systemd-run 失败诚实抛错
        self.fake.clear()
        self._show_rule(ts, b, [{"load": "not-found", "active": "inactive",
                                 "sub": "dead"}])
        self.fake.on(lambda c: c[0] == "systemd-run",
                     Proc(1, "", "Failed to start transient service"))
        with self.assertRaises(svc.ServiceError):
            ts.ensure(b)

    def test_ensure_active_is_noop_and_inactive_is_started(self):
        ts = self.make_service()
        b = self.linux_binding(ts)
        self._show_rule(ts, b, [{"active": "active", "sub": "running"}])
        ts.ensure(b)
        ts.ensure(b)
        self.assertEqual(self.fake.count_of("systemd-run"), 0)
        self.assertEqual(self.fake.count_of("systemctl", "--user", "start"),
                         0, "运行中的任务不得重启")

        self.fake.clear()
        self._show_rule(ts, b, [{"active": "inactive", "sub": "dead"}])
        self.fake.on(lambda c: c[:3] == ["systemctl", "--user", "start"],
                     Proc(0))
        ts.ensure(b)
        self.assertEqual(self.fake.calls_of("systemctl", "--user", "start"),
                         [["systemctl", "--user", "start", b["unit"]]])
        self.assertEqual(self.fake.count_of("systemd-run"), 0,
                         "已有定义停止态用 start 拉起，不再 systemd-run 新建")

    def test_remove_flows(self):
        ts = self.make_service()
        b = self.linux_binding(ts)
        # 未注册：幂等，不 stop
        self._show_rule(ts, b, [{"load": "not-found", "active": "inactive",
                                 "sub": "dead"}])
        ts.remove(b)
        self.assertEqual(self.fake.count_of("systemctl", "--user", "stop"), 0)

        # 已注册：stop → 复查 not-found
        self.fake.clear()
        self._show_rule(ts, b, [{"active": "active", "sub": "running"},
                                {"load": "not-found", "active": "inactive",
                                 "sub": "dead"}])
        self.fake.on(lambda c: c[:3] == ["systemctl", "--user", "stop"],
                     Proc(0))
        ts.remove(b)
        self.assertEqual(self.fake.calls_of("systemctl", "--user", "stop"),
                         [["systemctl", "--user", "stop", b["unit"]]])

    def test_remove_failures_fail_closed(self):
        ts = self.make_service()
        b = self.linux_binding(ts)
        self._show_rule(ts, b, [{"active": "active", "sub": "running"}])
        self.fake.on(lambda c: c[:3] == ["systemctl", "--user", "stop"],
                     Proc(1, "", "Job for unit canceled"))
        with self.assertRaises(svc.ServiceError):
            ts.remove(b)

        # stop 成功但单元仍在 → 拒绝宣称完成
        self.fake.clear()
        self._show_rule(ts, b, [{"active": "active", "sub": "running"}])
        self.fake.on(lambda c: c[:3] == ["systemctl", "--user", "stop"],
                     Proc(0))
        with self.assertRaises(svc.ServiceError) as ctx:
            ts.remove(b)
        self.assertIn("未完成", str(ctx.exception))


class TestWindows(ServiceCase):
    def make_service(self, state_dir=None, platform=None):
        return super().make_service(state_dir, platform or "windows")

    def test_task_xml_full_structure(self):
        ts = self.make_service()
        b = self.win_binding(ts)
        xml_bytes = ts._windows_xml(b)
        text = xml_bytes.decode("utf-16")
        self.assertIn("encoding='utf-16'", text)
        root = ET.fromstring(xml_bytes)
        self.assertEqual(root.tag, "{%s}Task" % TASK_NS)
        self.assertEqual(root.attrib.get("version"), "1.2")
        ns = {"t": TASK_NS}

        trigger = root.find("t:Triggers/t:TimeTrigger", ns)
        if trigger is None:
            raise self.failureException("缺少 TimeTrigger")
        repetition = trigger.find("t:Repetition", ns)
        self.assertEqual(repetition.findtext("t:Interval", "", ns), "PT1M")
        self.assertIsNone(repetition.find("t:Duration", ns),
                          "重复无期限，不得写 Duration")
        self.assertEqual(repetition.findtext("t:StopAtDurationEnd", "", ns),
                         "false")
        self.assertTrue(trigger.findtext("t:StartBoundary", "", ns))
        self.assertEqual(trigger.findtext("t:Enabled", "", ns), "true")

        principal = root.find("t:Principals/t:Principal", ns)
        self.assertEqual(principal.attrib, {"id": "Author"})
        self.assertEqual(principal.findtext("t:UserId", "", ns), b["sid"])
        self.assertEqual(principal.findtext("t:LogonType", "", ns),
                         "InteractiveToken")
        self.assertEqual(principal.findtext("t:RunLevel", "", ns),
                         "LeastPrivilege")

        actions = root.find("t:Actions", ns)
        self.assertEqual(actions.attrib, {"Context": "Author"})
        execs = actions.findall("t:Exec", ns)
        self.assertEqual(len(execs), 1)
        self.assertEqual(execs[0].findtext("t:Command", "", ns), b["python"])
        self.assertEqual(execs[0].findtext("t:Arguments", "", ns),
                         subprocess.list2cmdline(ts._args(b)[1:]))
        self.assertEqual(execs[0].findtext("t:WorkingDirectory", "", ns),
                         ts.state_dir)

        def setting(name):
            return root.findtext("t:Settings/t:%s" % name, None, ns)
        self.assertEqual(setting("MultipleInstancesPolicy"), "IgnoreNew")
        self.assertEqual(setting("ExecutionTimeLimit"), "PT0S")
        self.assertEqual(setting("DisallowStartIfOnBatteries"), "false")
        self.assertEqual(setting("StopIfGoingOnBatteries"), "false")
        self.assertEqual(setting("RunOnlyIfIdle"), "false")
        self.assertEqual(setting("StartWhenAvailable"), "true")
        self.assertEqual(setting("AllowStartOnDemand"), "true")
        self.assertEqual(setting("RunOnlyIfNetworkAvailable"), "false")
        restart = root.find("t:Settings/t:RestartOnFailure", ns)
        self.assertEqual(restart.findtext("t:Interval", "", ns), "PT1M")
        self.assertEqual(restart.findtext("t:Count", "", ns), "3")
        idle = root.find("t:Settings/t:IdleSettings", ns)
        self.assertEqual(idle.findtext("t:StopOnIdleEnd", "", ns), "false")
        self.assertEqual(idle.findtext("t:RestartOnIdle", "", ns), "false")
        self.assertNotIn("Password", text)
        self.assertNotIn("Bearer ", text)

    def test_task_xml_escapes_special_paths_and_quotes(self):
        ts = self.make_service(
            state_dir=os.path.join(self.tmp, "spa ce & <x>&"))
        b = self.win_binding(ts)
        b["python"] = 'C:\\Program Files\\Py q"ote\\python.exe'
        xml_bytes = ts._windows_xml(b)
        root = ET.fromstring(xml_bytes)
        ns = {"t": TASK_NS}
        exec_node = root.find("t:Actions/t:Exec", ns)
        self.assertEqual(exec_node.findtext("t:Command", "", ns), b["python"])
        self.assertEqual(exec_node.findtext("t:WorkingDirectory", "", ns),
                         ts.state_dir)
        self.assertEqual(exec_node.findtext("t:Arguments", "", ns),
                         subprocess.list2cmdline(ts._args(b)[1:]),
                         "参数一律 list2cmdline，&<>/引号/空格路径可无损还原")

    # ---- _win_query

    def test_query_ours_and_missing(self):
        ts = self.make_service()
        b = self.win_binding(ts)
        xml = self.win_task_xml(ts, b)
        ps_calls = self.install_ps(
            lambda script: {"exists": True, "state": 4, "xml": xml})
        self.assertEqual((ts.status(b)["registered"],
                          ts.status(b)["running"],
                          ts.status(b)["detail"]), (True, True, "4"))
        # 查询脚本里的「找不到」必须以 HRESULT 判定，不得吞异常后盲猜
        for marker in ("HResult -eq -2147024894", "exists=$false", "exit 0",
                       "throw"):
            self.assertIn(marker, ps_calls[0])
        self.install_ps(lambda script: {"exists": True, "state": 3,
                                        "xml": xml})
        self.assertEqual((ts.status(b)["registered"],
                          ts.status(b)["running"]), (True, False))
        # UserId 写用户名（而非 SID）同样认为是自己的任务
        xml_by_user = self.win_task_xml(ts, b, user=b["user"])
        self.install_ps(lambda script: {"exists": True, "state": 1,
                                        "xml": xml_by_user})
        self.assertEqual((ts.status(b)["registered"],
                          ts.status(b)["running"]), (True, False))
        # missing → 中性：返回 exists=False 即当作未注册
        self.install_ps(lambda script: {"exists": False})
        self.assertEqual((ts.status(b)["registered"],
                          ts.status(b)["running"],
                          ts.status(b)["detail"]), (False, False, "未注册"))

    def test_query_rejects_foreign_owners_or_commands(self):
        ts = self.make_service()
        b = self.win_binding(ts)
        variants = [
            ("UserId 非本人", {"user": "OTHER\\bob"}),
            ("Command 非本绑定解释器", {"command": "C:\\evil\\python.exe"}),
            ("Arguments 不匹配", {"arguments": '"-X" utf8 --other'}),
            ("WorkingDirectory 不匹配", {"workdir": "C:\\elsewhere"}),
            ("LogonType Basic", {"logon": "Basic"}),
        ]
        for label, kwargs in variants:
            with self.subTest(case=label):
                self.install_ps(
                    lambda script, kw=kwargs: {
                        "exists": True, "state": 3,
                        "xml": self.win_task_xml(ts, b, **kw)})
                with self.assertRaises(svc.ServiceError) as ctx:
                    ts.status(b)
                self.assertIn("不接管", str(ctx.exception))

    def test_query_garbage_never_faked_as_missing(self):
        ts = self.make_service()
        b = self.win_binding(ts)
        xml = self.win_task_xml(ts, b)
        garbage = [
            ("exists 非布尔", {"exists": 1, "state": 3, "xml": xml}),
            ("state 非整数", {"exists": True, "state": "Ready", "xml": xml}),
            ("state 越界", {"exists": True, "state": 7, "xml": xml}),
            ("xml 缺失键", {"exists": True, "state": 3}),
            ("xml 无法解析", {"exists": True, "state": 3, "xml": "<not-xml"}),
            ("xml 缺 Principal/Actions", {"exists": True, "state": 3,
                                          "xml": "<Task/>"}),
        ]
        for label, data in garbage:
            with self.subTest(case=label):
                self.install_ps(lambda script, d=data: d)
                with self.assertRaises(svc.ServiceError):
                    ts.status(b)
                self.assertNotIn("DeleteTask",
                                 self.ps_calls[-1] if self.ps_calls else "",
                                 "状态不明时绝不触发删除")

    # ---- ensure

    def test_ensure_flows(self):
        # 场景 1：全新任务 → 写 XML + /Create + /Run
        ts = self.make_service(state_dir=os.path.join(self.tmp, "fresh"))
        os.makedirs(ts.state_dir, exist_ok=True)
        b = self.win_binding(ts)
        xml_path = os.path.join(ts.state_dir, "task-watch.task.xml")
        self.install_ps(lambda script: {"exists": False})
        self.fake.on(lambda c: c[:2] == ["schtasks.exe", "/Create"], Proc(0))
        self.fake.on(lambda c: c[:2] == ["schtasks.exe", "/Run"], Proc(0))
        ts.ensure(b)
        self.assertTrue(os.path.isfile(xml_path))
        with open(xml_path, encoding="utf-16") as f:
            content = f.read()
        self.assertIn("PT1M", content)
        self.assertIn("InteractiveToken", content)
        self.assertEqual(self.fake.calls_of("schtasks.exe", "/Create"),
                         [["schtasks.exe", "/Create", "/TN", ts.name,
                           "/XML", xml_path]])
        self.assertEqual(self.fake.calls_of("schtasks.exe", "/Run"),
                         [["schtasks.exe", "/Run", "/TN", ts.name]])
        self.assertNotIn("ghp_", content)
        self.assertNotIn("<Password>", content)

        # 场景 2：已注册但未运行 → 不重建，仅 /Run 拉起
        self.fake.clear()
        ts2 = self.make_service(state_dir=os.path.join(self.tmp, "fresh2"))
        os.makedirs(ts2.state_dir, exist_ok=True)
        b2 = self.win_binding(ts2)
        xml_text2 = self.win_task_xml(ts2, b2)
        self.install_ps(
            lambda script: {"exists": True, "state": 3, "xml": xml_text2})
        self.fake.on(lambda c: c[:2] == ["schtasks.exe", "/Run"], Proc(0))
        ts2.ensure(b2)
        self.assertEqual(self.fake.count_of("schtasks.exe", "/Create"), 0)
        self.assertEqual(self.fake.calls_of("schtasks.exe", "/Run"),
                         [["schtasks.exe", "/Run", "/TN", ts2.name]])

        # 场景 3：既已运行 → 完全 noop
        self.fake.clear()
        ts3 = self.make_service(state_dir=os.path.join(self.tmp, "fresh3"))
        os.makedirs(ts3.state_dir, exist_ok=True)
        b3 = self.win_binding(ts3)
        xml_text3 = self.win_task_xml(ts3, b3)
        self.install_ps(
            lambda script: {"exists": True, "state": 4, "xml": xml_text3})
        self.assertIsNone(ts3.ensure(b3))
        self.assertEqual([c for c in self.fake.calls
                          if c[0] == "schtasks.exe"], [])

    def test_ensure_failures_fail_closed(self):
        ts = self.make_service()
        b = self.win_binding(ts)
        self.install_ps(lambda script: {"exists": False})
        # /Create 失败 → 不得 /Run
        self.fake.on(lambda c: c[:2] == ["schtasks.exe", "/Create"],
                     Proc(1, "", "ERROR: Access is denied."))
        with self.assertRaises(svc.ServiceError):
            ts.ensure(b)
        self.assertEqual(self.fake.count_of("schtasks.exe", "/Run"), 0)
        # /Create 成功但 /Run 失败 → 诚实抛错
        self.fake.clear()
        self.install_ps(lambda script: {"exists": False})
        self.fake.on(lambda c: c[:2] == ["schtasks.exe", "/Create"], Proc(0))
        self.fake.on(lambda c: c[:2] == ["schtasks.exe", "/Run"],
                     Proc(1, "", "ERROR: Access is denied."))
        with self.assertRaises(svc.ServiceError):
            ts.ensure(b)

    # ---- remove

    def test_remove_disable_stop_delete_then_verify_gone(self):
        ts = self.make_service()
        b = self.win_binding(ts)
        xml_path = os.path.join(ts.state_dir, "task-watch.task.xml")
        with open(xml_path, "wb") as f:
            f.write(ts._windows_xml(b))
        state = {"deleted": False}

        def handler(script):
            if "DeleteTask" in script:
                state["deleted"] = True
                return {"ok": True}
            if "GetTask" in script:
                if not state["deleted"]:
                    return {"exists": True, "state": 3,
                            "xml": self.win_task_xml(ts, b)}
                return {"exists": False}
            raise AssertionError("未预期脚本: %s" % script)
        ps_calls = self.install_ps(handler)
        ts.remove(b)
        delete_scripts = [s for s in ps_calls if "DeleteTask" in s]
        self.assertEqual(len(delete_scripts), 1)
        for marker in (
                "if ($t.State -eq 4)",          # 仅运行态才尝试 Stop
                "try { $t.Stop(0) }",
                "HResult -ne -2147216629",      # SCHED_E_TASK_NOT_RUNNING 竞态放行
                "$f.DeleteTask($d.name,0)",     # 无论是否 Stop 均继续删除
                "$t.Enabled=$false"):
            self.assertIn(marker, delete_scripts[0])
        encoded = re.search(r"FromBase64String\('([^']+)'\)",
                            delete_scripts[0]).group(1)

        self.assertEqual(json.loads(base64.b64decode(encoded).decode("utf-8")),
                         {"name": ts.name})
        self.assertFalse(os.path.exists(xml_path),
                         "注销成功后本地 XML 一并清理")

        # 非运行（State=3）：跳过 Stop 但 Delete 照常；运行（State=4）：脚本
        # 条件本身由 PowerShell 端处理（断言见上方 markers），二者同一路径
        state2 = {"deleted": False}
        ts_b = self.make_service(state_dir=os.path.join(self.tmp, "run-then-d"))
        os.makedirs(ts_b.state_dir, exist_ok=True)
        b_b = self.win_binding(ts_b)

        def handler2(script):
            if "DeleteTask" in script:
                state2["deleted"] = True
                return {"ok": True}
            if "GetTask" in script:
                if not state2["deleted"]:
                    return {"exists": True, "state": 4,
                            "xml": self.win_task_xml(ts_b, b_b)}
                return {"exists": False}
            raise AssertionError("未预期脚本: %s" % script)
        self.install_ps(handler2)
        ts_b.remove(b_b)
        self.assertFalse(os.path.exists(
            os.path.join(ts_b.state_dir, "task-watch.task.xml")))

    def test_remove_refuses_foreign_or_surviving_task(self):
        ts = self.make_service()
        b = self.win_binding(ts)
        xml_path = os.path.join(ts.state_dir, "task-watch.task.xml")
        with open(xml_path, "wb") as f:
            f.write(ts._windows_xml(b))

        # 属主/命令不匹配 → 拒绝，且不执行 Disable/Stop/Delete
        self.install_ps(
            lambda script: {"exists": True, "state": 3,
                            "xml": self.win_task_xml(
                                ts, b, command="C:\\evil\\python.exe")})
        with self.assertRaises(svc.ServiceError) as ctx:
            ts.remove(b)
        self.assertIn("不接管", str(ctx.exception))
        self.assertFalse(any("DeleteTask" in s for s in self.ps_calls))
        self.assertTrue(os.path.exists(xml_path))

        # 删除成功但复查任务仍在 → 拒绝宣称完成
        def handler(script):
            if "DeleteTask" in script:
                return {"ok": True}
            if "GetTask" in script:
                return {"exists": True, "state": 3,
                        "xml": self.win_task_xml(ts, b)}
            raise AssertionError("未预期脚本: %s" % script)
        self.install_ps(handler)
        with self.assertRaises(svc.ServiceError) as ctx:
            ts.remove(b)
        self.assertIn("未完成", str(ctx.exception))
        self.assertTrue(any("DeleteTask" in s for s in self.ps_calls))
        self.assertTrue(os.path.exists(xml_path))


if __name__ == "__main__":
    unittest.main(verbosity=2)