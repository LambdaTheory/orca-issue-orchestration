#!/usr/bin/env python3
"""Decide whether a blocked approval is safe to auto-approve for this run.

Two callers share this table:

  orch_guard.py  reads a pending permission prompt and presses the matching option
  PreToolUse hook returns permissionDecision before a prompt is ever shown

Only "allow" and "hold" exist. hold never means deny: it means a human (or the
standby agent) has to look. When the table cannot prove a command is safe the
answer is hold, because a wrong allow is not reversible.
"""
import argparse, json, os, re, shlex, sys
from pathlib import Path

ARTIFACT_NAMES = frozenset({
    "node_modules", ".cache", "dist", "build", "target", ".next", "coverage",
    ".turbo", "out", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".mypy_cache", ".parcel-cache", ".nuxt", ".svelte-kit", ".tox", ".gradle",
    ".eslintcache", ".tsbuildinfo", "tmp", "temp", ".tmp",
})
ARTIFACT_SUFFIXES = (".log", ".tmp", ".pyc", ".pyo", ".DS_Store")
SENSITIVE_PARTS = ("/.ssh", "/.aws", "/.gnupg", "/.config/gh", "/.docker/config.json",
                   "/.netrc", "/.git-credentials")
PROTECTED_BRANCHES = frozenset({"main", "master", "trunk", "develop", "release"})
READ_ONLY_PROGRAMS = frozenset({
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "pwd", "which", "whoami",
    "diff", "cmp", "sort", "uniq", "cut", "tr", "basename", "dirname", "realpath",
    "stat", "file", "date", "true", "false", "test", "echo", "printf", "jq",
    "find", "du", "df", "uname", "id", "env", "sleep", "awk", "column", "rev",
    "nl", "md5sum", "shasum", "sha256sum", "tree", "ps", "pgrep",
})
WRITE_PROGRAMS = frozenset({"mkdir", "touch", "cp", "mv", "ln", "install"})
TEST_PROGRAMS = frozenset({
    "pytest", "ruff", "eslint", "tsc", "vitest", "jest", "mocha", "prettier",
    "black", "flake8", "mypy", "isort", "golangci-lint", "shellcheck", "npx",
})
PACKAGE_SCRIPTS = frozenset({"test", "lint", "build", "typecheck", "check", "format", "verify"})
SHELLS = frozenset({"sh", "bash", "zsh", "fish", "csh", "tcsh", "powershell", "pwsh", "cmd"})


class Ctx(dict):
    """Plain dict with defaults so callers can pass only what they know."""

    def __init__(self, workspace=None, branch="", extra_allow=None, run_dirs=None, skill_dirs=None):
        super().__init__(
            workspace=str(workspace or os.getcwd()),
            branch=branch or "",
            home=os.path.expanduser("~"),
            extra_allow=list(extra_allow or []),
            run_dirs=[str(p) for p in (run_dirs or [])],
            skill_dirs=[str(p) for p in (skill_dirs or [])],
        )


def allow(rule, reason=""):
    return {"decision": "allow", "rule": rule, "reason": reason}


def hold(rule, reason):
    return {"decision": "hold", "rule": rule, "reason": reason}


# ---------------------------------------------------------------- path helpers

def _real(path, cwd):
    p = os.path.expanduser(str(path))
    if not os.path.isabs(p):
        p = os.path.join(cwd, p)
    return os.path.realpath(p)


def inside(path, root):
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(root)]) == os.path.realpath(root)
    except ValueError:
        return False


def looks_dynamic(token):
    return any(ch in token for ch in "$`*?[{}") or token.startswith("~") or ".." in Path(token).parts


def sensitive(path):
    real = os.path.realpath(os.path.expanduser(str(path)))
    return any(real.startswith(os.path.expanduser("~" + part)) or part in real for part in SENSITIVE_PARTS) \
        or Path(real).name.startswith(".env")


def is_artifact(real, ctx):
    for d in ctx["run_dirs"]:
        if inside(real, d):
            return True
    if real == os.path.realpath(ctx["workspace"]):
        return False
    if any(part in ARTIFACT_NAMES for part in Path(real).parts):
        return True
    return Path(real).name.endswith(ARTIFACT_SUFFIXES)


