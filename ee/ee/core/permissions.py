"""权限：三档模式 + deny → 强制询问 → ask → allow 规则 + 命令拆分逐段判 + 会话内“以后都行”。

模式：
  read-only  只读：只能读和搜（含只读 shell 命令）。
  default    默认：工作区内读写免问；只读白名单命令免问，其他命令问；工作区外写、联网命令问。
  auto       全自动：都不问，但 deny 规则照拦，危险命令（强制询问类）仍会问。
  plan       计划：权限同只读；模型调研后用 present_plan 提交方案，用户批准后切到默认/全自动执行。

危险命令规则对 bash 和 PowerShell（以及 cmd）两套语法都生效。
"""
from __future__ import annotations

import fnmatch
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

MODES = ["read-only", "default", "auto", "plan"]
MODE_LABEL = {"read-only": "只读", "default": "默认", "auto": "全自动", "plan": "计划"}
READONLY_MODES = ("read-only", "plan")

READ_TOOLS = {"read", "grep", "glob", "todo", "present_plan", "ask_user", "skill", "repo_map"}
WRITE_TOOLS = {"write", "edit", "apply_patch"}
NET_TOOLS = {"web_search", "web_fetch"}


@dataclass
class Decision:
    action: str               # allow | ask | deny
    reason: str = ""
    prefix: str = ""          # “以后都不问”要记住的前缀
    force: bool = False       # 强制询问（全自动模式下也问）


# ------------------------------------------------------------------ 命令拆分

_SEPARATORS = ("&&", "||", "|", ";", "&", "\n")


def split_command(cmd: str) -> list[str]:
    """按 | || && ; & 换行 拆段，并把 $(...)、`...`、PowerShell 的 $(...) 里的子命令也拆出来。尊重引号。"""
    segs: list[str] = []
    subs: list[str] = []
    buf: list[str] = []
    i, n = 0, len(cmd)
    quote: str | None = None
    while i < n:
        c = cmd[i]
        if quote:
            buf.append(c)
            if c == quote:
                quote = None
            elif c == "\\" and quote == '"' and i + 1 < n:
                buf.append(cmd[i + 1]); i += 1
            elif quote == '"' and cmd.startswith("$(", i):
                j = _match_paren(cmd, i + 1)
                subs.append(cmd[i + 2:j]); buf.append(cmd[i + 1:j + 1]); i = j
            i += 1
            continue
        if c in ("'", '"'):
            quote = c; buf.append(c); i += 1; continue
        if cmd.startswith("$(", i):
            j = _match_paren(cmd, i + 1)
            subs.append(cmd[i + 2:j]); buf.append("$(…)"); i = j + 1; continue
        if c == "`" and "`" in cmd[i + 1:]:
            j = cmd.index("`", i + 1)
            inner = cmd[i + 1:j]
            # PowerShell 用 ` 做转义字符（单个字符），bash 用 `cmd` 做命令替换
            if len(inner) > 1:
                subs.append(inner); buf.append("`…`"); i = j + 1; continue
        if c == ">" and i + 1 < n and cmd[i + 1] == "&":  # 2>&1 之类，不是分隔符
            buf.append(">&"); i += 2; continue
        sep = next((s for s in _SEPARATORS if cmd.startswith(s, i)), None)
        if sep:
            segs.append("".join(buf)); buf = []; i += len(sep); continue
        buf.append(c); i += 1
    segs.append("".join(buf))
    out = [s.strip() for s in segs if s.strip()]
    for s in subs:
        out.extend(split_command(s))
    return out


def _match_paren(s: str, open_idx: int) -> int:
    depth = 0
    for k in range(open_idx, len(s)):
        if s[k] == "(":
            depth += 1
        elif s[k] == ")":
            depth -= 1
            if depth == 0:
                return k
    return len(s) - 1


def tokens(seg: str) -> list[str]:
    try:
        if "\\" in seg:  # Windows 路径：posix 模式会吃掉反斜杠
            t = [x.strip("'\"") for x in shlex.split(seg, posix=False)]
        else:
            t = shlex.split(seg, posix=True)
    except ValueError:
        t = seg.split()
    # 去掉前导环境变量赋值 FOO=1 cmd
    while t and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t[0]):
        t = t[1:]
    if t and t[0] in ("&", "."):  # PowerShell 调用运算符
        t = t[1:]
    return t


