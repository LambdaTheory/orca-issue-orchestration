#!/usr/bin/env python3
"""On-demand per-task OS supervision. No registration occurs on import or prepare."""
from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import xml.etree.ElementTree as ET


class ServiceError(Exception):
    pass


def _run(argv):
    try:
        return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceError("系统托管命令不可用或超时: %s" % argv[0]) from exc


def _checked(argv):
    result = _run(argv)
    if result.returncode:
        raise ServiceError("%s 失败: %s" % (argv[0], (result.stderr or result.stdout)[-600:]))
    return result.stdout


def derive_task_name(directory):
    path = os.path.normcase(str(Path(directory).expanduser().resolve()))
    return "orca-task-watch-" + hashlib.sha256(path.encode()).hexdigest()[:16]


def _ps(script):
    # 固定脚本＋JSON base64 数据，不把路径或用户名插值为可执行 PowerShell。
    script = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); " + script
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    output = _checked(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded])
    try:
        return json.loads(output.lstrip("\ufeff").strip())
    except ValueError as exc:
        raise ServiceError("PowerShell 未返回有效结构化状态") from exc


def _ps_data(data):
    raw = base64.b64encode(json.dumps(data).encode()).decode("ascii")
    return "$d=([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('%s')) | ConvertFrom-Json); " % raw