# ---------------------------------------------------------------- command lexing

def split_pipeline(command):
    """Split on top-level && || ; | and newlines, keeping quoted text intact."""
    parts, buf, quote, i = [], [], None, 0
    while i < len(command):
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(command):
                i += 1
                buf.append(command[i])
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < len(command):
            buf.append(command[i:i + 2])
            i += 2
            continue
        if command[i:i + 2] in ("&&", "||"):
            parts.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch in ";|\n":
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


REDIRECT = re.compile(r"(?<![<>])&?>>?\s*([^\s;&|<>]+)")


def redirect_targets(command):
    return [m.group(1) for m in REDIRECT.finditer(command)]


def tokenize(segment):
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return None


def strip_prefix(tokens):
    """Drop env assignments and transparent wrappers; return (tokens, blocked_reason)."""
    while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
        tokens = tokens[1:]
    while tokens and tokens[0] in ("env", "command", "time", "nohup", "stdbuf", "builtin"):
        tokens = tokens[1:]
        while tokens and tokens[0].startswith("-"):
            tokens = tokens[1:]
    if tokens and tokens[0] == "sudo":
        return tokens, "sudo 需要人工确认"
    return tokens, None


def path_args(tokens):
    out = []
    for token in tokens:
        if token.startswith("-"):
            continue
        if token in ("|", "&&", "||", ";"):
            continue
        out.append(token)
    return out


def strip_option_values(tokens, options):
    """Drop options and the value that follows them, so messages are not read as paths."""
    out, skip = [], False
    for token in tokens:
        if skip:
            skip = False
            continue
        if token in options:
            skip = True
            continue
        out.append(token)
    return out


def check_write(tokens, cwd, ctx, rule):
    for token in path_args(tokens):
        if token in ("dev/null", "/dev/null"):
            continue
        if sensitive(_real(token, cwd)):
            return hold("path.sensitive", f"目标涉及凭据或系统目录：{token}")
        if looks_dynamic(token):
            return hold("path.dynamic", f"路径含变量、通配或上级引用，无法静态确认：{token}")
        real = _real(token, cwd)
        if not (inside(real, ctx["workspace"]) or inside(real, "/tmp")):
            return hold("path.outside", f"目标在工作区之外：{token}")
    return allow(rule)


def check_read(tokens, cwd, rule):
    """Reads are cheap; only credential paths are held. Relative patterns stay untouched."""
    for token in path_args(tokens):
        if token.startswith(("/", "~")) and sensitive(_real(token, cwd)):
            return hold("path.sensitive", f"读取凭据或系统目录：{token}")
        if Path(token).name.startswith(".env") or Path(token).name in ("id_rsa", "id_ed25519", ".netrc"):
            return hold("path.sensitive", f"读取凭据文件：{token}")
    return allow(rule)


# ---------------------------------------------------------------- per-program checks

