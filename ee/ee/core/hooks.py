"""Hooks（D5-4）：在会话的关键时刻运行用户配置的命令。

事件（6 个）：
  SessionStart      会话开始（本进程里第一次跑回合；source = startup | resume | clear | compact）。
                    stdout / additionalContext 作为补充上下文随第一条消息发给模型。
  UserPromptSubmit  用户发出消息、模型收到之前。退出码 2 / decision=block → 拦下这条消息（不发给模型）；
                    stdout / additionalContext 追加给模型。
  PreToolUse        工具执行前（权限检查之前）。退出码 2 / decision=block / permissionDecision=deny → 不执行，
                    原因回给模型；permissionDecision=allow → 免审批（deny 规则照样生效）；=ask → 强制审批；
                    updatedInput → 改写参数。
  PostToolUse       工具执行后。退出码 2 / decision=block → 把原因追加到工具结果里给模型看；additionalContext 同样追加。
  Stop              回合即将结束（模型不再调工具）。退出码 2 / decision=block → 把原因当作新的要求让模型继续
                    （每回合最多连续 3 次；stdin 里 stop_hook_active 表示已经因 Stop Hook 继续过）。
  Notification      需要用户审批时（notification_type = permission_prompt）。不等待、不能拦截。

配置：
  ~/.ee/config.toml（用户级，直接生效）：
    [hooks]
    enabled = true
    allowed_project = ["路径sha8:内容sha8", ...]   # 用户允许过的项目 hooks 文件（内容变了要重新允许）

    [[hooks.PreToolUse]]
    matcher = "shell|write"     # 工具名：| 分隔，可用 * ? 通配；含其他正则字符时按正则整体匹配；空 / * = 全部
    command = "python check.py" # 字符串 = 用 shell 运行（默认跟随设置的 shell，可用 shell = "cmd" 等覆盖）
    # command = ["python", "check.py"]   # 数组 = 直接运行程序，不经 shell
    timeout = 60                # 秒，1–600
    enabled = true
    name = "检查命令"           # 可选，界面显示用

  项目级（默认不执行，要在 设置 → Hooks 里允许）：
    <项目>/.ee/hooks.toml       顶层写 [[PreToolUse]] … 或 [[hooks.PreToolUse]] …
    <项目>/.ee/config.toml      里的 [[hooks.<事件>]]
    项目文件里写的 enabled / allowed_project 一律忽略（项目不能给自己授权）。

hook 进程：
  stdin  一个 JSON：session_id、cwd、hook_event_name、permission_mode、agent（main | subagent）及事件字段
         （prompt；tool_name、tool_input、tool_call_id；tool_response、tool_ok；stop_hook_active、
         last_assistant_message；source；notification_type、message）。
  环境   EE_PROJECT_DIR（git 根或工作目录）、EE_SESSION_ID、EE_HOOK_EVENT、EE_HOOK_SOURCE（user | project）。
  退出码 0 = 继续（stdout 若是 JSON 则解析：decision、reason、additionalContext、updatedInput、continue、
         stopReason、systemMessage，也认 hookSpecificOutput 里的同名字段）；2 = 拦截（原因取 stderr）；
         其他 = 出错：只记日志并在界面出提示，不影响流程。超时杀整个进程树。
同一事件匹配到的多个 hook 并行运行；任一拦截即拦截，补充上下文按配置顺序拼接。
"""
from __future__ import annotations

import asyncio
import copy
import fnmatch
import hashlib
import json
import logging
import os
import re
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ee.proc import IS_WINDOWS, kill_tree, popen_kwargs

log = logging.getLogger("ee.hooks")

EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "Notification"]
EVENT_LABEL = {
    "SessionStart": "会话开始",
    "UserPromptSubmit": "提交消息",
    "PreToolUse": "工具执行前",
    "PostToolUse": "工具执行后",
    "Stop": "回合结束",
    "Notification": "需要审批",
}
# matcher 匹配的对象：工具名 / SessionStart 的 source / Notification 的 notification_type；其余事件忽略 matcher
MATCHER_EVENTS = {"PreToolUse", "PostToolUse", "SessionStart", "Notification"}
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
MAX_STOP_CONTINUES = 3
TEXT_LIMIT = 10000          # 回给模型的原因 / 补充上下文上限（字符）
PAYLOAD_LIMIT = 100000      # stdin 里 tool_response 上限
HOOK_KEYS = ("matcher", "command", "timeout", "shell", "enabled", "name")
_GLOB_ONLY = re.compile(r"^[A-Za-z0-9_\-*?|. ]*$")
_TOOL_ALIAS = {"bash": "shell"}   # 其他工具的 hook 脚本里常见写法