def _cmd_name(tok: str) -> str:
    name = tok.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for ext in (".exe", ".cmd", ".bat", ".ps1"):
        if name.endswith(ext):
            name = name[: -len(ext)]
    return name


# ------------------------------------------------------------------ 规则表

# 永远拒绝（全自动也拦）。正则对整段做匹配（忽略大小写）。
DENY_PATTERNS = [
    (r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r|--recursive\s+--force|--force\s+--recursive|-r\s+-f|-f\s+-r)[a-z]*\s+(/|/\*|~|~/|\$home|/root|/home|/usr|/etc|/bin|c:[\\/]?)\s*$", "递归强删根目录/家目录"),
    (r"\b(remove-item|rm|ri|del|erase|rd|rmdir)\b.*(-recurse|-r\b).*\s(['\"]?)(c:[\\/]?|[a-z]:[\\/]?|/|~|\$home|\$env:userprofile|\$env:systemroot)\3(\s|$)", "PowerShell 递归删除盘符根/家目录"),
    (r"\b(remove-item|rm|ri|del|erase|rd|rmdir)\s+(-path\s+|-literalpath\s+)?(['\"]?)([a-z]:[\\/]?|/|~|\$home|\$env:userprofile|\$env:systemroot)\3\s.*-recurse", "PowerShell 递归删除盘符根/家目录"),
    (r"\b(del|erase)\b.*\s/s\b.*\s[a-z]:\\?\s*$", "cmd 递归删除整个盘"),
    (r"\b(rd|rmdir)\b.*\s/s\b.*\s[a-z]:\\?\s*$", "cmd 删除整个盘"),
    (r"\bmkfs(\.\w+)?\b", "格式化文件系统"),
    (r"\bformat(-volume)?\s+[a-z]:", "格式化磁盘"),
    (r"\bdd\b.*\bof=/dev/(sd|nvme|hd|disk)", "直接写块设备"),
    (r":\(\)\s*\{\s*:\|:&\s*\};:", "fork 炸弹"),
    (r"\bgit\s+push\b.*(\s--force(?!-with-lease)\b|\s-f\b|\s\+\S)", "git 强制推送"),
    (r"\b(shutdown|reboot|stop-computer|restart-computer)\b", "关机/重启"),
]

# 强制询问（全自动也问）：破坏性但有时确实需要。
FORCE_ASK_PATTERNS = [
    (r"\bgit\s+reset\b.*--hard", "git reset --hard 会丢弃未提交改动"),
    (r"\bgit\s+clean\b.*\s-[a-z]*[fdx]", "git clean 会删除未跟踪文件"),
    (r"\bgit\s+push\b.*--force-with-lease", "强制推送"),
    (r"\bgit\s+(checkout|restore)\b.*\s(--\s|\.\s*$|--worktree)", "丢弃工作区改动"),
    (r"\bgit\s+branch\b.*\s-D\b", "强制删除分支"),
    (r"\bgit\s+stash\s+(drop|clear)\b", "删除 stash"),
    (r"\brm\s+.*(-[a-z]*r|--recursive)", "递归删除"),
    (r"\b(remove-item|ri)\b.*-recurse", "PowerShell 递归删除"),
    (r"\b(rm|del|rmdir|rd)\b.*-recurse", "PowerShell 递归删除"),
    (r"\b(del|erase)\b.*\s/[sq]\b", "cmd 批量删除"),
    (r"\b(rd|rmdir)\b.*\s/s\b", "cmd 递归删除目录"),
    (r"\bsudo\b|\brunas\b|start-process\b.*-verb\s+runas", "提权运行"),
    (r"\bchmod\s+-r\b|\bchown\s+-r\b", "递归改权限"),
    (r"\b(set-executionpolicy|reg\s+(add|delete)|bcdedit|diskpart)\b", "修改系统设置"),
]

