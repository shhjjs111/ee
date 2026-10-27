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
import os
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

# 三套语法：posix（bash / git-bash / sh）、powershell（pwsh / Windows PowerShell）、cmd。
# 拆分按 shell 工具实际用的 shell 来（ShellInfo.syntax）；不知道时三套都拆一遍、取最严。
SYNTAXES = ("posix", "powershell", "cmd")
_PS_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
                            "\u201c": '"', "\u201d": '"', "\u201e": '"'})   # PowerShell 把弯引号也当引号


def split_command(cmd: str, syntax: str = "posix") -> list[str]:
    r"""按实际 shell 的语法拆段，并把子命令（$(…)、`…`、PowerShell 的 (…)、{…} 等）也拆出来。尊重引号和转义。

    posix：| || && ; & 换行 分隔；'…' 原样，"…" 里 \ 转义、$(…) 和 `…` 照样执行；引号外 \ 转义下一个字符。
    powershell：; | && || 换行 分隔，非行首的 & 是 PS7 后台运算符也算分隔；'…' 里 '' 是单引号，
                "…" 里反引号转义、$(…) 执行；引号外反引号转义下一个字符；(…)、$(…)、@(…)、{…}、@{…} 里都会执行命令。
    cmd：& && | || 换行 分隔（; 不是分隔符）；只有 "…" 是引号（' 是普通字符）；引号外 ^ 转义下一个字符。
    """
    if syntax == "powershell":
        segs, subs = _split_ps(cmd.translate(_PS_QUOTES))
    elif syntax == "cmd":
        segs, subs = _split_cmd(cmd)
    else:
        segs, subs = _split_posix(cmd)
    out = [s.strip() for s in segs if s.strip()]
    for sub in subs:
        out.extend(split_command(sub, syntax))
    return out


def _match_close(s: str, open_idx: int, syntax: str = "posix") -> int:
    """s[open_idx] 是 ( 或 {，返回配对的右括号位置（考虑引号和转义）；不配对就返回 len(s)。"""
    open_c = s[open_idx]
    close_c = {"(": ")", "{": "}"}[open_c]
    esc = {"posix": "\\", "powershell": "`", "cmd": "^"}.get(syntax, "\\")
    quotes = ('"',) if syntax == "cmd" else ("'", '"')
    depth, quote, k, n = 0, None, open_idx, len(s)
    while k < n:
        c = s[k]
        if quote:
            if c == esc and quote == '"' and syntax != "cmd":
                k += 2
                continue
            if c == quote:
                quote = None
        elif c == esc:
            k += 2
            continue
        elif c in quotes:
            quote = c
        elif c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return k
        k += 1
    return n


def _find_backtick(s: str, i: int) -> int | None:
    """posix：从 i 起找下一个未转义的反引号。"""
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == "`":
            return i
        i += 1
    return None


_POSIX_SEPS = ("&&", "||", "|", ";", "&", "\n")


def _split_posix(cmd: str) -> tuple[list[str], list[str]]:
    segs: list[str] = []
    subs: list[str] = []
    buf: list[str] = []
    i, n = 0, len(cmd)
    quote: str | None = None
    while i < n:
        c = cmd[i]
        if quote == "'":
            buf.append(c)
            if c == "'":
                quote = None
            i += 1
            continue
        if quote == '"':
            if c == "\\" and i + 1 < n:
                buf.append(cmd[i:i + 2]); i += 2; continue
            if cmd.startswith("$(", i):
                j = _match_close(cmd, i + 1)
                subs.append(cmd[i + 2:j]); buf.append(cmd[i:j + 1]); i = j + 1; continue
            if c == "`":
                j = _find_backtick(cmd, i + 1)
                if j is not None:
                    subs.append(cmd[i + 1:j]); buf.append(cmd[i:j + 1]); i = j + 1; continue
            buf.append(c)
            if c == '"':
                quote = None
            i += 1
            continue
        if c == "\\" and i + 1 < n:          # 引号外 \ 转义下一个字符（\; 不是分隔符）
            buf.append(cmd[i:i + 2]); i += 2; continue
        if c in ("'", '"'):
            quote = c; buf.append(c); i += 1; continue
        if cmd.startswith(("$(", "<(", ">("), i):   # 命令替换 / 进程替换
            j = _match_close(cmd, i + 1)
            subs.append(cmd[i + 2:j]); buf.append(c + "(…)"); i = j + 1; continue
        if c == "`":
            j = _find_backtick(cmd, i + 1)
            if j is not None:
                subs.append(cmd[i + 1:j]); buf.append("`…`"); i = j + 1; continue
        if cmd.startswith((">&", "<&"), i):  # 2>&1 之类，不是分隔符
            buf.append(cmd[i:i + 2]); i += 2; continue
        sep = next((x for x in _POSIX_SEPS if cmd.startswith(x, i)), None)
        if sep:
            segs.append("".join(buf)); buf = []; i += len(sep); continue
        buf.append(c); i += 1
    segs.append("".join(buf))
    return segs, subs


def _split_ps(cmd: str) -> tuple[list[str], list[str]]:
    segs: list[str] = []
    subs: list[str] = []
    buf: list[str] = []
    i, n = 0, len(cmd)
    quote: str | None = None

    def at_token_start() -> bool:
        j = len(buf) - 1
        prev = buf[j][-1] if buf and buf[j] else ""
        return prev == "" or prev.isspace() or prev in "|;(&={,"

    while i < n:
        c = cmd[i]
        if quote == "'":
            buf.append(c)
            if c == "'":
                if cmd.startswith("''", i):
                    buf.append("'"); i += 2; continue
                quote = None
            i += 1
            continue
        if quote == '"':
            if c == "`" and i + 1 < n:
                buf.append(cmd[i:i + 2]); i += 2; continue
            if cmd.startswith("$(", i):
                j = _match_close(cmd, i + 1, "powershell")
                subs.append(cmd[i + 2:j]); buf.append(cmd[i:j + 1]); i = j + 1; continue
            buf.append(c)
            if c == '"':
                if cmd.startswith('""', i):
                    buf.append('"'); i += 2; continue
                quote = None
            i += 1
            continue
        if cmd.startswith(("@'", '@"'), i):     # here-string：到行首的 '@ / "@ 为止
            q = cmd[i + 1]
            end = cmd.find("\n" + q + "@", i + 2)
            end = n if end < 0 else end + 3
            body = cmd[i:end]
            if q == '"':                         # 可展开的 here-string 里 $(…) 照样执行
                k = 0
                while True:
                    k = body.find("$(", k)
                    if k < 0:
                        break
                    j = _match_close(body, k + 1, "powershell")
                    subs.append(body[k + 2:j]); k = j + 1
            buf.append(body); i = end; continue
        if c == "`":
            if i + 1 < n:
                buf.append(cmd[i:i + 2]); i += 2; continue
            i += 1; continue
        if c in ("'", '"'):
            quote = c; buf.append(c); i += 1; continue
        if cmd.startswith(("$(", "@("), i):
            j = _match_close(cmd, i + 1, "powershell")
            subs.append(cmd[i + 2:j]); buf.append(c + "(…)"); i = j + 1; continue
        if c == "(":
            j = _match_close(cmd, i, "powershell")
            subs.append(cmd[i + 1:j]); buf.append("(…)"); i = j + 1; continue
        if cmd.startswith("@{", i) and at_token_start():
            j = _match_close(cmd, i + 1, "powershell")
            subs.append(cmd[i + 2:j]); buf.append("@{…}"); i = j + 1; continue
        if c == "{" and at_token_start():
            j = _match_close(cmd, i, "powershell")
            subs.append(cmd[i + 1:j]); buf.append("{…}"); i = j + 1; continue
        if cmd.startswith((">&", "<&"), i):
            buf.append(cmd[i:i + 2]); i += 2; continue
        if cmd.startswith(("&&", "||"), i):
            segs.append("".join(buf)); buf = []; i += 2; continue
        if c == "&":
            if "".join(buf).strip() == "":      # 行首：调用运算符
                buf.append(c); i += 1; continue
            segs.append("".join(buf)); buf = []; i += 1; continue   # PS7 后台运算符
        if c in ";|\n":
            segs.append("".join(buf)); buf = []; i += 1; continue
        if c == "\r":
            i += 1; continue
        buf.append(c); i += 1
    segs.append("".join(buf))
    return segs, subs