# ------------------------------------------------------------------ 配置
@dataclass
class HookDef:
    event: str
    command: str | list
    matcher: str = ""
    timeout: float = DEFAULT_TIMEOUT
    shell: str = ""            # 空 = 跟随设置里的 shell（只对字符串命令有效）
    enabled: bool = True
    name: str = ""
    source: str = "user"       # user | project
    file: str = ""

    @property
    def label(self) -> str:
        if self.name:
            return self.name
        cmd = self.command if isinstance(self.command, str) else " ".join(self.command)
        return cmd if len(cmd) <= 60 else cmd[:57] + "…"

    def matches(self, target: str | None) -> bool:
        if self.event not in MATCHER_EVENTS:
            return True
        return match(self.matcher, target or "")

    def summary(self) -> dict:
        return {"event": self.event, "matcher": self.matcher, "command": self.command, "timeout": self.timeout,
                "shell": self.shell, "enabled": self.enabled, "name": self.name, "source": self.source,
                "file": self.file, "label": self.label}


def match(matcher: str, target: str) -> bool:
    """空 / * = 全部；只含字母数字 _ - * ? | 时按 | 分隔的通配（不分大小写，bash 视同 shell）；否则按正则整体匹配。"""
    m = (matcher or "").strip()
    if m in ("", "*"):
        return True
    if _GLOB_ONLY.match(m):
        t = target.lower()
        for part in m.split("|"):
            p = part.strip().lower()
            if not p:
                continue
            p = _TOOL_ALIAS.get(p, p)
            if fnmatch.fnmatchcase(t, p):
                return True
        return False
    try:
        return re.fullmatch(m, target) is not None
    except re.error:
        return False


def _flatten(entry: dict) -> list[dict]:
    """兼容嵌套写法 {matcher, hooks: [{type: "command", command, timeout}]}。"""
    inner = entry.get("hooks")
    if isinstance(inner, list):
        out = []
        for h in inner:
            if isinstance(h, dict) and h.get("type", "command") == "command":
                out.append({**{k: v for k, v in entry.items() if k != "hooks"}, **h})
        return out
    return [entry]