def check_git(args, cwd, ctx):
    sub = args[0] if args else ""
    rest = args[1:]
    flags = [a for a in rest if a.startswith("-")]
    values = [a for a in rest if not a.startswith("-")]

    if sub in ("status", "log", "diff", "show", "rev-parse", "ls-files", "blame",
               "shortlog", "describe", "fetch", "tag", "stash"):
        if sub == "stash" and any(f.startswith("-") and "list" not in f for f in flags):
            return hold("git.stash.write", "git stash 会改工作区，需人工确认")
        return allow(f"git.{sub}")
    if sub == "branch":
        if any(f in ("-D", "--delete", "-d") for f in flags):
            if any(f == "-D" for f in flags):
                return hold("git.branch.force-delete", "强制删除分支不可自动放行")
            if any(v in PROTECTED_BRANCHES for v in values):
                return hold("git.branch.protected", "拒绝自动删除受保护分支")
            return allow("git.branch.delete")
        return allow("git.branch.read")
    if sub in ("add", "commit", "switch", "checkout", "restore", "mv", "rm"):
        rest = strip_option_values(rest, ("-m", "--message", "-am", "--author", "-b", "-B", "-c", "-C"))
        values = [a for a in rest if not a.startswith("-")]
        if sub == "checkout" and any(f == "--" for f in rest) and values and values[-1] in (".", "*"):
            return hold("git.checkout.discard", "整目录丢弃修改需人工确认")
        if sub == "restore" and (not values or values[-1] in (".", "*")):
            return hold("git.restore.discard", "整目录丢弃修改需人工确认")
        if sub == "rm" and any(f in ("-rf", "-fr") for f in flags):
            return hold("git.rm.recursive", "git rm 递归删除需人工确认")
        return check_write(rest, cwd, ctx, f"git.{sub}")
    if sub in ("pull", "merge", "rebase", "cherry-pick"):
        if sub == "rebase" and any(f in ("-i", "--interactive") for f in flags):
            return hold("git.rebase.interactive", "交互式 rebase 不可自动放行")
        if sub == "rebase" and not any(f == "--continue" for f in flags):
            return hold("git.rebase", "rebase 会改写历史，需人工确认")
        return allow(f"git.{sub}")
    if sub == "push":
        if any(f in ("-f", "--force", "--force-with-lease", "--force-if-includes") for f in flags) \
                or any(v.startswith("+") for v in values):
            return hold("git.push.force", "强制推送不可自动放行")
        if any(f in ("--delete", "-d") for f in flags):
            return hold("git.push.delete", "删除远端分支不可自动放行")
        targets = [v for v in values if v != "origin"]
        for target in targets:
            branch = target.split(":")[-1]
            if branch in PROTECTED_BRANCHES:
                return hold("git.push.protected", f"拒绝自动推送到受保护分支 {branch}")
        return allow("git.push")
    if sub == "clean":
        joined = "".join(flags)
        if "X" in joined:
            return allow("git.clean.ignored")
        return hold("git.clean.untracked", "git clean 会删除未跟踪文件，需人工确认")
    if sub == "config":
        if any(f in ("--get", "--get-all", "--list", "-l", "--global-get") for f in flags) or not flags:
            return allow("git.config.read")
        return hold("git.config.write", "修改 git 配置需人工确认")
    if sub in ("remote", "submodule", "worktree", "filter-branch", "update-ref", "reset"):
        if sub == "reset" and not any(f == "--hard" for f in flags):
            return allow("git.reset.soft")
        if sub == "remote" and not any(v in ("add", "remove", "rm", "set-url", "rename", "prune") for v in values):
            return allow("git.remote.read")
        return hold(f"git.{sub}", f"git {sub} 需人工确认")
    if sub in ("gc", "prune"):
        return hold(f"git.{sub}", "清理类 git 命令需人工确认")
    return hold("git.unknown", f"未纳入白名单的 git 子命令：{sub or '(空)'}")


def check_gh(args, ctx):
    sub = args[0] if args else ""
    rest = args[1:]
    if sub == "pr":
        action = rest[0] if rest else ""
        if action in ("view", "list", "checks", "status", "diff", "ready"):
            return allow(f"gh.pr.{action}")
        if action in ("create", "edit", "comment", "review"):
            return allow(f"gh.pr.{action}")
        if action == "checkout":
            return allow("gh.pr.checkout")
        if action in ("merge", "close", "reopen"):
            return hold(f"gh.pr.{action}", "PR 合并/关闭不自动放行，按交付授权由协调者决定")
        return hold("gh.pr.unknown", f"未纳入白名单的 gh pr 子命令：{action or '(空)'}")
    if sub == "issue":
        action = rest[0] if rest else ""
        if action in ("view", "list", "create", "edit", "comment", "close", "reopen", "status"):
            return allow(f"gh.issue.{action}")
        return hold("gh.issue.unknown", f"未纳入白名单的 gh issue 子命令：{action or '(空)'}")
    if sub == "api":
        if any(a in ("-X", "--method") for a in rest) or any(
                re.match(r"^(-X|--method)=(PUT|POST|PATCH|DELETE)", a, re.I) for a in rest):
            method = " ".join(rest).upper()
            if any(m in method for m in ("POST", "PATCH", "PUT", "DELETE")):
                return hold("gh.api.write", "写请求不可自动放行")
        if any(a in ("-f", "--field", "--raw-field", "-F", "--input") for a in rest):
            return hold("gh.api.write", "带字段的 gh api 调用不可自动放行")
        return allow("gh.api.read")
    if sub in ("auth", "secret", "ssh-key", "gpg-key", "repo", "release", "workflow", "run"):
        return hold(f"gh.{sub}", f"gh {sub} 涉及凭据或发布，需人工确认")
    if sub in ("search", "label", "status", "browse", "--version", "version"):
        return allow(f"gh.{sub}")
    return hold("gh.unknown", f"未纳入白名单的 gh 子命令：{sub or '(空)'}")