_CMD_SEPS = ("&&", "||", "|", "&", "\n")


def _split_cmd(cmd: str) -> tuple[list[str], list[str]]:
    segs: list[str] = []
    buf: list[str] = []
    i, n = 0, len(cmd)
    quote = False
    while i < n:
        c = cmd[i]
        if quote:
            buf.append(c)
            if c == '"':
                quote = False
            i += 1
            continue
        if c == "^" and i + 1 < n:            # ^ 转义下一个字符（^& 是字面量 &）
            buf.append(cmd[i:i + 2]); i += 2; continue
        if c == '"':
            quote = True; buf.append(c); i += 1; continue
        if cmd.startswith((">&", "<&"), i):
            buf.append(cmd[i:i + 2]); i += 2; continue
        sep = next((x for x in _CMD_SEPS if cmd.startswith(x, i)), None)
        if sep:
            segs.append("".join(buf)); buf = []; i += len(sep); continue
        if c == "\r":
            i += 1; continue
        buf.append(c); i += 1
    segs.append("".join(buf))
    return segs, []


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


def words(seg: str, syntax: str = "posix") -> list[str] | None:
    """按 shell 语法把一段拆成词（去掉引号和转义）。引号不配对等解析不了时返回 None。"""
    if syntax == "powershell":
        return _ps_words(seg.translate(_PS_QUOTES))
    if syntax == "cmd":
        return _cmd_words(seg)
    try:
        return shlex.split(seg, posix=True)
    except ValueError:
        return None


def _ps_words(seg: str) -> list[str] | None:
    out: list[str] = []
    cur: list[str] = []
    have = False
    quote: str | None = None
    i, n = 0, len(seg)
    while i < n:
        c = seg[i]
        if quote == "'":
            if c == "'":
                if seg.startswith("''", i):
                    cur.append("'"); i += 2; continue
                quote = None
            else:
                cur.append(c)
            i += 1
            continue
        if quote == '"':
            if c == "`" and i + 1 < n:
                cur.append(seg[i + 1]); i += 2; continue
            if c == '"':
                if seg.startswith('""', i):
                    cur.append('"'); i += 2; continue
                quote = None
            else:
                cur.append(c)
            i += 1
            continue
        if c == "`":
            if i + 1 < n and seg[i + 1] not in "\r\n":
                cur.append(seg[i + 1]); have = True
            i += 2
            continue
        if c in ("'", '"'):
            quote = c; have = True; i += 1; continue
        if c.isspace():
            if have or cur:
                out.append("".join(cur)); cur = []; have = False
            i += 1
            continue
        cur.append(c); i += 1
    if quote:
        return None
    if have or cur:
        out.append("".join(cur))
    # PowerShell 把 – — ― 也当参数前缀的 -
    return [re.sub(r"^[\u2013\u2014\u2015]", "-", w) for w in out]


def _cmd_words(seg: str) -> list[str]:
    out: list[str] = []
    cur: list[str] = []
    have = False
    quote = False
    i, n = 0, len(seg)
    while i < n:
        c = seg[i]
        if quote:
            if c == '"':
                quote = False
            else:
                cur.append(c)
            i += 1
            continue
        if c == "^":
            if i + 1 < n:
                cur.append(seg[i + 1]); have = True
            i += 2
            continue
        if c == '"':
            quote = True; have = True; i += 1; continue
        if c.isspace():
            if have or cur:
                out.append("".join(cur)); cur = []; have = False
            i += 1
            continue
        cur.append(c); i += 1
    if have or cur:
        out.append("".join(cur))
    return _cmd_builtin_args(out)


# cmd 内部命令：命令名后可以直接跟 / , ; =（rd/s/q、del,/q,x 都能执行），参数也按 , ; = 分隔
_CMD_BUILTINS = {"rd", "rmdir", "del", "erase", "dir", "copy", "move", "md", "mkdir", "type", "echo", "ren",
                 "rename", "cd", "chdir", "set", "start", "call", "pushd", "popd", "cls", "ver", "vol", "path",
                 "mklink", "assoc", "ftype", "for", "if", "goto", "shift", "title", "color", "date", "time",
                 "break", "verify", "prompt", "setlocal", "endlocal", "exit", "rem", "pause"}


def _cmd_builtin_args(w: list[str]) -> list[str]:
    if not w:
        return w
    first = w[0].lstrip("@(")
    m = re.match(r"^([^,;=/\\]+)([,;=/].*)$", first)
    head = (m.group(1) if m else first).lower()
    if head not in _CMD_BUILTINS:
        return w
    rest = ([m.group(2)] if m else []) + w[1:]
    out = [w[0][: len(w[0]) - len(first)] + (m.group(1) if m else first)]
    for a in rest:
        for part in re.split(r"[,;=]", a):
            # /s/q 连写 → /s /q
            out.extend(re.findall(r"/[^/]*", part) if re.fullmatch(r"(/[A-Za-z?:\-]+)+", part) else [part])
    return [x for x in out if x != ""]


def _cmd_name(tok: str) -> str:
    name = tok.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for ext in (".exe", ".cmd", ".bat", ".ps1"):
        if name.endswith(ext):
            name = name[: -len(ext)]
    return name


# ------------------------------------------------------------------ 归一（判危险命令用）

@dataclass
class _Seg:
    raw: str              # 原文
    syntax: str
    norm: str = ""        # 归一后的命令：命令名小写去路径/扩展名、去引号转义、去包装前缀和 git 全局选项、PS 参数缩写展开
    dynamic: str = ""     # 非空：无法确定要执行什么（原因）


_PREFIX_WRAPPERS = {"command", "builtin", "exec", "nohup", "nice", "env", "sudo", "doas", "xargs", "timeout",
                    "stdbuf", "time", "call"}
_XARGS_VALUE_OPTS = {"-I", "-L", "-n", "-P", "-d", "-E", "-e", "-s", "-a", "--delimiter", "--max-args",
                     "--max-procs", "--arg-file", "--replace", "--max-lines", "--eof", "--max-chars"}
_SUDO_VALUE_OPTS = {"-u", "-g", "-U", "-C", "-p", "-r", "-t", "-h", "-D", "-R"}
_KEYWORDS = {"{", "}", "(", ")", "!", "if", "then", "else", "elif", "do", "while", "until", "fi", "done"}
_GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--super-prefix"}
_PS_REMOVE = {"remove-item", "ri", "rm", "del", "erase", "rd", "rmdir"}
_PS_REMOVE_PARAMS = ("path", "literalpath", "filter", "include", "exclude", "recurse", "force", "credential",
                     "whatif", "confirm", "stream", "pspath", "lp")
_PS_VALUE_OPTS = ("executionpolicy", "ep", "ex", "workingdirectory", "wd", "windowstyle", "configurationname",
                  "inputformat", "outputformat", "settingsfile", "custompipename", "psconsolefile", "version")
_MAX_DEPTH = 4


def _analyze(command: str, syntax: str, depth: int = 0) -> list[_Seg]:
    out: list[_Seg] = []
    for raw in split_command(command, syntax):
        w = words(raw, syntax)
        if w is None:      # 引号不配对：_segment_safe 已按需要确认处理；危险规则仍按原文匹配
            out.append(_Seg(raw, syntax))
            continue
        out.extend(_normalize(raw, w, syntax, depth))
    return out


def _q(x: str) -> str:
    return f'"{x}"' if any(ch.isspace() for ch in x) else x


def _strip_opts(w: list[str], value_opts: set[str] = frozenset()) -> list[str]:
    while w and w[0].startswith("-") and w[0] != "-":
        w = w[2:] if w[0] in value_opts else w[1:]
    return w