def parse_hook(event: str, d: dict, source: str = "user", file: str = "") -> HookDef:
    if event not in EVENTS:
        raise ValueError(f"未知的 Hook 事件：{event}（可用：{'、'.join(EVENTS)}）")
    if not isinstance(d, dict):
        raise ValueError(f"{event} 的 Hook 配置格式不对（应为表）")
    cmd = d.get("command")
    if isinstance(cmd, list):
        cmd = [str(x) for x in cmd]
        if not cmd or not cmd[0].strip():
            raise ValueError(f"{event} 的 Hook 缺少 command")
    else:
        cmd = str(cmd or "").strip()
        if not cmd:
            raise ValueError(f"{event} 的 Hook 缺少 command")
    try:
        timeout = float(d.get("timeout") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        raise ValueError(f"{event} 的 Hook timeout 不是数字：{d.get('timeout')}")
    if not 1 <= timeout <= MAX_TIMEOUT:
        raise ValueError(f"{event} 的 Hook timeout 应在 1–{MAX_TIMEOUT} 秒之间")
    matcher = str(d.get("matcher") or "").strip()
    if matcher and not _GLOB_ONLY.match(matcher):
        try:
            re.compile(matcher)
        except re.error as e:
            raise ValueError(f"{event} 的 Hook matcher 不是合法的正则：{matcher}（{e}）")
    shell = str(d.get("shell") or "").strip()
    if shell:
        from ee.tools.shellenv import SHELL_CHOICES
        if shell.lower().replace("_", "-") not in SHELL_CHOICES + ["gitbash"]:
            raise ValueError(f"{event} 的 Hook shell 未知：{shell}（可选：{', '.join(SHELL_CHOICES)}）")
    return HookDef(event=event, command=cmd, matcher=matcher, timeout=timeout, shell=shell,
                   enabled=bool(d.get("enabled", True)), name=str(d.get("name") or "").strip(),
                   source=source, file=file)


def parse_table(table: dict, source: str = "user", file: str = "") -> tuple[list[HookDef], list[str]]:
    hooks: list[HookDef] = []
    errors: list[str] = []
    for event, entries in (table or {}).items():
        if event in ("enabled", "allowed_project"):
            continue
        if event not in EVENTS:
            errors.append(f"{file or '用户配置'}：未知的 Hook 事件 {event}（可用：{'、'.join(EVENTS)}）")
            continue
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            errors.append(f"{file or '用户配置'}：hooks.{event} 应写成 [[hooks.{event}]]")
            continue
        for e in entries:
            for d in _flatten(e) if isinstance(e, dict) else [e]:
                try:
                    hooks.append(parse_hook(event, d, source, file))
                except ValueError as ex:
                    errors.append(f"{file or '用户配置'}：{ex}")
    return hooks, errors


def events_table(table: dict) -> dict:
    """只保留事件键（项目文件里的 enabled / allowed_project 不算，也不参与内容指纹）。"""
    return {k: v for k, v in (table or {}).items() if k not in ("enabled", "allowed_project")}


def _sha8(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:8]


def allow_id(path: Path, table: dict) -> str:
    """项目 hooks 的允许记录：路径指纹:内容指纹。内容（hook 定义）一变就要重新允许。"""
    try:
        p = str(Path(path).resolve())
    except OSError:
        p = str(path)
    if IS_WINDOWS:
        p = p.lower()
    blob = json.dumps(events_table(table), sort_keys=True, ensure_ascii=False)
    return f"{_sha8(p)}:{_sha8(blob)}"


@dataclass
class ProjectHooks:
    file: str
    allow_id: str
    allowed: bool
    hooks: list[HookDef] = field(default_factory=list)

    def summary(self) -> dict:
        return {"file": self.file, "allow_id": self.allow_id, "allowed": self.allowed,
                "hooks": [h.summary() for h in self.hooks]}


@dataclass
class HookConfig:
    enabled: bool = True
    user: list[HookDef] = field(default_factory=list)
    projects: list[ProjectHooks] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def active(self, event: str) -> list[HookDef]:
        if not self.enabled:
            return []
        out = [h for h in self.user if h.event == event and h.enabled]
        for p in self.projects:
            if p.allowed:
                out += [h for h in p.hooks if h.event == event and h.enabled]
        return out

    @property
    def pending_projects(self) -> list[ProjectHooks]:
        return [p for p in self.projects if not p.allowed and p.hooks]

    def summary(self) -> dict:
        return {"enabled": self.enabled, "user": [h.summary() for h in self.user],
                "projects": [p.summary() for p in self.projects], "errors": list(self.errors)}


def project_files(cwd: Path | None) -> list[Path]:
    """项目 hooks 文件：从 cwd 往上（到 git 根为止）最近的 .ee/hooks.toml 和 .ee/config.toml（不含用户级）。"""
    if cwd is None:
        return []
    from ee.config import ee_home
    try:
        cur = Path(cwd).resolve()
        home_ee = ee_home().resolve()
    except OSError:
        return []
    found: dict[str, Path] = {}
    for d in [cur, *cur.parents]:
        for name in ("hooks.toml", "config.toml"):
            p = d / ".ee" / name
            if name not in found and p.is_file():
                try:
                    if p.resolve().parent == home_ee:
                        continue
                except OSError:
                    continue
                found[name] = p
        if (d / ".git").exists():
            break
    return [found[n] for n in ("hooks.toml", "config.toml") if n in found]


def _project_table(p: Path) -> dict:
    with open(p, "rb") as f:
        data = tomllib.load(f)
    if p.name == "hooks.toml":
        t = data.get("hooks") if isinstance(data.get("hooks"), dict) else data
    else:
        t = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    return events_table(t)


def load(cwd: Path | None, user_data: dict | None = None) -> HookConfig:
    """读 hooks 配置。user_data 默认读 ~/.ee/config.toml（绝不用合并了项目配置的 config.data，免得项目自己授权）。"""
    if user_data is None:
        from ee.config import load_user_file
        try:
            user_data = load_user_file()
        except Exception as e:  # noqa: BLE001
            return HookConfig(errors=[f"读取用户配置失败：{e}"])
    h = user_data.get("hooks") or {}
    if not isinstance(h, dict):
        return HookConfig(errors=["用户配置里的 hooks 格式不对（应为 [hooks] 表）"])
    cfg = HookConfig(enabled=bool(h.get("enabled", True)))
    cfg.user, cfg.errors = parse_table(events_table(h), "user", "")
    allowed = set(str(x) for x in (h.get("allowed_project") or []))
    for p in project_files(cwd):
        try:
            table = _project_table(p)
        except (OSError, ValueError) as e:
            cfg.errors.append(f"读取 {p} 失败：{e}")
            continue
        if not table:
            continue
        hooks, errs = parse_table(table, "project", str(p))
        cfg.errors += errs
        aid = allow_id(p, table)
        cfg.projects.append(ProjectHooks(str(p), aid, aid in allowed, hooks))
    return cfg


# ------------------------------------------------------------------ 运行一个 hook
@dataclass
class HookRun:
    hook: HookDef
    code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str = ""            # 启动失败
    duration_ms: int = 0
    data: dict | None = None   # stdout 解析出的 JSON

    def summary(self) -> dict:
        return {"label": self.hook.label, "event": self.hook.event, "code": self.code, "stdout": self.stdout[-20000:],
                "stderr": self.stderr[-20000:], "timed_out": self.timed_out, "error": self.error,
                "duration_ms": self.duration_ms, "json": self.data}


def project_dir(cwd: Path) -> Path:
    cur = Path(cwd)
    for d in [cur, *cur.parents]:
        if (d / ".git").exists():
            return d
    return cur


def _argv(hook: HookDef, default_shell: str) -> tuple[list[str], Path | None]:
    if isinstance(hook.command, list):
        return list(hook.command), None
    from ee.tools.shellenv import ShellNotFound, resolve_shell
    try:
        sh = resolve_shell(hook.shell or default_shell or "auto")
    except ShellNotFound:
        sh = resolve_shell("auto")
    return sh.prepare(hook.command)


def _decode(b: bytes) -> str:
    s = (b or b"").decode("utf-8", errors="replace").replace("\r\n", "\n")
    if "#< CLIXML" in s:
        from ee.tools.shellenv import strip_clixml
        s = strip_clixml(s)
    return s


async def _kill(proc) -> None:
    if proc.returncode is not None:
        return
    try:
        if IS_WINDOWS:
            await asyncio.to_thread(kill_tree, proc.pid)
        else:
            kill_tree(proc.pid)
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except Exception:  # noqa: BLE001
        pass


async def run_hook(hook: HookDef, payload: dict, cwd: Path, default_shell: str = "auto",
                   env_extra: dict | None = None) -> HookRun:
    """跑一个 hook：stdin 写 JSON 后关闭；超时 / 被取消时杀整个进程树。绝不抛异常（取消除外）。"""
    r = HookRun(hook)
    t0 = time.monotonic()
    script = None
    try:
        argv, script = _argv(hook, default_shell)
    except Exception as e:  # noqa: BLE001
        r.error = f"找不到可用的 shell：{e}"
        return r
    env = dict(os.environ)
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "EE_PROJECT_DIR": str(project_dir(cwd)),
                "EE_HOOK_EVENT": hook.event, "EE_HOOK_SOURCE": hook.source})
    env.update({k: str(v) for k, v in (env_extra or {}).items()})
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    proc = None
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(cwd), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=env, **popen_kwargs())
        except (OSError, ValueError) as e:
            r.error = f"无法启动：{e}"
            return r
        try:
            out, err = await asyncio.wait_for(proc.communicate(data), timeout=hook.timeout)
            r.code = proc.returncode
            r.stdout, r.stderr = _decode(out), _decode(err)
        except asyncio.TimeoutError:
            r.timed_out = True
            await _kill(proc)
        except asyncio.CancelledError:
            if proc.returncode is None:
                try:
                    kill_tree(proc.pid)
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            raise
    finally:
        r.duration_ms = int((time.monotonic() - t0) * 1000)
        if script is not None:
            try:
                script.unlink()
            except OSError:
                pass
    s = r.stdout.strip()
    if r.code == 0 and s.startswith("{"):
        try:
            v = json.loads(s)
            r.data = v if isinstance(v, dict) else None
        except ValueError:
            r.data = None
    return r