# 只读白名单（默认模式免问）。key=命令名，value=None 表示任何参数都行，否则为允许的子命令集合。
SAFE_COMMANDS: dict[str, set[str] | None] = {
    # posix
    "ls": None, "cat": None, "head": None, "tail": None, "wc": None, "pwd": None, "echo": None, "printf": None,
    "rg": None, "grep": None, "egrep": None, "which": None, "whoami": None, "tree": None, "stat": None,
    "file": None, "du": None, "df": None, "date": None, "uname": None, "basename": None, "dirname": None,
    "realpath": None, "sort": None, "uniq": None, "cut": None, "diff": None, "true": None, "type": None,
    "less": None, "nl": None, "jq": None, "md5sum": None, "sha256sum": None, "cmp": None, "column": None,
    # windows cmd
    "dir": None, "where": None, "findstr": None, "ver": None, "hostname": None,
    # PowerShell（小写比较）
    "get-childitem": None, "gci": None, "get-content": None, "gc": None, "get-location": None, "gl": None,
    "select-string": None, "sls": None, "get-item": None, "gi": None, "test-path": None, "write-output": None,
    "write-host": None, "get-command": None, "gcm": None, "measure-object": None, "measure": None,
    "resolve-path": None, "get-date": None, "select-object": None, "select": None, "where-object": None,
    "foreach-object": None, "sort-object": None, "format-table": None, "ft": None, "format-list": None,
    "fl": None, "out-string": None, "get-process": None, "get-filehash": None, "split-path": None,
    "join-path": None, "get-itemproperty": None, "compare-object": None, "group-object": None,
    "convertto-json": None, "convertfrom-json": None, "get-help": None, "get-history": None,
    # 测试/版本查询
    "pytest": None, "py.test": None,
    # 只读 git / gh 子命令
    "git": {"status", "diff", "log", "show", "branch", "blame", "rev-parse", "ls-files", "remote", "describe",
            "shortlog", "reflog", "grep", "config", "tag", "ls-tree", "cat-file", "merge-base", "rev-list",
            "show-ref", "whatchanged", "version", "help", "worktree", "stash"},
    "gh": {"pr", "issue", "repo", "run", "status", "release", "workflow", "search", "auth", "browse"},
}

# git 子命令里还要看参数才能算只读的
_GIT_READONLY_CHECK = {
    "branch": lambda a: not any(x in a for x in ("-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move",
                                                  "--copy", "--set-upstream-to", "-u", "--unset-upstream"))
                        and all(x.startswith("-") for x in a),
    "remote": lambda a: not a or a in (["-v"], ["show"]) or (a[:1] == ["show"]) or a == ["get-url", "origin"],
    "config": lambda a: any(x in a for x in ("--get", "--list", "-l", "--get-all", "--get-regexp")),
    "tag": lambda a: not a or a[0] in ("-l", "--list", "--contains", "--points-at"),
    "worktree": lambda a: a[:1] == ["list"],
    "stash": lambda a: a[:1] in (["list"], ["show"]),
}
_GH_READONLY_VERBS = {"view", "list", "status", "diff", "checks", "ls", "search", "watch"}
_VERSION_FLAGS = {"--version", "-v", "-V", "version", "--help", "-h"}

NETWORK_COMMANDS = {"curl", "wget", "invoke-webrequest", "iwr", "invoke-restmethod", "irm", "ssh", "scp",
                    "nc", "ncat", "telnet", "ftp", "rsync"}