def _normalize(raw: str, w: list[str], syntax: str, depth: int) -> list[_Seg]:
    if depth > _MAX_DEPTH:
        return [_Seg(raw, syntax, "", "命令嵌套太深，无法可靠判断")]
    w = list(w)
    called = False
    while w:                                   # 去掉不改变“执行什么”的前缀
        h = w[0]
        hl = h.lower()
        if syntax == "posix" and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", h):
            w = w[1:]; continue
        if syntax == "powershell" and h.startswith("$"):
            m = re.match(r"^\$[\w:{}.\[\]]+\s*[+\-*/]?=(.*)$", h)
            if m:                              # $x = 命令 / $x=命令：右边照样执行
                w = ([m.group(1)] if m.group(1) else []) + w[1:]; continue
            if len(w) > 1 and re.fullmatch(r"[+\-*/]?=", w[1]):
                w = w[2:]; continue
        if h in ("&", ".") and syntax != "cmd":
            called = called or h == "&" or syntax == "powershell"
            w = w[1:]; continue
        if hl in _KEYWORDS and syntax != "cmd":
            w = w[1:]; continue
        stripped = h.lstrip("({@" if syntax == "cmd" else "({")
        if stripped != h:
            w = ([stripped] if stripped else []) + w[1:]; continue
        name = _cmd_name(h)
        if name in _PREFIX_WRAPPERS and (syntax == "posix" or name in ("call",) and syntax == "cmd"):
            rest = w[1:]
            if name == "env":
                while rest and (rest[0].startswith("-") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", rest[0])):
                    if rest[0] in ("-S", "--split-string") and len(rest) > 1:
                        rest = (words(rest[1], "posix") or [rest[1]]) + rest[2:]; continue
                    rest = rest[2:] if rest[0] in ("-u", "--unset", "-C", "--chdir") else rest[1:]
            elif name == "xargs":
                rest = _strip_opts(rest, _XARGS_VALUE_OPTS)
            elif name in ("sudo", "doas"):
                rest = _strip_opts(rest, _SUDO_VALUE_OPTS)
            elif name == "nice":
                rest = _strip_opts(rest, {"-n", "--adjustment"})
            elif name == "timeout":
                rest = _strip_opts(rest, {"-s", "--signal", "-k", "--kill-after"})[1:]
            elif name == "stdbuf":
                rest = _strip_opts(rest, {"-i", "-o", "-e"})
            elif name == "command":
                rest = _strip_opts(rest)
            w = rest; continue
        break
    if not w:
        return []
    name0 = w[0]
    dyn = ""
    if syntax == "posix" and ("$" in name0 or "`" in name0):
        dyn = "命令名是变量/替换，无法确定要执行什么"
    elif syntax == "powershell" and name0.startswith(("$", "(")):
        if not called:
            return [_Seg(raw, syntax)]          # 不带 & 的 $x… 是表达式，不是命令
        dyn = "调用的命令是变量/表达式，无法确定要执行什么"
    elif syntax == "cmd" and re.search(r"%[^%\s]+%", name0):
        dyn = "命令名里有 %变量%，无法确定要执行什么"
    name = _cmd_name(name0)
    args = w[1:]
    nested: list[_Seg] = []
    if name in ("bash", "sh", "zsh", "dash", "ksh", "ash"):
        for i, a in enumerate(args):
            if not a.startswith("-"):
                break
            if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", a) and i + 1 < len(args):
                nested = _analyze(args[i + 1], "posix", depth + 1)
                break
    elif name == "cmd":
        for i, a in enumerate(args):
            if re.fullmatch(r"/[ckCK]", a):
                nested = _analyze(" ".join(args[i + 1:]), "cmd", depth + 1)
                break
            if re.match(r"(?i)^/[ck]\S", a):
                nested = _analyze(" ".join([a[2:]] + args[i + 1:]), "cmd", depth + 1)
                break
    elif name in ("powershell", "pwsh"):
        i = 0
        while i < len(args):
            a = args[i]
            o = a.lower().lstrip("-/") if a[:1] in ("-", "/") else None
            if o is None:                       # 位置参数：powershell.exe 当命令，pwsh 当脚本文件
                if name == "powershell":
                    nested = _analyze(" ".join(args[i:]), "powershell", depth + 1)
                break
            if o and "command".startswith(o):
                nested = _analyze(" ".join(args[i + 1:]), "powershell", depth + 1)
                break
            if o in ("e", "ec") or (len(o) >= 2 and "encodedcommand".startswith(o)):
                try:
                    import base64
                    script = base64.b64decode(args[i + 1]).decode("utf-16-le")
                    nested = _analyze(script, "powershell", depth + 1)
                except Exception:  # noqa: BLE001
                    dyn = dyn or "无法解码 -EncodedCommand"
                break
            if o and "file".startswith(o):
                break
            i += 2 if (o in _PS_VALUE_OPTS or (len(o) >= 3 and any(x.startswith(o) for x in _PS_VALUE_OPTS))) else 1
    elif name == "eval" and syntax == "posix":
        script = " ".join(args)
        if "$" in script or "`" in script:
            dyn = dyn or "eval 的内容是变量/替换，无法确定要执行什么"
        else:
            nested = _analyze(script, "posix", depth + 1)
    elif name in ("iex", "invoke-expression"):
        script = " ".join(a for a in args if not re.fullmatch(r"(?i)-c(o(m(m(a(n(d)?)?)?)?)?)?", a))
        if not script or "$" in script or "(" in script:
            dyn = dyn or "Invoke-Expression 的内容是变量/表达式，无法确定要执行什么"
        else:
            nested = _analyze(script, "powershell", depth + 1)
    norm_words = [name] + args
    if name == "git":
        rest = list(args)
        while rest and rest[0].startswith("-"):
            o = rest[0]
            key = ""
            if o in ("-c", "--config-env") and len(rest) > 1:
                key = rest[1]
            elif o.startswith("--config-env="):
                key = o.split("=", 1)[1]
            if key.lower().startswith("alias."):
                dyn = dyn or "git -c alias.* 能把子命令换成任意命令"
            rest = rest[2:] if o in _GIT_VALUE_OPTS else rest[1:]
        if rest and rest[0] == "push":
            exp: list[str] = []
            for a in rest[1:]:
                exp.extend(f"-{ch}" for ch in a[1:]) if re.fullmatch(r"-[A-Za-z]{2,}", a) else exp.append(a)
            rest = ["push"] + exp
        norm_words = ["git"] + rest
    elif name in _PS_REMOVE and syntax == "powershell":
        exp = []
        for a in args:
            m = re.fullmatch(r"-([A-Za-z]+)(:.*)?", a)
            if m:
                o = m.group(1).lower()
                hits = [p for p in _PS_REMOVE_PARAMS if p.startswith(o)]
                if o in _PS_REMOVE_PARAMS:
                    hits = [o]
                if "recurse" in hits and (len(hits) == 1 or o == "r"):
                    a = "-recurse"
                elif "force" in hits and len(hits) == 1:
                    a = "-force"
            exp.append(a)
        norm_words = ["remove-item"] + exp
    norm = " ".join(_q(x) for x in norm_words)
    return [_Seg(raw, syntax, norm, dyn)] + nested


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
    (r"\bgit\b.*\bworktree\s+remove\b.*\s(--force|-f)\b", "强制删除 worktree 会丢弃其中未提交的改动"),
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
    # 只读 git / gh 子命令
    "git": {"status", "diff", "log", "show", "branch", "blame", "rev-parse", "ls-files", "remote", "describe",
            "shortlog", "reflog", "grep", "config", "tag", "ls-tree", "cat-file", "merge-base", "rev-list",
            "show-ref", "whatchanged", "version", "help", "worktree", "stash"},
    "gh": {"pr", "issue", "repo", "run", "status", "release", "workflow", "search", "auth", "browse"},
}