# ------------------------------------------------------------------ 结果解释与合并
@dataclass
class Outcome:
    block: bool = False
    reasons: list[str] = field(default_factory=list)
    context: list[str] = field(default_factory=list)
    permission: str | None = None          # allow | ask（PreToolUse）
    updated_input: dict | None = None
    stop: bool = False                     # continue=false：结束本回合
    stop_reason: str = ""
    notices: list[tuple[str, str]] = field(default_factory=list)   # (文字, info|warn)
    runs: list[HookRun] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return _clip("\n".join(x for x in self.reasons if x).strip(), TEXT_LIMIT)

    @property
    def context_text(self) -> str:
        return _clip("\n\n".join(x for x in self.context if x).strip(), TEXT_LIMIT)


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + f"\n…（截断了 {len(s) - n} 个字符）"


def _first(s: str, n: int = 300) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


CAN_BLOCK = {"UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"}
PLAIN_CONTEXT = {"SessionStart", "UserPromptSubmit"}   # 退出码 0 时普通 stdout 也作为补充上下文


def interpret(run: HookRun, out: Outcome) -> None:
    ev, h = run.hook.event, run.hook
    tag = f"Hook · {EVENT_LABEL.get(ev, ev)} · {h.label}"
    out.runs.append(run)
    if run.error:
        out.notices.append((f"{tag}：{run.error}", "warn"))
        return
    if run.timed_out:
        out.notices.append((f"{tag}：超过 {h.timeout:g} 秒没有结束，已终止（不影响继续）", "warn"))
        return
    if run.code == 2:
        why = run.stderr.strip() or run.stdout.strip() or "（Hook 没有给出原因）"
        if ev in CAN_BLOCK:
            out.block = True
            out.reasons.append(why)
        else:
            out.notices.append((f"{tag}：{_first(why)}", "warn"))
        return
    if run.code != 0:
        out.notices.append((f"{tag}：退出码 {run.code}" + (f"：{_first(run.stderr)}" if run.stderr.strip() else ""),
                            "warn"))
        return
    d = run.data
    if d is None:
        if ev in PLAIN_CONTEXT and run.stdout.strip():
            out.context.append(run.stdout.strip())
        return
    spec = d.get("hookSpecificOutput") if isinstance(d.get("hookSpecificOutput"), dict) else {}
    ctx = d.get("additionalContext") or spec.get("additionalContext")
    if ctx:
        out.context.append(str(ctx).strip())
    if d.get("systemMessage"):
        out.notices.append((f"{tag}：{_first(str(d['systemMessage']), 1000)}", "info"))
    decision = str(d.get("decision") or "").lower()
    pd = str(spec.get("permissionDecision") or "").lower()
    reason = str(d.get("reason") or spec.get("permissionDecisionReason") or "").strip()
    if decision == "block" or pd == "deny":
        if ev in CAN_BLOCK:
            out.block = True
            out.reasons.append(reason or "（Hook 没有给出原因）")
    elif ev == "PreToolUse" and (decision in ("approve", "allow") or pd == "allow"):
        out.permission = out.permission or "allow"
    elif ev == "PreToolUse" and pd == "ask":
        out.permission = "ask"
    upd = d.get("updatedInput") if isinstance(d.get("updatedInput"), dict) else spec.get("updatedInput")
    if ev == "PreToolUse" and isinstance(upd, dict):
        out.updated_input = upd
    if d.get("continue") is False:
        out.stop = True
        out.stop_reason = str(d.get("stopReason") or reason or "").strip()