ORCA_ACTIONS = {
    "terminal": {"list", "show", "read", "send", "wait", "create", "split", "rename",
                 "switch", "focus", "close"},
    "orchestration": {"run-create", "run-use", "run-current", "run-list", "run-show", "send",
                      "check", "ask", "reply", "inbox", "task-create", "task-list", "task-update",
                      "dispatch", "dispatch-show", "worker-start", "worker-show", "worker-read",
                      "worker-stop", "worker-release", "worker-retain", "worker-list",
                      "gate-create", "gate-resolve", "gate-list"},
    "worktree": {"list", "show", "current", "create", "set", "ps"},
    "repo": {"list", "show", "search-refs"},
    "project": {"list", "setups"},
    "reference": {"list", "add", "find"},
    "skills": {"list", "get", "installed"},
    "file": {"open", "diff", "open-changed"},
    "host": {"list"},
    "environment": {"list", "show"},
    "agent": {"launch", "context"},
    "search": {"__any__"},
    "status": {"__any__"},
    "diagnostics": {"__any__"},
    "tab": {"create", "list", "show", "current", "switch", "close", "profile"},
    "linear": {"__any__"},
}


def check_orca(args):
    group = args[0] if args else ""
    action = args[1] if len(args) > 1 else ""
    actions = ORCA_ACTIONS.get(group)
    if actions is None:
        return hold("orca.unknown", f"未纳入白名单的 orca 命令：{group or '(空)'}")
    if "__any__" in actions or action in actions:
        return allow(f"orca.{group}")
    return hold(f"orca.{group}.{action}", f"未纳入白名单的 orca {group} 子命令：{action or '(空)'}")


def check_rm(args, cwd, ctx):
    flags = [a for a in args if a.startswith("-")]
    targets = [a for a in args if not a.startswith("-")]
    if not targets:
        return hold("rm.no-target", "删除命令没有可确认的目标")
    if any(t in (".", "./", "..", "~", "/", "/*", "*") for t in targets):
        return hold("rm.root", "删除目标指向当前目录、上级或根，需人工确认")
    for target in targets:
        if looks_dynamic(target):
            return hold("rm.dynamic", f"删除目标含变量或通配，无法静态确认：{target}")
        real = _real(target, cwd)
        if real == os.path.realpath(ctx["workspace"]) or real == os.path.realpath(ctx["home"]) \
                or real == os.path.realpath(os.sep):
            return hold("rm.root", f"拒绝删除工作区根或家目录：{target}")
        if not (inside(real, ctx["workspace"]) or inside(real, "/tmp")):
            return hold("rm.outside", f"删除目标在工作区之外：{target}")
        if not is_artifact(real, ctx):
            return hold("rm.not-artifact", f"目标不像构建/测试产物，需人工确认：{target}")
    recursive = any("r" in f.lower() for f in flags)
    return allow("rm.artifact" + (".recursive" if recursive else ""))


def check_package(program, args):
    """Skip leading flags so `pnpm -C app test` is read as the test action."""
    rest, i = [], 0
    while i < len(args):
        token = args[i]
        if token in ("-C", "--prefix", "--dir", "--cwd", "-F", "--filter"):
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        rest = args[i:]
        break
    if not rest:
        return allow(f"{program}.noop")
    action = rest[0]
    if action in ("test", "t"):
        return allow(f"{program}.test")
    if action in ("run", "run-script") and len(rest) > 1 and rest[1] in PACKAGE_SCRIPTS:
        return allow(f"{program}.run.{rest[1]}")
    if action in ("install", "i", "add", "remove", "rm", "uninstall", "update", "up", "upgrade",
                  "publish", "link", "exec", "dlx", "create", "init", "audit", "outdated"):
        return hold(f"{program}.{action}", "依赖变更/发布需人工确认")
    if action in ("--version", "-v", "version", "ls", "list", "why", "info", "view"):
        return allow(f"{program}.{action}")
    return hold(f"{program}.unknown", f"未纳入白名单的 {program} 子命令：{action}")