class TaskService:
    def __init__(self, state_dir):
        self.state_dir = str(Path(state_dir).expanduser().resolve())
        self.name = derive_task_name(self.state_dir)
        self.script = str(Path(__file__).resolve().with_name("task_watch.py"))
        self.platform = "macos" if sys.platform == "darwin" else "windows" if sys.platform == "win32" else "linux" if sys.platform.startswith("linux") else "unsupported"

    def prepare(self):
        binding = {"platform": self.platform, "name": self.name, "state_dir": self.state_dir,
                   "python": sys.executable, "script": self.script}
        if self.platform == "macos":
            for domain in ("gui/%s" % os.getuid(), "user/%s" % os.getuid()):
                result = _run(["launchctl", "print", domain])
                if not result.returncode:
                    binding["domain"] = domain
                    break
                if "could not find domain" not in (result.stderr + result.stdout).lower():
                    raise ServiceError("无法检查 launchd 用户域: " + result.stderr[-400:])
            else:
                raise ServiceError("没有可用的 launchd 用户域")
        elif self.platform == "linux":
            result = _run(["systemctl", "--user", "is-system-running"])
            if result.stdout.strip() not in ("running", "degraded"):
                raise ServiceError("systemd 用户管理器不可用；不自动提权或启用 linger")
            binding["unit"] = self.name + ".service"
        elif self.platform == "windows":
            info = _ps("$me=[Security.Principal.WindowsIdentity]::GetCurrent(); "
                       "$accounts=@(Get-CimInstance Win32_UserAccount -Filter (\"SID='\"+$me.User.Value+\"'\")); "
                       "$sessions=@($accounts | ForEach-Object { Get-CimAssociatedInstance -InputObject $_ -Association Win32_LoggedOnUser -ResultClassName Win32_LogonSession }); "
                       "$ok=@($sessions | Where-Object { $_.LogonType -in 2,10,11 }).Count -gt 0; "
                       "@{user=$me.Name;sid=$me.User.Value;interactive=$ok}|ConvertTo-Json -Compress")
            if not info.get("interactive"):
                raise ServiceError("当前用户无可确认的交互登录；纯 SSH 环境需另行配置，不自动存密码或提权")
            binding.update(user=info["user"], sid=info["sid"])
        else:
            raise ServiceError("不支持此平台的系统托管")
        return binding

    def _validate(self, binding):
        for key, expected in (("platform", self.platform), ("name", self.name),
                              ("state_dir", self.state_dir), ("script", self.script)):
            if binding.get(key) != expected:
                raise ServiceError("系统任务绑定不匹配: " + key)
        if not os.path.isabs(binding.get("python", "")):
            raise ServiceError("绑定解释器必须为绝对路径")
        if any("\n" in value or "\r" in value for value in self._args(binding)):
            raise ServiceError("托管路径不能含换行符，无法可靠核验系统任务定义")
        if self.platform == "macos" and binding.get("domain") not in ("gui/%s" % os.getuid(), "user/%s" % os.getuid()):
            raise ServiceError("launchd 域不属于当前用户")
        if self.platform == "linux" and binding.get("unit") != self.name + ".service":
            raise ServiceError("systemd unit 不匹配")
        if self.platform == "windows" and not re.fullmatch(r"S-\d+(?:-\d+)+", binding.get("sid", "")):
            raise ServiceError("缺少有效的计划任务属主 SID")

    def _args(self, binding):
        return [binding["python"], "-X", "utf8", self.script, "serve", "--state-dir", self.state_dir]

    def _mac_query(self, binding):
        target = binding["domain"] + "/" + self.name
        result = _run(["launchctl", "print", target])
        if result.returncode:
            if "could not find service" in (result.stderr + result.stdout).lower():
                return None
            raise ServiceError("无法查询 launchd 任务（不能当作未注册）: " + result.stderr[-400:])
        text = result.stdout
        match = re.search(r"(?m)^\s*arguments\s*=\s*\{\s*\n(.*?)^\s*\}", text, re.S)
        if not match:
            raise ServiceError("无法核实已有 launchd 任务的参数")
        args = [line.strip().rstrip(",") for line in match.group(1).splitlines() if line.strip()]
        if args != self._args(binding):
            raise ServiceError("已有 launchd 任务不属于本绑定，不接管或注销")
        state = re.search(r"(?m)^\s*state\s*=\s*(\S+)", text)
        if not state:
            raise ServiceError("launchd 状态不可解析")
        return {"running": state.group(1) == "running", "detail": state.group(1)}

    def _linux_query(self, binding):
        result = _run(["systemctl", "--user", "show", binding["unit"],
                       "--property=LoadState,ActiveState,SubState,ExecStart,WorkingDirectory,Transient"])
        props = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if props.get("LoadState") == "not-found":
            return None
        if result.returncode or props.get("LoadState") != "loaded":
            raise ServiceError("无法查询 systemd 任务（状态未知）")
        # systemctl show joins argv with spaces; include its field delimiter so
        # a semicolon inside a legitimate path does not truncate the comparison.
        expected = " ".join(self._args(binding))
        command = props.get("ExecStart", "")
        argv_match = re.search(r"argv\[\]=(.*) ; ignore_errors=", command)
        if (props.get("Transient") != "yes" or props.get("WorkingDirectory") != self.state_dir or
                not argv_match or argv_match.group(1) != expected):
            raise ServiceError("已有 systemd 任务定义与本绑定不一致，拒绝接管/注销")
        if props.get("ActiveState") not in ("active", "inactive", "failed", "activating", "deactivating"):
            raise ServiceError("未知 systemd 活动状态")
        return {"running": props["ActiveState"] == "active", "detail": props["ActiveState"]}

    def _win_query(self, binding):
        data = _ps(_ps_data({"name": self.name}) +
                   "$s=New-Object -ComObject Schedule.Service; $s.Connect(); $f=$s.GetFolder('\\'); "
                   "try { $t=$f.GetTask($d.name) } catch { "
                   "$e=$_.Exception; while ($e.InnerException) { $e=$e.InnerException }; "
                   "if ($e.HResult -eq -2147024894) { @{exists=$false}|ConvertTo-Json -Compress; exit 0 }; throw }; "
                   "@{exists=$true;state=[int]$t.State;xml=$t.Xml}|ConvertTo-Json -Compress")
        if data.get("exists") is False:
            return None
        if data.get("exists") is not True:
            raise ServiceError("任务计划程序返回未知状态")
        try:
            root = ET.fromstring(data["xml"])
        except (KeyError, ET.ParseError) as exc:
            raise ServiceError("无法解析已有计划任务定义") from exc
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        principal = root.find("t:Principals/t:Principal", ns)
        actions = root.findall("t:Actions/t:Exec", ns)
        if principal is None or len(actions) != 1:
            raise ServiceError("计划任务属主/执行动作未知")
        user = principal.findtext("t:UserId", "", ns)
        action = actions[0]
        if (user.casefold() not in (binding["sid"].casefold(), binding["user"].casefold()) or
                principal.findtext("t:LogonType", "", ns) != "InteractiveToken" or
                action.findtext("t:Command", "", ns).casefold() != binding["python"].casefold() or
                action.findtext("t:Arguments", "", ns) != subprocess.list2cmdline(self._args(binding)[1:]) or
                action.findtext("t:WorkingDirectory", "", ns) != self.state_dir):
            raise ServiceError("已有计划任务属主或命令不匹配，不接管/注销")
        if type(data.get("state")) is not int or data["state"] not in range(5):
            raise ServiceError("计划任务运行状态未知")
        return {"running": data["state"] == 4, "detail": str(data["state"])}

    def _query(self, binding):
        self._validate(binding)
        return getattr(self, "_" + self.platform.replace("macos", "mac").replace("windows", "win") + "_query")(binding)

    def status(self, binding):
        state = self._query(binding)
        return {"registered": state is not None, "running": bool(state and state["running"]),
                "detail": state["detail"] if state else "未注册"}

    def _write_mac(self, binding):
        path = Path(self.state_dir) / "task-watch.plist"
        content = {"Label": self.name, "ProgramArguments": self._args(binding), "KeepAlive": True,
                   "ThrottleInterval": 10, "ProcessType": "Background", "WorkingDirectory": self.state_dir,
                   "StandardOutPath": str(Path(self.state_dir) / "task-watch.out.log"),
                   "StandardErrorPath": str(Path(self.state_dir) / "task-watch.err.log")}
        path.write_bytes(plistlib.dumps(content))
        path.chmod(0o600)
        return str(path)

    def _windows_xml(self, binding):
        ns = "http://schemas.microsoft.com/windows/2004/02/mit/task"
        ET.register_namespace("", ns)
        def child(parent, name, text=None, **attrs):
            node = ET.SubElement(parent, "{%s}%s" % (ns, name), attrs)
            if text is not None:
                node.text = text
            return node
        root = ET.Element("{%s}Task" % ns, version="1.2")
        triggers = child(root, "Triggers")
        trigger = child(triggers, "TimeTrigger")
        repetition = child(trigger, "Repetition")
        child(repetition, "Interval", "PT1M")
        child(repetition, "StopAtDurationEnd", "false")
        child(trigger, "StartBoundary", datetime.now().astimezone().isoformat(timespec="seconds"))
        child(trigger, "Enabled", "true")
        principal = child(child(root, "Principals"), "Principal", id="Author")
        child(principal, "UserId", binding["sid"])
        child(principal, "LogonType", "InteractiveToken")
        child(principal, "RunLevel", "LeastPrivilege")
        settings = child(root, "Settings")
        for key, value in (("MultipleInstancesPolicy", "IgnoreNew"),
                           ("DisallowStartIfOnBatteries", "false"), ("StopIfGoingOnBatteries", "false"),
                           ("StartWhenAvailable", "true"), ("RunOnlyIfNetworkAvailable", "false")):
            child(settings, key, value)
        idle = child(settings, "IdleSettings")
        child(idle, "StopOnIdleEnd", "false")
        child(idle, "RestartOnIdle", "false")
        for key, value in (("AllowStartOnDemand", "true"), ("Enabled", "true"),
                           ("RunOnlyIfIdle", "false"), ("ExecutionTimeLimit", "PT0S")):
            child(settings, key, value)
        restart = child(settings, "RestartOnFailure")
        child(restart, "Interval", "PT1M")
        child(restart, "Count", "3")
        action = child(child(root, "Actions", Context="Author"), "Exec")
        child(action, "Command", binding["python"])
        child(action, "Arguments", subprocess.list2cmdline(self._args(binding)[1:]))
        child(action, "WorkingDirectory", self.state_dir)
        return ET.tostring(root, encoding="utf-16", xml_declaration=True)

    def ensure(self, binding):
        self._validate(binding)
        if not os.path.isfile(self.script) or not os.path.isfile(binding["python"]):
            raise ServiceError("托管脚本或解释器不存在")
        state = self._query(binding)
        if state and state["running"]:
            return
        if self.platform == "macos":
            if state:
                _checked(["launchctl", "kickstart", binding["domain"] + "/" + self.name])
            else:
                path = self._write_mac(binding)
                _checked(["launchctl", "bootstrap", binding["domain"], path])
        elif self.platform == "linux":
            if state:
                _checked(["systemctl", "--user", "start", binding["unit"]])
            else:
                _checked(["systemd-run", "--user", "--unit", binding["unit"],
                          "--property=Type=exec", "--property=Restart=always", "--property=RestartSec=10s",
                          "--property=StartLimitIntervalSec=0", "--property=CollectMode=inactive-or-failed",
                          "--property=WorkingDirectory=" + self.state_dir, "--"] + self._args(binding))
        else:
            if not state:
                path = Path(self.state_dir) / "task-watch.task.xml"
                path.write_bytes(self._windows_xml(binding))
                _checked(["schtasks.exe", "/Create", "/TN", self.name, "/XML", str(path)])
            _checked(["schtasks.exe", "/Run", "/TN", self.name])

    def remove(self, binding):
        state = self._query(binding)  # 核验归属，不能仅凭名字停止其他任务。
        if state:
            if self.platform == "macos":
                _checked(["launchctl", "bootout", binding["domain"] + "/" + self.name])
            elif self.platform == "linux":
                _checked(["systemctl", "--user", "stop", binding["unit"]])
            else:
                _ps(_ps_data({"name": self.name}) +
                    "$s=New-Object -ComObject Schedule.Service; $s.Connect(); $f=$s.GetFolder('\\'); "
                    "$t=$f.GetTask($d.name); $t.Enabled=$false; "
                    "if ($t.State -eq 4) { try { $t.Stop(0) } catch { "
                    "$e=$_.Exception; while ($e.InnerException) { $e=$e.InnerException }; "
                    "if ($e.HResult -ne -2147216629) { throw } } }; "
                    "$f.DeleteTask($d.name,0); "
                    "@{ok=$true}|ConvertTo-Json -Compress")
        if self._query(binding) is not None:
            raise ServiceError("系统任务撤销未完成，请保留 stopped 状态并重试收尾")
        filename = "task-watch.plist" if self.platform == "macos" else "task-watch.task.xml" if self.platform == "windows" else None
        if filename:
            (Path(self.state_dir) / filename).unlink(missing_ok=True)