# ------------------------------------------------------------------ 会话侧
class HookRunner:
    """每个 Session 一个（懒建在 session._ee_hooks 上）。回合开始时重读配置，回合内不变。"""

    def __init__(self, session):
        self.session = session
        self.config: HookConfig | None = None
        self.started_key: tuple | None = None     # SessionStart 已为哪个 (store id, 压缩次数) 跑过
        self.stop_count = 0
        self.updated: dict[str, dict] = {}        # call_id → PreToolUse 改写后的参数（PostToolUse 用）
        self.tasks: set = set()
        self.warned: set[str] = set()

    @property
    def is_sub(self) -> bool:
        return getattr(self.session, "hooks_role", "main") == "subagent"

    async def reload(self) -> HookConfig:
        try:
            self.config = await asyncio.to_thread(load, self.session.cwd)
        except Exception as e:  # noqa: BLE001
            log.warning("读取 hooks 配置失败：%s", e)
            self.config = HookConfig(errors=[f"读取 hooks 配置失败：{e}"])
        return self.config

    async def ensure(self) -> HookConfig:
        return self.config if self.config is not None else await self.reload()

    def base_payload(self, event: str) -> dict:
        s = self.session
        p = {"session_id": s.store.id, "cwd": str(s.cwd), "hook_event_name": event,
             "permission_mode": s.policy.mode, "agent": "subagent" if self.is_sub else "main"}
        path = getattr(s.store, "path", None)
        if path is not None and getattr(s.store, "enabled", False):
            p["transcript_path"] = str(path)
        return p

    async def fire(self, event: str, extra: dict, target: str | None, emit) -> Outcome:
        cfg = await self.ensure()
        hooks = [h for h in cfg.active(event) if h.matches(target)]
        out = Outcome()
        if not hooks:
            return out
        from ee.core.events import Notice, Status
        s = self.session
        payload = {**self.base_payload(event), **extra}
        env = {"EE_SESSION_ID": s.store.id}
        shell = s.shell_name or "auto"
        if event != "Notification":
            emit(Status(f"正在运行 Hook（{EVENT_LABEL.get(event, event)}）…"))
        try:
            runs = await asyncio.gather(*(run_hook(h, payload, s.cwd, shell, env) for h in hooks))
        finally:
            if event != "Notification":
                emit(Status(""))
        for r in runs:
            log.info("Hook %s [%s] %s：退出码 %s 用时 %dms%s%s", event, r.hook.source, r.hook.label, r.code,
                     r.duration_ms, " 超时" if r.timed_out else "", f" 错误 {r.error}" if r.error else "")
            if r.code not in (0, None) and r.stderr.strip():
                log.info("Hook %s stderr：%s", r.hook.label, _first(r.stderr, 2000))
            interpret(r, out)
        for text, level in out.notices:
            emit(Notice(text, level))
        return out

    def config_notices(self, emit) -> None:
        """配置错误 / 未允许的项目 hooks：每个会话只提示一次。"""
        from ee.core.events import Notice
        cfg = self.config
        if cfg is None or not cfg.enabled:
            return
        for e in cfg.errors:
            if e not in self.warned:
                self.warned.add(e)
                emit(Notice(f"Hooks 配置：{e}", "warn"))
        for p in cfg.pending_projects:
            key = "pending:" + p.allow_id
            if key not in self.warned:
                self.warned.add(key)
                emit(Notice(f"项目里的 {len(p.hooks)} 个 Hook（{p.file}）还没被允许，不会运行；"
                            "确认可信后在 设置 → Hooks 里允许。", "info"))