PY_VALUE_OPTS = ("-X", "-W", "--check-hash-based-pycs")


def check_python(program, args, cwd, ctx):
    rest, i = [], 0
    while i < len(args):
        token = args[i]
        if token in PY_VALUE_OPTS:
            i += 2
            continue
        if token.startswith("-") and token not in ("-m", "-c"):
            i += 1
            continue
        rest = args[i:]
        break
    if not rest:
        return hold(f"{program}.repl", "裸解释器会进入交互，需人工确认")
    if rest[0] in ("-V", "--version", "-VV"):
        return allow(f"{program}.version")
    if rest[0] == "-m":
        module = rest[1] if len(rest) > 1 else ""
        if module.split(".")[0] in ("pytest", "unittest", "py_compile", "compileall", "json"):
            return allow(f"{program}.m.{module}")
        if module in ("pip", "venv", "ensurepip"):
            return hold(f"{program}.m.{module}", "包管理操作需人工确认")
        return hold(f"{program}.m", f"未纳入白名单的模块：{module}")
    if rest[0] == "-c":
        return hold(f"{program}.c", "内联代码无法静态确认")
    script = rest[0]
    real = _real(script, cwd)
    roots = [ctx["workspace"], *ctx["skill_dirs"], "/tmp"]
    if any(inside(real, root) for root in roots):
        return allow(f"{program}.script")
    return hold(f"{program}.script.outside", f"脚本不在工作区或 Skill 目录：{script}")


def check_curl(program, args):
    write_flags = ("-o", "-O", "--output", "-d", "--data", "-F", "--form", "-T", "--upload-file",
                   "-X", "--request", "-H", "--header", "-u", "--user", "-b", "--cookie")
    if any(a in write_flags or a.startswith(("-d", "-F", "-T", "--data")) for a in args):
        return hold(f"{program}.write", "带请求体/输出/头的下载或上传需人工确认")
    return allow(f"{program}.read")