def _segment_safe(seg: str) -> bool:
    if re.search(r"(^|[^0-9&])>{1,2}(?!&)", seg) and not re.search(r">\s*(/dev/null|\$null|nul)\b", seg, re.I):
        return False  # 重定向写文件
    t = tokens(seg)
    if not t:
        return True
    name = _cmd_name(t[0])
    args = t[1:]
    if name in ("python", "python3", "py", "node", "npm", "pnpm", "yarn", "go", "cargo", "java", "dotnet",
                "pip", "uv", "rustc", "gcc", "make", "cmake", "deno", "bun", "ruby", "php", "pwsh", "powershell"):
        if args and args[0] in _VERSION_FLAGS:
            return True
        if name in ("python", "python3", "py") and args[:2] == ["-m", "pytest"]:
            return True
        return False
    if name == "find":
        return not any(a in ("-exec", "-execdir", "-delete", "-ok", "-fprint") for a in args)
    if name == "sed":
        return "-i" not in args and not any(a.startswith("-i") for a in args)
    if name not in SAFE_COMMANDS:
        return False
    subs = SAFE_COMMANDS[name]
    if subs is None:
        return True
    # git / gh：跳过全局选项（git -C dir --no-pager status）
    rest = list(args)
    while rest and rest[0].startswith("-"):
        if rest[0] in ("-C", "-c") and len(rest) > 1:
            rest = rest[2:]
        else:
            rest = rest[1:]
    if not rest:
        return rest == [] and bool(args) and all(a in _VERSION_FLAGS for a in args)
    sub = rest[0]
    if name == "git":
        if sub not in subs:
            return False
        chk = _GIT_READONLY_CHECK.get(sub)
        return chk(rest[1:]) if chk else True
    if name == "gh":
        if sub not in subs:
            return False
        if sub in ("status", "browse"):
            return sub == "status"
        if sub == "auth":
            return rest[1:2] == ["status"]
        if sub == "search":
            return True
        verb = rest[1] if len(rest) > 1 else ""
        return verb in _GH_READONLY_VERBS
    return True


def command_prefix(seg: str) -> str:
    """用于“以后 `<前缀>` 开头的命令都不问”：命令名 + 子命令（如 git commit、npm run）。"""
    t = tokens(seg)
    if not t:
        return seg.strip()
    name = t[0]
    if len(t) > 1 and not t[1].startswith("-") and _cmd_name(name) in (
            "git", "gh", "npm", "pnpm", "yarn", "cargo", "go", "docker", "kubectl", "pip", "uv", "dotnet", "make"):
        return f"{name} {t[1]}"
    if _cmd_name(name) in ("python", "python3", "py") and len(t) > 2 and t[1] == "-m":
        return f"{name} -m {t[2]}"
    return name


def _match_rule(rule: str, tool: str, subject: str) -> bool:
    """规则形如 shell(git push *)、write(**/*.env)、edit、shell。"""
    m = re.match(r"^\s*([\w\-]+)\s*(?:\((.*)\))?\s*$", rule)
    if not m:
        return False
    rtool, pat = m.group(1).lower(), m.group(2)
    if rtool == "bash":
        rtool = "shell"
    if rtool != tool:
        return False
    if pat is None or pat.strip() in ("", "*"):
        return True
    return fnmatch.fnmatch(subject.lower().replace("\\", "/"), pat.strip().lower().replace("\\", "/"))


# ------------------------------------------------------------------ 判定