def runner_for(session) -> HookRunner:
    r = getattr(session, "_ee_hooks", None)
    if r is None:
        r = HookRunner(session)
        session._ee_hooks = r
    return r


async def on_turn_start(session, text: str, emit) -> bool:
    """回合开始：重读配置 → SessionStart（需要时）→ UserPromptSubmit。返回 True = 这条消息被拦下。"""
    from ee.core.events import Notice
    r = runner_for(session)
    if r.is_sub:
        return False
    r.stop_count = 0
    await r.reload()
    r.config_notices(emit)
    key = (session.store.id, getattr(session, "compactions", 0))
    if r.started_key != key:
        if r.started_key is None:
            source = "resume" if any(m.get("role") == "assistant" for m in session.history) else "startup"
        elif r.started_key[0] != key[0]:
            source = "clear"
        else:
            source = "compact"
        r.started_key = key
        o = await r.fire("SessionStart", {"source": source}, source, emit)
        if o.context_text:
            session.add_note("[Hook 补充的上下文（会话开始）]\n" + o.context_text)
    o = await r.fire("UserPromptSubmit", {"prompt": text}, None, emit)
    if o.block or o.stop:
        why = o.reason or o.stop_reason or "（没有给出原因）"
        emit(Notice(f"Hook 拦下了这条消息，没有发给模型：{_first(why, 1000)}", "warn"))
        return True
    if o.context_text:
        session.add_note("[Hook 补充的上下文]\n" + o.context_text)
    return False