def check_segment(segment, cwd, ctx):
    """-> (decision, new_cwd)"""
    tokens = tokenize(segment)
    if tokens is None:
        return hold("bash.unparsable", "命令引号不配对，无法解析"), cwd
    tokens, blocked = strip_prefix(tokens)
    if blocked:
        return hold("bash.sudo", blocked), cwd
    if not tokens:
        return allow("bash.empty"), cwd

    program = tokens[0]
    args = tokens[1:]
    bare = os.path.basename(program)
    if os.sep in program and sensitive(program):
        return hold("path.sensitive", f"可执行文件位于敏感目录：{program}"), cwd

    if bare == "cd":
        target = args[0] if args else ctx["home"]
        if looks_dynamic(target):
            return hold("cd.dynamic", f"切换目录含变量，无法确认：{target}"), cwd
        new = _real(target, cwd)
        if not inside(new, ctx["workspace"]):
            return hold("cd.outside", f"切换目录在工作区之外：{target}"), cwd
        return allow("cd"), new
    if bare == "rm" or bare == "rmdir":
        return check_rm(args, cwd, ctx), cwd
    if bare in WRITE_PROGRAMS:
        return check_write(args, cwd, ctx, f"{bare}.workspace"), cwd
    if bare == "sed":
        if any(a.startswith("-i") or a == "--in-place" for a in args):
            return check_write(args, cwd, ctx, "sed.in-place"), cwd
        return check_read(args, cwd, "sed.read"), cwd
    if bare == "find" and any(a in ("-delete", "-exec", "-execdir", "-ok") for a in args):
        return hold("find.delete", "find 的删除/执行动作需人工确认"), cwd
    if bare in READ_ONLY_PROGRAMS:
        return check_read(args, cwd, f"{bare}.read"), cwd
    if bare == "git":
        return check_git(args, cwd, ctx), cwd
    if bare == "gh":
        return check_gh(args, ctx), cwd
    if bare == "orca":
        return check_orca(args), cwd
    if bare in ("npm", "pnpm", "yarn", "bun", "deno"):
        return check_package(bare, args), cwd
    if bare in ("python", "python3", "python3.11", "python3.12", "python3.13", "py"):
        return check_python(bare, args, cwd, ctx), cwd
    if bare in ("curl", "wget"):
        return check_curl(bare, args), cwd
    if bare in ("pytest", "ruff", "eslint", "tsc", "vitest", "jest", "mocha", "mypy",
                "black", "flake8", "isort", "shellcheck", "prettier"):
        return allow(f"{bare}.test"), cwd
    if bare in ("make",):
        targets = [a for a in args if not a.startswith("-")]
        if targets and all(t in PACKAGE_SCRIPTS or t.startswith("test") for t in targets):
            return allow("make.test"), cwd
        return hold("make.unknown", "未确认的 make 目标"), cwd
    if bare in ("go", "cargo"):
        if args and args[0] in ("test", "build", "vet", "check", "fmt"):
            return allow(f"{bare}.{args[0]}"), cwd
        return hold(f"{bare}.unknown", f"未纳入白名单的 {bare} 子命令"), cwd
    if bare in SHELLS:
        return hold("shell.inline", "二次 shell 执行需人工确认"), cwd
    if bare in ("docker", "podman", "kubectl", "brew", "apt", "apt-get", "yum", "dnf",
                "npm-g", "gem", "pip", "pip3", "chmod", "chown", "mkfs", "dd", "mount",
                "launchctl", "systemctl", "crontab"):
        return hold(f"{bare}.system", f"{bare} 属于系统级操作，需人工确认"), cwd
    return hold("bash.unknown", f"未纳入白名单的命令：{bare}"), cwd


def decide_bash(command, ctx, cwd=None):
    cwd = cwd or ctx["workspace"]
    if not isinstance(command, str) or not command.strip():
        return hold("bash.empty", "空命令")
    for pattern in ctx["extra_allow"]:
        try:
            if re.search(pattern, command):
                return allow("config.extra_allow", pattern)
        except re.error:
            return hold("config.bad_pattern", f"额外白名单正则无效：{pattern}")

    for target in redirect_targets(command):
        if target.startswith("&"):
            continue
        if sensitive(_real(target, cwd)):
            return hold("path.sensitive", f"重定向目标涉及敏感路径：{target}")
        real = _real(target, cwd)
        if not (inside(real, ctx["workspace"]) or inside(real, "/tmp") or real == os.devnull):
            return hold("redirect.outside", f"重定向写到工作区之外：{target}")

    last = allow("bash.empty")
    for segment in split_pipeline(command):
        decision, cwd = check_segment(segment, cwd, ctx)
        if decision["decision"] != "allow":
            return decision
        last = decision
    return last


TOOL_ALLOW = {
    "read": "tool.read", "glob": "tool.glob", "grep": "tool.grep", "ls": "tool.ls",
    "todowrite": "tool.todo", "todoread": "tool.todo", "webfetch": "tool.webfetch",
    "websearch": "tool.websearch", "task": "tool.subagent", "agent": "tool.subagent",
    "skill": "tool.skill", "bashoutput": "tool.bash-output", "killbash": "tool.bash-kill",
}

# CodeBuddy sometimes wraps a single Bash call in a REPL cell. Only the exact shape
# below is unwrapped: one Bash call with command (+ optional description) and one
# field returned. Anything else is arbitrary code and stays a hold.
REPL_BASH = re.compile(
    r'^(?:const|let|var)\s+\w+\s*=\s*await\s+Bash\(\{\s*command:\s*("(?:[^"\\]|\\.)*")'
    r'(?:\s*,\s*"?description"?:\s*"(?:[^"\\]|\\.)*")?\s*\}\);?\s*'
    r'return\s+\w+\.(?:stdout|stderr|exitCode|output);?$', re.S)