# 每个命令允许的**选项**白名单（只读模式免问的前提）。
# 方向：只放行明确认识的选项，不认识的一律拦。黑名单要穷举所有危险写法；
# 白名单只需保证"里面没有能执行程序/写文件的选项"，新增绕过就没法只用白名单组合出来。
# 缺省（命令不在表里）= 不接受任何选项。
# 长选项按 GNU 唯一前缀匹配（--all 可写 --al），由 _opt_is 处理。
SAFE_OPTS: dict[str, set[str]] = {
    "ls": {"-l", "-a", "-A", "-h", "-R", "-r", "-S", "-t", "-1", "-d", "-F", "-i", "-n", "-s", "-u",
           "--all", "--almost-all", "--long", "--human-readable", "--recursive", "--reverse", "--color",
           "--directory", "--classify", "--inode", "--numeric-uid-gid", "--size", "--time", "--sort"},
    "cat": {"-n", "-A", "-b", "-E", "-s", "-T", "-v", "-e", "-t", "--number", "--show-all",
            "--number-nonblank", "--show-ends", "--squeeze-blank"},
    "head": {"-n", "-c", "-q", "-v", "--lines", "--bytes", "--quiet", "--verbose"},
    "tail": {"-n", "-c", "-f", "-q", "-v", "-F", "--lines", "--bytes", "--follow", "--quiet", "--verbose",
             "--retry", "--sleep-interval"},
    "wc": {"-l", "-c", "-w", "-m", "-L", "--lines", "--bytes", "--words", "--chars", "--max-line-length"},
    "echo": {"-n", "-e", "-E"},
    "printf": set(),
    "grep": {"-n", "-i", "-r", "-R", "-E", "-F", "-G", "-P", "-v", "-c", "-l", "-L", "-o", "-w", "-x",
             "-A", "-B", "-C", "-H", "-h", "-s", "-q", "-e", "-f", "-a", "-I", "-z",
             "--color", "--include", "--exclude", "--exclude-dir", "--line-number", "--ignore-case",
             "--recursive", "--extended-regexp", "--fixed-strings", "--invert-match", "--count",
             "--files-with-matches", "--only-matching", "--word-regexp", "--line-regexp",
             "--after-context", "--before-context", "--context", "--with-filename", "--no-filename",
             "--no-messages", "--quiet", "--regexp", "--file"},
    "rg": {"-n", "-i", "-S", "-s", "-v", "-c", "-l", "-w", "-x", "-o", "-F", "-z", "-u", "-uu", "-uuu",
           "-A", "-B", "-C", "-e", "-f", "-g", "-t", "-T", "-j", "-M", "-m", "-p", "-E",
           "--line-number", "--ignore-case", "--smart-case", "--case-sensitive", "--invert-match",
           "--count", "--files-with-matches", "--word-regexp", "--line-regexp", "--only-matching",
           "--fixed-strings", "--hidden", "--glob", "--iglob", "--type", "--type-not", "--type-add",
           "--after-context", "--before-context", "--context", "--max-count", "--max-columns",
           "--max-depth", "--threads", "--no-ignore", "--no-ignore-vcs", "--no-ignore-parent",
           "--no-ignore-dot", "--no-ignore-global", "--unrestricted", "--color", "--json",
           "--files", "--heading", "--no-heading", "--trim", "--no-messages", "--multiline"},
    "tree": {"-a", "-d", "-f", "-L", "-I", "-P", "--ignore-case", "--charset", "--filelimit",
             "--dirsfirst", "--level", "--prune", "--noreport", "--gitignore", "--color", "--nocolor"},
    "sort": {"-n", "-r", "-u", "-f", "-b", "-h", "-V", "-k", "-t", "-s", "-z", "-m", "-c", "-C",
             "--numeric-sort", "--reverse", "--unique", "--ignore-case", "--ignore-leading-blanks",
             "--human-numeric-sort", "--version-sort", "--key", "--field-separator", "--stable",
             "--zero-terminated", "--merge", "--check", "--dictionary-order", "--general-numeric-sort",
             "--month-sort", "--random-sort"},
    "uniq": {"-c", "-d", "-u", "-i", "-f", "-s", "-w", "-z",
             "--count", "--repeated", "--unique", "--ignore-case", "--skip-fields", "--skip-chars",
             "--check-chars", "--zero-terminated"},
    "cut": {"-d", "-f", "-c", "-b", "-s", "--delimiter", "--fields", "--characters", "--bytes",
            "--only-delimited", "--complement", "--output-delimiter"},
    "diff": {"-u", "-q", "-r", "-N", "-a", "-b", "-i", "-w", "-B", "-c", "-e", "-y", "-U", "-C",
             "--unified", "--brief", "--recursive", "--new-file", "--text", "--ignore-space-change",
             "--ignore-case", "--ignore-all-space", "--ignore-blank-lines", "--context",
             "--side-by-side", "--label", "--color", "--no-dereference"},
    "nl": {"-b", "-n", "-s", "-w", "-v", "-i", "--number-style", "--number-format", "--separator",
           "--starting-line-number", "--line-increment", "--body-numbering"},
    "column": {"-t", "-s", "-c", "-x", "-n", "--table", "--separator", "--output-width",
               "--table-columns", "--table-name"},
    "jq": {"-r", "-c", "-n", "-s", "-e", "-j", "-a", "-R", "-C", "-M", "-S",
           "--raw-output", "--compact-output", "--null-input", "--slurp", "--exit-status",
           "--join-output", "--ascii-output", "--raw-input", "--color-output", "--monochrome-output",
           "--sort-keys", "--tab", "--indent", "--arg", "--argjson"},
    "md5sum": {"-b", "-t", "-c", "--binary", "--text", "--check", "--quiet", "--status"},
    "sha256sum": {"-b", "-t", "-c", "--binary", "--text", "--check", "--quiet", "--status"},
    "cmp": {"-b", "-i", "-l", "-n", "-s", "--print-bytes", "--ignore-initial", "--verbose",
            "--bytes", "--silent"},
    "stat": {"-c", "-f", "-L", "-t", "--format", "--file-system", "--dereference", "--terse"},
    "file": {"-b", "-i", "-L", "-z", "-h", "-N", "-r", "-s", "--brief", "--mime", "--dereference",
             "--uncompress", "--no-dereference", "--separator", "--readelf"},
    "du": {"-a", "-h", "-s", "-c", "-k", "-m", "-b", "-x", "-L", "-d", "--all", "--human-readable",
           "--summarize", "--total", "--bytes", "--one-file-system", "--dereference", "--max-depth",
           "--apparent-size", "--exclude"},
    "df": {"-h", "-a", "-i", "-k", "-m", "-T", "-P", "--human-readable", "--all", "--inodes",
           "--block-size", "--type", "--portability", "--print-type"},
    "date": {"-u", "-R", "-I", "-d", "+%s", "--utc", "--rfc-3339", "--iso-8601", "--date", "--universal"},
    "uname": {"-a", "-s", "-n", "-r", "-v", "-m", "-p", "-i", "-o", "--all", "--kernel-name",
              "--nodename", "--kernel-release", "--kernel-version", "--machine", "--processor",
              "--hardware-platform", "--operating-system"},
    "basename": {"-a", "-s", "-z", "--multiple", "--suffix", "--zero"},
    "dirname": {"-z", "--zero"},
    "realpath": {"-e", "-m", "-s", "-z", "--canonicalize-existing", "--canonicalize-missing",
                 "--strip", "--zero", "--quiet", "--relative-to"},
    "which": {"-a", "--all"},
    "type": {"-a", "-t", "-p", "-P", "-f", "--all", "--type", "--path"},
    "true": set(),
    "pwd": {"-L", "-P", "--logical", "--physical"},
    "whoami": set(),
    "hostname": {"-s", "-f", "-i", "-I", "--short", "--fqdn", "--ip-address", "--all-ip-addresses"},
    "less": {"-N", "-S", "-R", "-F", "-X", "-i", "-M", "--LINE-NUMBERS", "--chop-long-lines"},
    "find": {"-name", "-iname", "-type", "-maxdepth", "-mindepth", "-path", "-ipath", "-not", "-a",
             "-and", "-o", "-or", "!", "(", ")", "-newer", "-mtime", "-atime", "-ctime", "-size",
             "-perm", "-user", "-group", "-empty", "-readable", "-writable", "-executable",
             "-samefile", "-links", "-regex", "-iregex", "-print", "-print0", "-prune", "-quit"},
    # 只读 git / gh 子命令的选项（子命令本身另有集合校验）
    "git": {"-C", "-c", "-n", "-p", "-e", "-v", "-m", "-f", "-d", "-l", "-a", "-u", "-s", "-z", "-q",
            "-i", "-w", "-E", "-G", "-F", "-P", "-L", "-M", "-C", "-O", "-D", "-U", "--all", "--cached",
            "--staged", "--graph", "--oneline", "--stat", "--short", "--porcelain", "--pretty",
            "--format", "--no-pager", "--pager", "--name-only", "--name-status", "--numstat",
            "--decorate", "--no-decorate", "--color", "--no-color", "--verbose", "--quiet", "--branch",
            "--tags", "--contains", "--merged", "--no-merged", "--abbrev-commit", "--no-abbrev-commit",
            "--date", "--since", "--until", "--author", "--grep", "--count", "--max-count", "--skip",
            "--reverse", "--first-parent", "--merges", "--no-merges", "--follow", "--unified",
            "--word-diff", "--raw", "--patch", "--binary", "--check", "--exit-code", "--ignore-case",
            "--word-regexp", "--line-number", "--list", "--local", "--global", "--system", "--get",
            "--get-all", "--unset", "--add", "--replace-all", "--file", "--ignore-matching-lines",
            "--diff-filter", "--find-renames", "--relative", "--work-tree", "--git-dir", "--bare",
            "--long", "--dirty", "--squash", "--origin", "--set-upstream", "--show-current"},
    "gh": {"--json", "--limit", "--state", "--label", "--author", "--assignee", "--search",
           "--head", "--base", "--branch", "--repo", "-R", "-L", "-s", "-a", "-A", "-w", "-l",
           "--web", "--comments", "--draft", "--merged", "--closed", "--open", "--all", "--view"},
    # windows cmd
    "dir": {"/a", "/b", "/o", "/s", "/w", "/p", "/t", "/q", "/n", "/d", "/x", "/c"},
    "where": {"/r", "/q", "/f", "/t"},
    "findstr": {"/b", "/e", "/l", "/r", "/s", "/i", "/x", "/v", "/n", "/m", "/o", "/p", "/f", "/c", "/g", "/d", "/a"},
    "ver": set(),
    # PowerShell：参数不区分大小写，_check_opts 里按 lower() 比对；这里统一写小写
    "get-childitem": {"-path", "-filter", "-include", "-exclude", "-recurse", "-depth", "-force",
                      "-name", "-file", "-directory", "-hidden", "-readonly", "-system",
                      "-attributes", "-erroraction", "-ea"},
    "gci": {"-path", "-filter", "-include", "-exclude", "-recurse", "-depth", "-force",
            "-name", "-file", "-directory", "-hidden", "-readonly", "-system", "-attributes",
            "-erroraction", "-ea"},
    "get-content": {"-path", "-totalcount", "-head", "-tail", "-encoding", "-raw", "-delimiter",
                    "-readcount", "-first", "-last", "-erroraction", "-ea"},
    "gc": {"-path", "-totalcount", "-head", "-tail", "-encoding", "-raw", "-delimiter",
           "-readcount", "-first", "-last", "-erroraction", "-ea"},
    "get-location": {"-psprovider", "-psdrive", "-stack", "-erroraction", "-ea"},
    "gl": {"-psprovider", "-psdrive", "-stack", "-erroraction", "-ea"},
    "select-string": {"-pattern", "-path", "-inputobject", "-simplematch", "-casesensitive",
                      "-notmatch", "-allmatches", "-context", "-encoding", "-list", "-quiet",
                      "-raw", "-literalpath", "-erroraction", "-ea"},
    "sls": {"-pattern", "-path", "-inputobject", "-simplematch", "-casesensitive",
            "-notmatch", "-allmatches", "-context", "-encoding", "-list", "-quiet",
            "-raw", "-literalpath", "-erroraction", "-ea"},
    "get-item": {"-path", "-literalpath", "-force", "-stream", "-erroraction", "-ea"},
    "gi": {"-path", "-literalpath", "-force", "-stream", "-erroraction", "-ea"},
    "test-path": {"-path", "-literalpath", "-pathtype", "-filter", "-isvalid", "-newerthan",
                  "-olderthan", "-erroraction", "-ea"},
    "write-output": {"-inputobject", "-nonewline", "-noenumerate"},
    "write-host": {"-object", "-nonewline", "-foregroundcolor", "-backgroundcolor",
                   "-separator", "-noenumerate"},
    "get-command": {"-name", "-commandtype", "-modulename", "-all", "-listimported",
                    "-totalcount", "-syntax", "-argumentlist", "-erroraction", "-ea"},
    "gcm": {"-name", "-commandtype", "-modulename", "-all", "-listimported",
            "-totalcount", "-syntax", "-argumentlist", "-erroraction", "-ea"},
    "measure-object": {"-property", "-inputobject", "-sum", "-average", "-maximum", "-minimum",
                       "-count", "-standarddeviation", "-line", "-character", "-word"},
    "measure": {"-property", "-inputobject", "-sum", "-average", "-maximum", "-minimum",
                "-count", "-standarddeviation", "-line", "-character", "-word"},
    "resolve-path": {"-path", "-literalpath", "-relative", "-relativebasepath",
                     "-erroraction", "-ea"},
    "get-date": {"-date", "-format", "-uformat", "-displayhint", "-asutc"},
    "select-object": {"-property", "-expandproperty", "-first", "-last", "-skip", "-skipfirst",
                      "-skiplast", "-unique", "-inputobject", "-index", "-excludeproperty",
                      "-wait", "-descending", "-casesensitive", "-erroraction", "-ea"},
    "select": {"-property", "-expandproperty", "-first", "-last", "-skip", "-skipfirst",
               "-skiplast", "-unique", "-inputobject", "-index", "-excludeproperty",
               "-wait", "-descending", "-casesensitive", "-erroraction", "-ea"},
    "where-object": {"-filterscript", "-inputobject", "-property", "-value", "-eq", "-ne", "-gt",
                     "-ge", "-lt", "-le", "-like", "-notlike", "-match", "-notmatch", "-contains",
                     "-notcontains", "-in", "-notin", "-is", "-isnot", "-and", "-or", "-not",
                     "-casesensitive", "-erroraction", "-ea"},
    "where": {"-filterscript", "-inputobject", "-property", "-value", "-eq", "-ne", "-gt",
              "-ge", "-lt", "-le", "-like", "-notlike", "-match", "-notmatch", "-contains",
              "-notcontains", "-in", "-notin", "-is", "-isnot", "-and", "-or", "-not",
              "-casesensitive", "-erroraction", "-ea"},
    "foreach-object": {"-process", "-begin", "-end", "-membername", "-inputobject",
                       "-parallel", "-throttlelimit", "-erroraction", "-ea"},
    "sort-object": {"-property", "-descending", "-casesensitive", "-unique", "-inputobject",
                    "-stable", "-top", "-bottom", "-erroraction", "-ea"},
    "format-table": {"-property", "-autoSize", "-wrap", "-groupby", "-hideTableHeaders",
                     "-inputobject", "-view", "-erroraction", "-ea"},
    "ft": {"-property", "-autoSize", "-wrap", "-groupby", "-hidetableheaders",
           "-inputobject", "-view", "-erroraction", "-ea"},
    "format-list": {"-property", "-groupby", "-inputobject", "-view", "-force",
                    "-erroraction", "-ea"},
    "fl": {"-property", "-groupby", "-inputobject", "-view", "-force", "-erroraction", "-ea"},
    "out-string": {"-inputobject", "-width", "-stream"},
    "get-process": {"-name", "-id", "-computername", "-fileversioninfo", "-module",
                    "-includeusername", "-erroraction", "-ea"},
    "get-filehash": {"-path", "-literalpath", "-algorithm", "-inputobject", "-erroraction", "-ea"},
    "split-path": {"-path", "-literalpath", "-parent", "-leaf", "-leafbase", "-extension",
                   "-qualifier", "-noqualifier", "-isabsolute", "-resolve"},
    "join-path": {"-path", "-childpath", "-additionalchildpath", "-resolve"},
    "get-itemproperty": {"-path", "-literalpath", "-name", "-erroraction", "-ea"},
    "compare-object": {"-referenceobject", "-differenceobject", "-property", "-passthru",
                       "-includeequal", "-excludedifferent", "-syncwindow", "-casesensitive"},
    "group-object": {"-property", "-inputobject", "-noelement", "-asstring", "-casesensitive"},
    "convertto-json": {"-inputobject", "-depth", "-compress", "-enumsasstrings", "-asarray"},
    "convertfrom-json": {"-inputobject", "-depth", "-asenumerable"},
    "get-help": {"-name", "-category", "-component", "-functionality", "-role", "-module",
                 "-detailed", "-full", "-examples", "-parameter", "-online", "-showwindow"},
    "get-history": {"-id", "-count"},

    "ver": set(),
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


def _opt_allowed(arg: str, allowed: set[str]) -> bool:
    """这个选项在不在白名单里（长选项按 GNU 唯一前缀匹配）。

    与 `_opt_is` 的区别：这里是「用户写法是白名单项的缩写」，
    方向是 `白名单项.startswith(用户写法)`——和危险黑名单那边的方向一致。

    PowerShell 的参数不区分大小写（-Recurse / -recurse 一样），所以再按小写比一次。
    """
    a = arg.strip()
    if a in allowed:
        return True
    low = a.lower()
    low_allowed = {x.lower() for x in allowed} if "-" in a and a[:1] in "-/" else allowed
    if low in low_allowed:
        return True
    if a.startswith("--"):
        name = a.split("=", 1)[0]
        return any(o.startswith(name) for o in allowed if o.startswith("--"))
    # 短选项允许合并写：-la = -l -a；带值的 -n5 也认 -n
    if a.startswith("-") and len(a) > 2 and not a.startswith("--"):
        body = a[1:]
        if all(f"-{c}" in allowed for c in body):
            return True
        return f"-{body[0]}" in allowed          # -n5 / -A2 这类"选项+紧贴的值"
    return False


def _check_opts(name: str, args: list[str]) -> bool:
    """命令的选项是否都在白名单里。位置参数（文件名、模式串）不受限制。

    一个以 `-` 开头的参数可能是负数值（`head -5` 的 -5 是"-n 5"的简写，但 `cut -1`
    少见），也可能是选项的值（`--color=auto` 已按 = 截断）。这里只判"看起来像选项"
    的那些，允许白名单里显式列出的写法。
    """
    allowed = SAFE_OPTS.get(name)
    if allowed is None:
        # 不在 SAFE_OPTS 里的命令不接受任何选项（git/gh 走各自的子命令集合）
        return not any(a.startswith("-") and a != "-" for a in args)
    for a in args:
        if a == "-" or not a.startswith("-"):
            continue                              # - 表示 stdin；裸参数放行
        if a.startswith("/") and len(a) > 1 and name in ("dir", "where", "findstr"):
            pass                                  # windows cmd 的 /x 由 SAFE_OPTS 覆盖
        if _opt_allowed(a, allowed):
            continue
        # 纯数字（head -5 / tail -5）视为 -n 的简写
        if re.fullmatch(r"-\d+", a) and ("-n" in allowed or "--lines" in allowed):
            continue
        return False
    return True

NETWORK_COMMANDS = {"curl", "wget", "invoke-webrequest", "iwr", "invoke-restmethod", "irm", "ssh", "scp",
                    "nc", "ncat", "telnet", "ftp", "rsync"}


_NULL_TARGET = re.compile(r"(/dev/null|\$null|nul)", re.I)
# sed 只认这些“只读”写法：可选地址 + p/d/q/=/l/n/N 等，或 s 命令（标志里不能有 w / e）
_SED_ADDR = r"(?:\d+(?:~\d+)?|\$|/(?:[^/\\]|\\.)*/I?)"
_SED_CMD = re.compile(
    rf"\s*(?:{_SED_ADDR}(?:\s*,\s*(?:{_SED_ADDR}|[+~]\d+))?)?\s*!?\s*"
    r"(?:[pdqQ=lnNgGhHxDPz]|s(?P<d>[^\\\n\sA-Za-z0-9])(?:(?!(?P=d))[^\\]|\\.)*(?P=d)(?:(?!(?P=d))[^\\]|\\.)*(?P=d)[gpiImM0-9]*)?\s*")
_SED_FLAGS = {"-n", "--quiet", "--silent", "-E", "-r", "--regexp-extended", "-s", "--separate", "-u",
              "--unbuffered", "-z", "--null-data", "--posix", "--debug", "--sandbox"}


def _redirects_safe(seg: str) -> bool:
    """每个写重定向（> >> 1> 2> &> *>）的目标都必须是空设备；>&1 这种合并输出不算写文件。"""
    for m in re.finditer(r">+(?!&)\s*([^\s;|&<>]*)", seg):
        if not _NULL_TARGET.fullmatch(m.group(1).strip("'\"")):
            return False
    return True


def _has_code_syntax(seg: str, git: bool, syntax: str = "posix") -> bool:
    """引号外的 ( ) { }：PowerShell 的 (表达式)、@(…)、{脚本块}、@{…}，bash 的 <(…) 进程替换，都会执行命令。"""
    quote = None
    quotes = ('"',) if syntax == "cmd" else ("'", '"')   # cmd 里 ' 不是引号
    i = 0
    while i < len(seg):
        c = seg[i]
        if quote:
            if c == quote:
                quote = None
            elif c == "\\" and quote == '"':
                i += 1
        elif c in quotes:
            quote = c
        elif c in "(){}":
            if git and c == "{":         # git 的 HEAD@{1} / @{u}：只允许 @{简单内容}
                m = re.match(r"\{[\w.:-]*\}", seg[i:])
                if i > 0 and seg[i - 1] == "@" and m:
                    i += m.end()
                    continue
            return True
        i += 1
    return False


# ---------------------------------------------------------------- PowerShell 管道操作符
# 这几个 cmdlet 在白名单里（它们本身不碰文件/进程），但**参数可以是动作成员名**：
#   Get-Process | ForEach-Object Kill          ← Kill 是作用在每个进程上的方法
#   Get-Process | ForEach-Object -MemberName Kill
#   Get-ChildItem C:\ -Recurse -File | ForEach-Object Delete
# 只看命令名会直接放行。它们的“安全子集”是投影/过滤/统计：参数只能是属性名、比较
# 运算符、格式化参数，不能是裸的、看起来像动词的成员名。
_PS_MEMBER_CMDLETS = {
    "foreach-object", "%", "foreach",
    "where-object", "where", "?",
    "select-object", "select",
    "measure-object", "measure",
    "sort-object", "sort",
    "group-object", "group",
}
# 这些 cmdlet 的第一个位置参数是属性名（投影），可以放行
_PS_PROJECTION_CMDLETS = {"select-object", "select", "sort-object", "sort", "group-object", "group", "measure-object", "measure"}
# 明确的危险成员名（PowerShell 动词 - 名词 形式的系统方法 / 常见别名）
_PS_ACTION_MEMBERS = {
    "delete", "remove", "remove-item", "del", "erase", "rmdir", "rd", "rm",
    "kill", "stop", "stop-process", "spps", "terminate",
    "start", "start-process", "saps", "invoke", "invoke-expression", "iex",
    "set", "set-item", "set-content", "add-content", "out-file", "new-item",
    "move", "move-item", "copy", "copy-item", "rename", "rename-item",
    "clear", "clear-content", "close", "dispose", "write", "writeline",
    "open", "save", "lock", "unlock", "flush",
}
# 允许出现在参数里的、纯查询/格式化/比较用的开关（小写比较，允许 PS 前缀缩写）
_PS_SAFE_OPTS = {
    "-property", "-properties", "-expandproperty", "-first", "-last", "-skip", "-skipfirst", "-skiplast",
    "-unique", "-descending", "-ascending", "-casesensitive", "-casesensitive:", "-not", "-and", "-or",
    "-eq", "-ne", "-gt", "-ge", "-lt", "-le", "-like", "-notlike", "-match", "-notmatch",
    "-contains", "-notcontains", "-in", "-notin", "-is", "-isnot", "-as",
    "-membername", "-process", "-inputobject", "-end", "-begin",
    "-table", "-list", "-auto", "-wrap", "-none", "-full", "-noenumerate",
    "-sum", "-average", "-maximum", "-minimum", "-count", "-standarddeviation",
    "-line", "-character", "-word", "-culture", "-echo", "-verbose", "-debug",
}
# 这几个开关的值是“要执行的成员/脚本”，必须按动作判（支持 PowerShell 前缀缩写）
_PS_MEMBER_OPTS = {"-membername", "-process", "-expandproperty", "-begin", "-end",
                   "-filterscript", "-value"}
# 参数值是属性名/字符串，不会是动作的情况：这些开关后面跟的值一律跳过
_PS_VALUE_OPTS = {
    "-property", "-properties", "-expandproperty", "-first", "-last", "-skip", "-skipfirst", "-skiplast",
    "-membername", "-inputobject", "-eq", "-ne", "-gt", "-ge", "-lt", "-le", "-like", "-notlike",
    "-match", "-notmatch", "-contains", "-notcontains", "-in", "-notin", "-is", "-isnot", "-as",
}


def _ps_pipeline_safe(name: str, args: list[str]) -> bool:
    """管道操作符的参数检查：禁止把动作成员名当参数传进去。

    放行的形态（都是纯查询/投影/统计）：
      Select-Object Name / -Property Name,Length / -First 10 / -ExpandProperty Name
      Where-Object Name -like '*.py'
      ForEach-Object Name            （取属性）
      Measure-Object                 （无参数）
    拒绝：
      ForEach-Object Kill / -MemberName Kill / -Process Kill
      Where-Object Remove-Item
      Select-Object -ExpandProperty Kill
    """
    if not args:
        return True                     # 无参数：Measure-Object 之类
    i = 0
    n = len(args)
    while i < n:
        a = args[i]
        low = a.lower()
        if a.startswith("-") or (a.startswith("/") and len(a) > 1):
            # PowerShell 允许 -Param:Value 把值直接跟在参数名后面：-MemberName:Kill。
            # 必须先按冒号切开分别看——否则 :Kill 会被当成参数名的一部分而漏检。
            if ":" in low:
                base, val = low.split(":", 1)
            else:
                base, val = low, None
            if _resolve_member_opt(base):
                # 这几个开关后面跟的就是“要执行的成员/脚本”，一律按动作判
                target = val if val is not None else (args[i + 1] if i + 1 < n else None)
                if target is not None and _looks_like_action(target):
                    return False
                i += 1 if val is not None else 2
                continue
            if val is not None:
                # 其余开关的冒号值：不是成员名，但也不该当成“未知开关”跳过检查
                if _looks_like_action(val):
                    return False
                i += 1
                continue
            if base in _PS_VALUE_OPTS and i + 1 < n:
                i += 2
                continue
            if _ps_opt_known(base):
                i += 1
                continue
            # 不认识的开关：它后面的裸词可能是成员名，交给下面的裸词判断
            i += 1
            continue
        # 位置参数 / 裸词
        if _looks_like_action(a):
            # 投影类 cmdlet 的第一个位置参数是属性名，不是动作
            if i == 0 and name in _PS_PROJECTION_CMDLETS:
                # 名字里不能带动词形态的连字符（Delete-All 这种），纯属性名（Name/Length）放行
                if not _looks_like_verb_noun(a):
                    i += 1
                    continue
            return False
        i += 1
    return True


def _resolve_member_opt(base: str) -> bool:
    """这个开关是不是“值是待执行成员”的那一类（支持 PowerShell 前缀缩写：-memb → -MemberName）。

    缩写不能太短（-m 会同时命中 -MemberName / -Match），至少 4 个字符。
    """
    if base in _PS_MEMBER_OPTS:
        return True
    if len(base) < 4:
        return False
    return any(o.startswith(base) for o in _PS_MEMBER_OPTS)


def _ps_opt_known(low: str) -> bool:
    base = low.split(":", 1)[0]
    if base in _PS_SAFE_OPTS:
        return True
    # PowerShell 允许前缀缩写：-Exp 代表 -ExpandProperty
    return any(o.startswith(base) and len(base) >= 3 for o in _PS_SAFE_OPTS)


def _looks_like_verb_noun(s: str) -> bool:
    """PowerShell 系统方法名（Delete / Stop-Process / Remove-Item）。"""
    return bool(re.match(r"^[A-Za-z]+-[A-Za-z]+$", s))


def _is_dynamic(s: str) -> bool:
    """这个词是运行期才确定的值（变量、子表达式、脚本块、字符串插值、成员取值）。

    PowerShell 里 `$env:X` 可以是任意命令名、`$x.Kill` 是先取属性再当命令、`$(...)`
    直接执行命令。静态分析看不出它会变成什么，一律按"可能执行动作"处理。
    """
    t = s.strip()
    if not t:
        return False
    if t.startswith("$"):
        return True                       # $var / $env:X / ${env:X} / $(…) / @(…)
    if t.startswith("(") or t.startswith("@(") or t.startswith("{"):
        return True                       # (表达式) / @(数组) / {脚本块}
    if "${" in t or "$(" in t:
        return True                       # 字符串里带插值/子表达式
    return False


def _looks_like_action(s: str) -> bool:
    """这个词是不是"动作"而不是属性名。

    属性名（Name / Length / FullName / PSIsContainer）不含连字符；
    动作名要么在已知集合里，要么是 Verb-Noun 形态。
    运行期才定值的表达式（$env:X、$x.Kill、$(…)）也算——静态看不出它会不会是动作。
    """
    t = s.strip().strip("'\"")
    if not t:
        return False
    if _is_dynamic(t):
        return True                       # 动态值：不能证明安全，按动作处理
    low = t.lower()
    if low in _PS_ACTION_MEMBERS:
        return True
    if _looks_like_verb_noun(t):
        return True
    return False


def _sed_safe(args: list[str]) -> bool:
    scripts: list[str] = []
    rest = list(args)
    while rest:
        a = rest.pop(0)
        if a in ("-e", "--expression") and rest:
            scripts.append(rest.pop(0))
        elif a.startswith("--expression="):
            scripts.append(a.split("=", 1)[1])
        elif a.startswith("-") and a != "-":
            if a.startswith("-i") or a.startswith("--in-place"):
                return False
            if a not in _SED_FLAGS and not re.fullmatch(r"-[nErsuz]+", a):
                return False             # -f 脚本文件、-l 等其他参数：不判断，按需要确认处理
        else:
            if not scripts:
                scripts.append(a)
            # 其余是输入文件
    if "--sandbox" in args:
        return True
    for sc in scripts:
        for part in re.split(r"[;\n]", sc):
            if not _SED_CMD.fullmatch(part):
                return False
    return True


def _opt_is(arg: str, names: tuple[str, ...] | set[str]) -> bool:
    """这个参数是不是 names 里某个长选项的写法（含 GNU 的任意唯一前缀缩写）。

    GNU getopt_long 允许把 `--output` 写成 `--out` / `--ou` / `--o`，
    所以判断方向必须是「危险选项名.startswith(用户写的选项名)」——
    原来写成 `用户写法.startswith(危险选项名)`，只能匹配完整写法，写短了就绕过。

    前缀匹配是有意保守的：`--o` 同时是 `--output` / `--open-files-in-pager` 的前缀，
    实际 git 会因歧义报错，但我们直接拒绝——宁可让用户多写几个字母。
    """
    a = arg.strip()
    if not a.startswith("--"):
        return False
    name = a.split("=", 1)[0].rstrip()          # 按第一个 = 截断选项名
    if name == "--":                            # 选项结束标记，不是选项
        return False
    return any(d.startswith(name) for d in names)


def _any_opt(args: list[str], names: tuple[str, ...] | set[str]) -> bool:
    return any(_opt_is(a, names) for a in args)


# 各命令里能执行程序 / 写文件的长选项（完整写法；_opt_is 负责前缀）
# --paginate：ee 的 child_env() 强制 GIT_PAGER=cat / PAGER=cat，实测当前不可利用
#   （config core.pager 也被环境变量压住）；但它依赖那个前提，加一道纵深防御成本为零。
#   --no-pager 不以 --paginate 开头，不受影响。
_GIT_DANGEROUS_LONG = {
    "--config-env", "--exec-path", "--output", "--open-files-in-pager", "--ext-diff", "--paginate",
}
_SORT_DANGEROUS_LONG = {"--output", "--compress-program"}


def _segment_safe(seg: str, syntax: str = "posix") -> bool:
    if not _redirects_safe(seg):
        return False  # 重定向写文件
    if re.match(r"^\s*[A-Za-z_][A-Za-z0-9_]*=", seg):
        return False  # 前置环境变量（GIT_CONFIG_*、GIT_EXTERNAL_DIFF、LESSOPEN…）能让白名单命令执行任意程序
    if syntax == "posix":
        if words(seg, "posix") is None:
            return False  # 引号不配对：解析不了，按需要确认
        t = tokens(seg)
    else:
        w = words(seg, syntax)
        if w is None:
            return False
        if syntax == "cmd" and re.search(r"%[^%\s]+%", seg):
            return False  # %VAR% 在 cmd 解析前展开，内容里可能带 & | 等分隔符
        t = w[1:] if w and w[0] in ("&", ".") else w
    if t and _has_code_syntax(seg, _cmd_name(t[0]) == "git", syntax):
        return False
    if not t:
        return True
    name = _cmd_name(t[0])
    args = t[1:]
    if name in ("python", "python3", "py", "node", "npm", "pnpm", "yarn", "go", "cargo", "java", "dotnet",
                "pip", "uv", "rustc", "gcc", "make", "cmake", "deno", "bun", "ruby", "php", "pwsh", "powershell"):
        if args and args[0] in _VERSION_FLAGS:
            return True
        return False     # pytest 也不算只读：它会导入并执行仓库里的 conftest.py / 插件（任意代码）
    if name == "find":
        return not any(a in ("-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf",
                             "-fls") for a in args)
    # 白名单命令里能执行外部程序或写文件的参数（长选项按 GNU 前缀缩写匹配）
    if name == "rg" and _any_opt(args, {"--pre"}):
        return False
    if name == "sort" and (any(a == "-o" or a.startswith("-o") for a in args)
                           or _any_opt(args, _SORT_DANGEROUS_LONG)):
        return False
    if name == "tree" and any(a == "-o" or a.startswith("-o") for a in args):
        return False
    if name == "uniq" and len([a for a in args if not a.startswith("-")]) > 1:
        return False  # uniq IN OUT 会写 OUT
    if name == "git" and (any(a in ("-c", "-O") for a in args) or _any_opt(args, _GIT_DANGEROUS_LONG)):
        return False  # -c 注入配置可执行任意命令；--output 写文件；外部 diff / 分页器会执行程序
    if name == "sed":
        return _sed_safe(args)
    if name in _PS_MEMBER_CMDLETS:
        return _ps_pipeline_safe(name, args)
    if name not in SAFE_COMMANDS:
        return False
    # 参数白名单：不认识的一律拦（黑名单要穷举危险写法，白名单不用）
    if not _check_opts(name, args):
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


def _clean_path(p: str) -> str:
    p = p.strip().strip('"').strip("'")
    if os.name == "nt" and p:     # Windows：结尾点/空格、::$DATA、8.3 短名都指向同一个文件，先归一再比规则
        from ee.core.winpath import normalize_win_path
        p = normalize_win_path(p)
    return p


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
    # shell 工具实际用的命令语法（ShellInfo.syntax：posix / powershell / cmd）；None = 不知道，三套都按最严判
    shell: str | None = None

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
        # 与工具实际解析路径的方式一致（ToolContext.resolve 去首尾空白和引号；apply_patch 解析时也 strip），
        # 否则 ' /abs' 或 '"/abs"' 在这里被当成工作区内的相对路径，工具却写到了工作区外。
        if tool == "apply_patch":
            patch = (args.get("patch") or args.get("input") or "").replace("\r\n", "\n")
            raw = re.findall(r"^\*\*\* (?:Add File|Delete File|Update File|Move to): (.+)$", patch, re.M)
        else:
            raw = [str(args.get("path") or "")]
        return [_clean_path(p) for p in raw]

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
            if ".git" in [x.lower() for x in ap.relative_to(ws).parts]:
                return Decision("ask", f"修改 .git 内部文件：{ap}", prefix="")
        return Decision("allow")

    # ---- shell
    def _check_shell(self, command: str) -> Decision:
        if not command.strip():
            return Decision("allow")
        syntaxes = [self.shell] if self.shell in SYNTAXES else list(SYNTAXES)
        pairs: list[tuple[str, str]] = []          # (段, 语法)；不知道 shell 时三套语法的拆法取并集
        entries: list[_Seg] = []                   # 归一后的段（含 bash -c / cmd /c / pwsh -Command 里的命令）
        for sx in syntaxes:
            for seg in split_command(command, sx):
                if (seg, sx) not in pairs:
                    pairs.append((seg, sx))
            entries.extend(_analyze(command, sx))
        segs = list(dict.fromkeys(seg for seg, _ in pairs))
        norms = [e.norm for e in entries if e.norm]

        def hits(pat: str, raw: str, norm: str = "") -> bool:
            # 原文任意位置匹配（兼容旧行为）；归一后的命令只从命令名开头匹配，免得把参数里的字样当命令
            return bool(re.search(pat, raw, re.I) or (norm and re.match(pat, norm, re.I)))

        for e in entries + [_Seg(command, "")]:
            for pat, why in DENY_PATTERNS:
                if hits(pat, e.raw, e.norm):
                    return Decision("deny", f"危险命令已拦截（{why}）：{e.raw}")
            if any(_match_rule(r, "shell", e.raw) or (e.norm and _match_rule(r, "shell", e.norm))
                   for r in self.deny):
                return Decision("deny", f"命中 deny 规则：{e.raw}")
        for e in entries:
            for pat, why in FORCE_ASK_PATTERNS:
                if hits(pat, e.raw, e.norm):
                    if self.mode in READONLY_MODES:
                        return Decision("deny", f"只读模式不能执行：{e.raw}")
                    return Decision("ask", why, prefix="", force=True)
        for e in entries:
            if e.dynamic:
                if self.mode in READONLY_MODES:
                    return Decision("deny", f"只读模式不能执行：{e.raw}（{e.dynamic}）")
                return Decision("ask", e.dynamic, prefix="", force=True)
        unsafe = list(dict.fromkeys(seg for seg, sx in pairs if not _segment_safe(seg, sx)))
        if self.mode in READONLY_MODES:
            if unsafe:
                return Decision("deny", f"只读模式只能运行只读命令，被拦截：{unsafe[0]}")
            return Decision("allow")
        if any(_match_rule(r, "shell", s) for r in self.ask for s in segs + norms):
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
        return Decision("ask", why, prefix=command_prefix(first))