@dataclass
class PreResult:
    args: dict
    result: object = None          # ToolResult：不执行工具，直接用它
    permission: str | None = None  # allow | ask


async def pre_tool_use(session, call_id: str, tool, args: dict, emit) -> PreResult:
    r = runner_for(session)
    cfg = await r.ensure()
    if not cfg.active("PreToolUse"):
        return PreResult(args)
    o = await r.fire("PreToolUse", {"tool_name": tool.name, "tool_input": args, "tool_call_id": call_id},
                     tool.name, emit)
    if o.block or o.stop:
        from ee.tools import ToolResult
        why = o.reason or o.stop_reason or "（没有给出原因）"
        return PreResult(args, ToolResult(f"被 Hook 拦截，没有执行：{why}", ok=False,
                                          meta={"denied": True, "hook": True}))
    if o.updated_input is not None:
        args = {**o.updated_input}
        r.updated[call_id] = args
    return PreResult(args, None, o.permission)


def adjust_decision(d, pre: PreResult):
    """PreToolUse 的 permissionDecision：allow 把“要问”变成“允许”（deny 规则不受影响）；ask 把“允许”变成“要问”。"""
    from ee.core.permissions import Decision
    if pre.permission == "allow" and d.action == "ask":
        return Decision("allow", "Hook 允许", d.prefix)
    if pre.permission == "ask" and d.action == "allow":
        return Decision("ask", "Hook 要求确认", d.prefix, force=True)
    return d


async def post_tool_use(session, call_id: str, tool, args: dict, res, emit):
    r = runner_for(session)
    args = r.updated.pop(call_id, args)
    cfg = await r.ensure()
    if not cfg.active("PostToolUse") or res.meta.get("denied"):
        return res
    out = res.output or ""
    o = await r.fire("PostToolUse", {"tool_name": tool.name, "tool_input": args, "tool_call_id": call_id,
                                     "tool_response": _clip(out, PAYLOAD_LIMIT), "tool_ok": bool(res.ok)},
                     tool.name, emit)
    extra = []
    if o.block:
        extra.append(f"[Hook 反馈] {o.reason}")
    if o.context_text:
        extra.append(f"[Hook 补充的上下文]\n{o.context_text}")
    if not extra:
        return res
    res.output = (out.rstrip() + "\n\n" if out.strip() else "") + "\n\n".join(extra)
    return res


async def on_stop(session, final: str, emit) -> bool:
    """回合即将结束。返回 True = Stop Hook 要求继续（已把原因作为提示加进历史）。"""
    from ee.core.events import Notice
    r = runner_for(session)
    if r.is_sub:
        return False
    cfg = await r.ensure()
    if not cfg.active("Stop"):
        return False
    o = await r.fire("Stop", {"stop_hook_active": r.stop_count > 0, "last_assistant_message": final}, None, emit)
    if not o.block or o.stop:
        return False
    if r.stop_count >= MAX_STOP_CONTINUES:
        emit(Notice(f"Hook（回合结束）已连续 {MAX_STOP_CONTINUES} 次要求继续，本回合到此结束。", "warn"))
        return False
    r.stop_count += 1
    emit(Notice(f"Hook（回合结束）要求继续：{_first(o.reason, 500)}", "info"))
    session._append({"role": "user", "content": f"[Hook 要求继续] {o.reason}", "_kind": "note"})
    return True


def notify(session, notification_type: str, message: str, emit) -> None:
    """Notification：后台跑，不等待、不能拦截。"""
    r = runner_for(session)
    cfg = r.config
    if cfg is None or not cfg.active("Notification"):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    t = loop.create_task(r.fire("Notification", {"notification_type": notification_type, "message": message},
                                notification_type, emit))
    r.tasks.add(t)
    t.add_done_callback(r.tasks.discard)