@dataclass
class PermissionPolicy:
    mode: str = "default"
    workspace: Path = field(default_factory=Path.cwd)
    allow: list[str] = field(default_factory=list)
    ask: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    session_prefixes: set[str] = field(default_factory=set)      # 会话内“以后都行”的命令前缀
    session_tools: set[str] = field(default_factory=set)         # 会话内“以后都行”的写目录等

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"未知权限模式：{mode}")
        self.mode = mode

    def remember(self, d: Decision, tool: str) -> None:
        if tool == "shell" and d.prefix:
            self.session_prefixes.add(d.prefix)
        elif d.prefix:
            self.session_tools.add(f"{tool}:{d.prefix}")

    def _eeignored(self, tool: str, args: dict) -> str | None:
        """.eeignore 命中的路径（D4-8）；read/write/edit/apply_patch 碰到一律拒绝。"""
        if tool not in ("read", "write", "edit", "apply_patch"):
            return None
        try:
            from ee.core.eeignore import for_dir
            ig = for_dir(self.workspace)
        except Exception:  # noqa: BLE001
            return None
        if ig.empty:
            return None
        for p in (self._paths(tool, args) if tool != "read" else [str(args.get("path") or "")]):
            if not p:
                continue
            ap = Path(p.strip().strip('"').strip("'")).expanduser()
            if not ap.is_absolute():
                ap = self.workspace / ap
            if ig.ignored(ap):
                return p
        return None

    def check(self, tool: str, args: dict, info: dict | None = None) -> Decision:
        hit = self._eeignored(tool, args)
        if hit is not None:
            return Decision("deny", f"{hit} 被项目的 .eeignore 排除，ee 不能读写它（也不要用 shell 绕过）")
        if tool.startswith("mcp__"):
            return self._check_mcp(tool, args, info or {})
        if tool == "shell":
            return self._check_shell(args.get("command") or "")
        if tool in WRITE_TOOLS:
            return self._check_write(tool, args)
        if tool in READ_TOOLS:
            subject = str(args.get("path") or args.get("pattern") or args.get("name") or "")
            if any(_match_rule(r, tool, subject) for r in self.deny):
                return Decision("deny", "命中 deny 规则")
            return Decision("allow")
        if tool in ("shell_output", "shell_kill"):
            # 只能看 / 结束 ee 自己启动的后台命令（D4-6）
            return Decision("allow")
        if tool == "shell_write":
            # 往已经批准启动的后台命令里输入；只读 / 计划模式下不允许（可能让交互程序做写操作）
            if self.mode in READONLY_MODES:
                return Decision("deny", "当前是只读模式，不能向后台命令输入")
            if any(_match_rule(r, tool, "") for r in self.deny):
                return Decision("deny", "命中 deny 规则")
            return Decision("allow")
        if tool in NET_TOOLS or tool == "task":
            # 网页搜索/抓取是只读的（只读模式“只能读和搜”也允许）；deny/ask 规则按 url / query 匹配。
            # task 本身放行：子代理里的每个工具调用各自走权限判断（主会话只读时子代理强制只读）。
            subject = str(args.get("url") or args.get("query") or args.get("description") or "")
            if any(_match_rule(r, tool, subject) for r in self.deny):
                return Decision("deny", "命中 deny 规则")
            if self.mode != "auto" and any(_match_rule(r, tool, subject) for r in self.ask):
                return Decision("ask", "命中 ask 规则", prefix=tool)
            return Decision("allow")
        if tool == "memory":
            if args.get("action") in (None, "", "list"):
                return Decision("allow")
            if self.mode in READONLY_MODES:
                return Decision("deny", "当前是只读模式，不能修改记忆")
            if any(_match_rule(r, tool, "") for r in self.deny):
                return Decision("deny", "命中 deny 规则")
            return Decision("allow")
        # 未知/扩展工具：只读模式拒绝，默认模式问（“以后都行”记在 session_tools）
        if self.mode in READONLY_MODES:
            return Decision("deny", "只读模式")
        if self.mode == "auto" or any(_match_rule(r, tool, "") for r in self.allow) \
                or f"{tool}:{tool}" in self.session_tools:
            return Decision("allow")
        return Decision("ask", f"使用工具 {tool}", prefix=tool)

    # ---- MCP 工具：mcp__<服务器>__<工具>
    def _check_mcp(self, tool: str, args: dict, info: dict) -> Decision:
        import json as _json
        subject = _json.dumps(args or {}, ensure_ascii=False)
        server = tool.split("__")[1] if tool.count("__") >= 2 else ""

        def rule_hit(rules: list[str]) -> bool:
            for r in rules:
                m = re.match(r"^\s*([\w\-*]+)\s*(?:\((.*)\))?\s*$", r)
                if not m:
                    continue
                rt = m.group(1)
                if not rt.lower().startswith("mcp__") or not fnmatch.fnmatch(tool.lower(), rt.lower()):
                    continue
                pat = m.group(2)
                if pat is None or pat.strip() in ("", "*") or fnmatch.fnmatch(subject.lower(), pat.strip().lower()):
                    return True
            return False

        if rule_hit(self.deny):
            return Decision("deny", "命中 deny 规则")
        trusted = bool(info.get("trusted"))
        if self.mode in READONLY_MODES:
            if not info.get("read_only"):
                return Decision("deny", "只读模式下只能用标为只读的 MCP 工具")
            if trusted or rule_hit(self.allow):
                return Decision("allow")
            return Decision("ask", f"MCP 只读工具 {tool}", prefix=tool)
        if self.mode != "auto" and rule_hit(self.ask):
            return Decision("ask", "命中 ask 规则", prefix=tool)
        if self.mode == "auto" or trusted or rule_hit(self.allow) or f"{tool}:{tool}" in self.session_tools \
                or f"{tool}:mcp__{server}__*" in self.session_tools:
            return Decision("allow")
        return Decision("ask", f"调用 MCP 工具 {server} · {tool.split('__', 2)[-1]}", prefix=tool)

    # ---- 写文件
    def _paths(self, tool: str, args: dict) -> list[str]:
        if tool == "apply_patch":
            patch = args.get("patch") or ""
            return re.findall(r"^\*\*\* (?:Add File|Delete File|Update File|Move to): (.+)$", patch, re.M)
        return [str(args.get("path") or "")]

    def _check_write(self, tool: str, args: dict) -> Decision:
        paths = self._paths(tool, args)
        for p in paths:
            if any(_match_rule(r, tool, p) or _match_rule(r, "write", p) for r in self.deny):
                return Decision("deny", f"命中 deny 规则：{p}")
        if self.mode in READONLY_MODES:
            return Decision("deny", "当前是只读/计划模式，不能修改文件")
        if any(_match_rule(r, tool, p) for r in self.ask for p in paths):
            return Decision("ask", "命中 ask 规则", prefix="")
        if self.mode == "auto":
            return Decision("allow")
        ws = self.workspace.resolve()
        for p in paths:
            ap = Path(p).expanduser()
            if not ap.is_absolute():
                ap = ws / ap
            try:
                ap = ap.resolve()
            except OSError:
                pass
            inside = ap == ws or ws in ap.parents
            if not inside:
                d = str(ap.parent)
                if f"{tool}:{d}" in self.session_tools or f"write:{d}" in self.session_tools:
                    continue
                if any(_match_rule(r, tool, str(ap)) for r in self.allow):
                    continue
                return Decision("ask", f"写工作区外的文件：{ap}", prefix=d)
            if ".git" in ap.relative_to(ws).parts:
                return Decision("ask", f"修改 .git 内部文件：{ap}", prefix="")
        return Decision("allow")

    # ---- shell
    def _check_shell(self, command: str) -> Decision:
        if not command.strip():
            return Decision("allow")
        segs = split_command(command)
        low_full = command.lower()
        for seg in segs + [command]:
            low = seg.lower()
            for pat, why in DENY_PATTERNS:
                if re.search(pat, low, re.I):
                    return Decision("deny", f"危险命令已拦截（{why}）：{seg}")
            if any(_match_rule(r, "shell", seg) for r in self.deny):
                return Decision("deny", f"命中 deny 规则：{seg}")
        for seg in segs:
            for pat, why in FORCE_ASK_PATTERNS:
                if re.search(pat, seg, re.I):
                    if self.mode in READONLY_MODES:
                        return Decision("deny", f"只读模式不能执行：{seg}")
                    return Decision("ask", why, prefix="", force=True)
        unsafe = [s for s in segs if not _segment_safe(s)]
        if self.mode in READONLY_MODES:
            if unsafe:
                return Decision("deny", f"只读模式只能运行只读命令，被拦截：{unsafe[0]}")
            return Decision("allow")
        if any(_match_rule(r, "shell", s) for r in self.ask for s in segs):
            return Decision("ask", "命中 ask 规则", prefix=command_prefix(segs[0]))
        if self.mode == "auto":
            return Decision("allow")
        remaining = []
        for s in unsafe:
            # “shell(npm run *)” 也要命中不带参数的 “npm run”
            if any(_match_rule(r, "shell", s) or _match_rule(r, "shell", s + " ") for r in self.allow):
                continue
            pre = command_prefix(s)
            if pre in self.session_prefixes or any(s.startswith(p + " ") or s == p for p in self.session_prefixes):
                continue
            remaining.append(s)
        if not remaining:
            return Decision("allow")
        first = remaining[0]
        name = _cmd_name(tokens(first)[0]) if tokens(first) else ""
        why = "联网命令" if name in NETWORK_COMMANDS else "需要确认的命令"
        _ = low_full
        return Decision("ask", why, prefix=command_prefix(first))