def repl_command(code):
    """Return the single wrapped Bash command, or None when the cell does more."""
    if not isinstance(code, str):
        return None
    match = REPL_BASH.match(code.strip())
    if not match:
        return None
    try:
        command = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return command if isinstance(command, str) and command else None


def decide(tool, tool_input, ctx=None):
    """Public entry: tool name + input -> {decision, rule, reason}."""
    ctx = ctx or Ctx()
    name = str(tool or "").strip()
    key = name.lower()
    data = tool_input if isinstance(tool_input, dict) else {}

    if key.startswith("mcp__"):
        return hold("mcp.any", "MCP 工具不在编排白名单内")
    if key in ("askuserquestion", "ask_user_question"):
        return hold("tool.ask-user", "用户决定类提问必须由人回答，守护不代答")
    if key == "bash":
        return decide_bash(data.get("command") or data.get("cmd") or "", ctx,
                           data.get("cwd") or ctx["workspace"])
    if key in ("repl", "code_execute", "codeexec", "run_code"):
        inner = repl_command(data.get("code") or "")
        if inner is None:
            return hold("repl.unverifiable", "REPL 单元不是单条 Bash 调用，无法静态确认")
        return decide("Bash", {"command": inner}, ctx)
    if key in ("write", "edit", "multiedit", "notebookedit", "notebook_write"):
        path = data.get("file_path") or data.get("notebook_path") or data.get("path") or ""
        if not path:
            return hold("write.no-path", "写入缺少目标路径")
        if sensitive(_real(path, ctx["workspace"])):
            return hold("path.sensitive", f"写入敏感路径：{path}")
        if not inside(_real(path, ctx["workspace"]), ctx["workspace"]):
            return hold("path.outside", f"写入工作区之外：{path}")
        return allow(f"tool.{key}")
    if key == "read":
        path = data.get("file_path") or data.get("path") or ""
        if path and sensitive(_real(path, ctx["workspace"])):
            return hold("path.sensitive", f"读取敏感路径：{path}")
        return allow("tool.read")
    if key in TOOL_ALLOW:
        return allow(TOOL_ALLOW[key])
    return hold("tool.unknown", f"未纳入白名单的工具：{name or '(空)'}")


# ---------------------------------------------------------------- CLI

def load_extra(path):
    if not path:
        return []
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"无法读取 extra-allow 配置：{exc}")
    values = data.get("extra_allow") if isinstance(data, dict) else data
    return [str(v) for v in values or []]


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workspace", default="", help="worktree root the run may touch")
    p.add_argument("--branch", default="")
    p.add_argument("--extra-allow", default="", help="JSON file with regexes allowed verbatim")
    p.add_argument("--run-dir", action="append", default=[], help="run-owned scratch dir")
    p.add_argument("--skill-dir", action="append", default=[], help="skill dirs whose scripts may run")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="classify one tool call")
    c.add_argument("--tool", required=True)
    c.add_argument("--command", default="", help="Bash command when --tool Bash")
    c.add_argument("--file", default="", help="path argument for Read/Write/Edit")
    h = sub.add_parser("hook", help="PreToolUse entry: read the hook payload on stdin")
    h.add_argument("--armed", default="", help="only decide when this marker file exists")
    return p


def cmd_check(a, ctx):
    data = {}
    if a.command:
        data["command"] = a.command
    if a.file:
        data["file_path"] = a.file
    return decide(a.tool, data, ctx)


def cmd_hook(a, ctx):
    if a.armed and not Path(a.armed).exists():
        return None
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return decide(payload.get("tool_name") or payload.get("tool"), payload.get("tool_input") or {}, ctx)


def main(argv=None):
    a = build_parser().parse_args(argv)
    ctx = Ctx(a.workspace or os.getcwd(), a.branch, load_extra(a.extra_allow), a.run_dir, a.skill_dir)
    if a.cmd == "check":
        decision = cmd_check(a, ctx)
        print(json.dumps(decision, ensure_ascii=False))
        return 0
    decision = cmd_hook(a, ctx)
    if decision is None or decision["decision"] != "allow":
        print("{}")
        return 0
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": f"orch-guard: {decision['rule']}",
    }}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