# ------------------------------------------------------------------ 设置页
SAMPLE = {
    "SessionStart": {"source": "startup"},
    "UserPromptSubmit": {"prompt": "（测试）帮我看看这个项目"},
    "PreToolUse": {"tool_name": "shell", "tool_input": {"command": "git status"}, "tool_call_id": "test"},
    "PostToolUse": {"tool_name": "shell", "tool_input": {"command": "git status"}, "tool_call_id": "test",
                    "tool_response": "退出码 0\nOn branch main", "tool_ok": True},
    "Stop": {"stop_hook_active": False, "last_assistant_message": "（测试）已完成。"},
    "Notification": {"notification_type": "permission_prompt", "message": "运行命令：git push"},
}


async def test_hook(hook: HookDef, cwd: Path, shell: str = "auto") -> dict:
    """设置页「测试运行」：用示例输入跑一次，返回原始结果和 ee 会怎么理解它。"""
    payload = {"session_id": "test", "cwd": str(cwd), "hook_event_name": hook.event, "permission_mode": "default",
               "agent": "main", "test": True, **SAMPLE.get(hook.event, {})}
    r = await run_hook(hook, payload, cwd, shell, {"EE_SESSION_ID": "test"})
    o = Outcome()
    interpret(r, o)
    if r.error:
        verdict = "启动失败"
    elif r.timed_out:
        verdict = "超时"
    elif o.block:
        verdict = "拦截" if hook.event in CAN_BLOCK else "退出码 2（此事件不能拦截）"
    elif r.code not in (0, None):
        verdict = "出错（不影响流程）"
    else:
        verdict = "继续"
    return {**r.summary(), "payload": payload, "verdict": verdict, "reason": o.reason, "context": o.context_text,
            "permission": o.permission, "updated_input": o.updated_input, "notices": [t for t, _ in o.notices]}


def settings_out(data: dict) -> dict:
    h = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    events: dict[str, list] = {}
    for ev in EVENTS:
        rows = []
        entries = h.get(ev) or []
        if isinstance(entries, dict):
            entries = [entries]
        for e in entries if isinstance(entries, list) else []:
            for d in _flatten(e) if isinstance(e, dict) else []:
                rows.append({"matcher": str(d.get("matcher") or ""), "command": copy.deepcopy(d.get("command", "")),
                             "timeout": d.get("timeout") or DEFAULT_TIMEOUT, "shell": str(d.get("shell") or ""),
                             "enabled": bool(d.get("enabled", True)), "name": str(d.get("name") or "")})
        events[ev] = rows
    return {"enabled": bool(h.get("enabled", True)),
            "allowed_project": [str(x) for x in (h.get("allowed_project") or [])], "events": events}


def settings_in(new: dict, out: dict) -> None:
    n = new.get("hooks")
    if not isinstance(n, dict):
        return
    h = dict(out.get("hooks") or {}) if isinstance(out.get("hooks"), dict) else {}
    if n.get("enabled") is not None:
        h["enabled"] = bool(n["enabled"])
    if n.get("allowed_project") is not None:
        h["allowed_project"] = list(dict.fromkeys(str(x) for x in n["allowed_project"] if str(x).strip()))
    if isinstance(n.get("events"), dict):
        for ev in EVENTS:
            h.pop(ev, None)
        for ev, rows in n["events"].items():
            if ev not in EVENTS:
                raise ValueError(f"未知的 Hook 事件：{ev}")
            clean = []
            for d in rows or []:
                d = {k: v for k, v in (d or {}).items() if k in HOOK_KEYS}
                parse_hook(ev, d)                 # 校验
                e: dict = {}
                if str(d.get("name") or "").strip():
                    e["name"] = str(d["name"]).strip()
                if str(d.get("matcher") or "").strip() and ev in MATCHER_EVENTS:
                    e["matcher"] = str(d["matcher"]).strip()
                cmd = d.get("command")
                e["command"] = [str(x) for x in cmd] if isinstance(cmd, list) else str(cmd).strip()
                t = float(d.get("timeout") or DEFAULT_TIMEOUT)
                if t != DEFAULT_TIMEOUT:
                    e["timeout"] = int(t) if t.is_integer() else t
                if str(d.get("shell") or "").strip() and not isinstance(cmd, list):
                    e["shell"] = str(d["shell"]).strip()
                if d.get("enabled") is False:
                    e["enabled"] = False
                clean.append(e)
            if clean:
                h[ev] = clean
    out["hooks"] = h
