"""配置：内置默认 ← ~/.ee/config.toml ← 项目 .ee/config.toml（越近越优先）。

API key：凭据存储（设置页保存的，见 ee/core/secrets.py：Windows 凭据管理器）> 旧版 config.toml 明文
providers.<名>.api_key（启动时会迁移进凭据存储）> 环境变量 api_key_env。
任何地方都不打印 key 本身（设置接口只返回来源和末四位）。
Windows 上 ~ 即 %USERPROFILE%；可用环境变量 EE_HOME 改位置（测试也用它）。
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ee.core.providers.profiles import Profile, profile_for

log = logging.getLogger("ee.config")

DEFAULTS: dict = {
    "default_model": "deepseek-flash",
    "max_steps": 50,
    "shell": "auto",                 # auto | pwsh | powershell | cmd | bash | gitbash
    "bash_timeout": 120,
    "background_shell": True,
    # 精简模式：full（默认）| lean（只有 read/write/edit/shell + 短系统提示词）；新会话生效，权限审批不变
    "profile": "full",
    "todo": True,                  # todo 计划清单工具
    "present_plan": True,          # 计划模式的 present_plan 工具（关掉时计划模式用文字给方案）
    "ask_user": True,              # ask_user 提问工具（D4-8）      # shell 工具可后台运行 + shell_output/shell_write/shell_kill（D4-6）
    # 上下文压缩：上下文用量 ≥ threshold × 模型窗口时自动压缩（回合开始时和回合内每步之前检查）
    # 磁盘快照（影子 git 仓库）：disk=false 回退内存快照；清理时每个会话保留最近 keep_days 天、最近 keep_turns 个回合
    "snapshot": {"disk": True, "keep_days": 14, "keep_turns": 100},
    "compact": {"auto": True, "threshold": 0.8, "keep_user_messages": 3, "keep_user_tokens": 20000},
    # 修剪旧工具输出（D5-10）：上下文 ≥ threshold × 窗口（低于压缩阈值）时把较早的大块工具结果换成占位，成批做一次。
    # 默认关闭：修剪会让后面整段历史的前缀缓存失效，DeepSeek（缓存命中价约 1/50）上往往更贵；需要时设 enabled = true
    "prune": {"enabled": False, "threshold": 0.6, "keep_turns": 2, "keep_tokens": 40000, "min_tokens": 20000,
              "min_output_tokens": 300},
    # AGENTS.md / CLAUDE.md：用户级 ~/.ee/ + 项目根→cwd 分层加载，总大小上限
    "agents_md": {"enabled": True, "max_bytes": 32768},
    # 防死循环：同调用连续 repeat_limit 次不执行并纠正，repeat_hard 次停止回合；无进展步数提醒/停止
    "loop_guard": {"repeat_limit": 3, "repeat_hard": 5, "no_progress_steps": 8, "no_progress_hard": 16},
    "permissions": {"mode": "default", "allow": [], "ask": [], "deny": []},
    # 网页搜索/抓取：search_backend = exa（免费，默认）| duckduckgo（免费）| deepseek（付费）| tavily | brave | off
    # search_fallback = 主后端出错时依次改用的后端；fetch_backend = direct（免费，默认）| auto | deepseek | off
    "web": {"search_backend": "exa", "search_fallback": ["duckduckgo"], "fetch_backend": "direct", "timeout": 20,
            "max_results": 8, "fetch_max_chars": 20000},
    # 子代理（task 工具）：默认最大步数与上限
    "subagent": {"enabled": True, "max_steps": 30, "max_steps_limit": 100, "max_parallel": 3, "worktree": True},
    # 长期记忆：~/.ee/memory.md + 工作区 .ee/memory.md，会话开始时注入（总大小上限）
    # inject = 会话开始时注入记忆；tool = 提供 memory 工具（enabled 为总开关）
    "memory": {"enabled": True, "workspace": True, "max_bytes": 8192, "inject": True, "tool": True},
    # 技能：~/.ee/skills、<项目>/.ee/skills（compat 时兼容 .claude/skills、.agents/skills）；清单总长上限 max_chars
    # claude_home = 是否读用户主目录的 ~/.claude/skills（默认关：那是 Claude Code 的技能，不一定适合 ee，也会让清单随机器变化）
    "skills": {"enabled": True, "compat": True, "claude_home": False, "disabled": [], "max_chars": 8000},
    # 仓库地图（repo_map 工具，tree-sitter）：auto_inject = 会话开始时把一小份地图固定注入（默认关，省 token）
    "repo_map": {"enabled": True, "auto_inject": False, "max_tokens": 1536, "inject_tokens": 1024,
                 "max_files": 3000, "time_limit": 20},
    # 推理强度（D5-9）：全局默认档位 "" | low | medium | high | max | off（DeepSeek 只有 "" / low / max / off；会话里可切换，按模型记住）
    "reasoning": {"effort": ""},
    # 按用途分模型（D5-9）：留空 = 跟随主模型。旧写法 [models.roles] 读取时会并进来
    "roles": {"title": "", "compact": "", "explore": "", "plan": "", "review": "", "commit": ""},
    # 用模型生成对话标题（用 roles.title 的模型；关掉则取第一条消息开头）
    "title": {"ai": False},
    # 回答风格（D5-9）：default | concise | ~/.ee/styles/<名>.md
    "style": {"default": "default"},
    # 花费上限（D5-9）：0 = 不限；action = warn（仅提醒）| pause（暂停并询问）；currency 跟单价币种
    "budget": {"session": 0, "daily": 0, "monthly": 0, "action": "warn", "warn_ratio": 0.8, "currency": "¥"},
    # 模型请求的网络：proxy = system（跟随系统/环境变量）| direct（直连）| 代理地址；超时单位秒
    # 命令沙箱（D5-8，仅 Windows 隔离写入）：enabled = shell 前台/后台命令在受限令牌里跑，只能写工作区和沙箱临时目录；
    # network=false = 用环境变量拦截联网（代理指向死端口、包管理器离线），尽力而为
    "sandbox": {"enabled": False, "network": True, "readonly_default": True,
                "readonly_network_default": False},
    "network": {"proxy": "system", "connect_timeout": 15, "read_timeout": 90, "retries": 2},
    "providers": {
        "deepseek": {"base_url": "https://api.deepseek.com", "api_key_env": "DEEPSEEK_API_KEY"},
    },
    "models": {
        "deepseek-flash": {"provider": "deepseek", "model": "deepseek-flash"},
        "deepseek-pro": {"provider": "deepseek", "model": "deepseek-v4-pro"},
    },
}


def ee_home() -> Path:
    env = os.environ.get("EE_HOME")
    return Path(env) if env else Path.home() / ".ee"


def user_ee_dirs() -> set[Path]:
    """所有“用户级 .ee 目录”的解析后路径，绝不能被当成项目级配置再叠一次。

    三个来源都要算：`ee_home()`（EE_HOME 可指到别处）、`Path.home()`、以及**环境变量
    推出的主目录**。三个不能合并成两个：只要工作区位于真实主目录之下（例如
    `C:\\Users\\alice\\proj\\foo`，很常见的布局），向上查找就会撞上真实主目录的
    `~/.ee/config.toml`；如果 `EE_HOME` 指向别处、而 `Path.home()` 又与真实主目录不同
    （测试里隔离、或 PYTHONHOME 之类），只按前两者排除就会漏掉它，用户级设置被
    静默当成项目级配置再叠一遍。
    """
    out: set[Path] = set()
    cands = [ee_home(), Path.home() / ".ee"]
    try:
        cands.append(Path(os.path.expanduser("~")) / ".ee")      # 不受 Path.home 补丁影响
    except (OSError, RuntimeError):
        pass
    for cand in cands:
        try:
            out.add(cand.resolve())
        except OSError:
            continue
    return out



def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


ROLES = ("title", "compact", "explore", "plan", "review", "commit")
ROLE_LABEL = {"title": "对话标题", "compact": "上下文压缩", "explore": "只读子代理（explore）", "plan": "计划模式",
              "review": "代码审查", "commit": "提交信息"}


def normalize_roles(data: dict) -> dict:
    """[models.roles]（旧写法）并进顶层 [roles]，并从模型表里拿掉，免得被当成一个模型。原地修改并返回。"""
    models = data.get("models")
    if isinstance(models, dict) and isinstance(models.get("roles"), dict) and "provider" not in models["roles"]:
        legacy = models.pop("roles")
        roles = dict(data.get("roles") or {})
        for k, v in legacy.items():
            if not str(roles.get(k) or "").strip():
                roles[k] = v
        data["roles"] = roles
    return data


# ------------------------------------------------------------------ 项目层配置过滤（只能收紧，不能放宽）
#
# 项目 .ee/config.toml 跟着仓库走，打开一个陌生/恶意仓库就会被读到。它不能：
#   - 改 provider / base_url / api_key / 代理，把用户的 key 或请求发到别处；
#   - 用 [mcp.servers] 等字段直接让 ee 启动程序（项目 MCP 只走 .mcp.json + 用户允许）；
#   - 放宽权限（更宽松的 mode、加 allow 规则、删掉用户的 ask/deny）。
# hooks 不在这里合并：项目 hooks 由 ee/core/hooks.py 直接读文件，走“设置 → Hooks 里允许”的信任机制。

PROJECT_DROP_TOP = ("providers", "hooks")
_MODE_RANK = {"read-only": 0, "plan": 0, "default": 1, "auto": 2}
_KEYISH = re.compile(r"(api_key|api_key_env|base_url|_url|headers|proxy)$")

# 最近一次 load_config 时各项目配置里被丢弃的字段：{项目配置路径: [字段, ...]}（设置页展示）
PROJECT_IGNORED: dict[str, list[str]] = {}


def sanitize_project_layer(layer: dict, user_cfg: dict) -> tuple[dict, list[str]]:
    """过滤项目层配置：返回 (可以安全合并的部分, 被丢弃的字段名列表)。不修改入参。

    user_cfg 是合并项目层之前的配置（内置默认 + 用户级），用来判断“是否比用户更宽松”。
    """
    clean = copy.deepcopy(layer)
    dropped: list[str] = []
    for k in PROJECT_DROP_TOP:
        if k in clean:
            clean.pop(k)
            dropped.append(k)

    def drop_keyish(section: str) -> None:
        t = clean.get(section)
        if isinstance(t, dict):
            for k in [k for k in t if _KEYISH.search(str(k))]:
                t.pop(k)
                dropped.append(f"{section}.{k}")

    drop_keyish("web")
    drop_keyish("network")

    models = clean.get("models")
    if "models" in clean and not isinstance(models, dict):
        clean.pop("models")
        dropped.append("models")
    elif isinstance(models, dict):              # 只能在用户已有的 provider 上定义/调整模型，不能带自定义请求头
        known = set((user_cfg.get("providers") or {}).keys())
        for name in list(models):
            m = models[name]
            if not isinstance(m, dict) or ("provider" in m and str(m["provider"]) not in known):
                models.pop(name)
                dropped.append(f"models.{name}")
                continue
            if "headers" in m:
                m.pop("headers")
                dropped.append(f"models.{name}.headers")
        if not models:
            clean.pop("models")

    perms = clean.get("permissions")
    if "permissions" in clean and not isinstance(perms, dict):
        clean.pop("permissions")
        dropped.append("permissions")
    elif isinstance(perms, dict):
        up = user_cfg.get("permissions") or {}
        if "mode" in perms:
            want, have = str(perms["mode"]), str(up.get("mode") or "default")
            if want not in _MODE_RANK or _MODE_RANK[want] > _MODE_RANK.get(have, 1):
                perms.pop("mode")
                dropped.append("permissions.mode")
        if "allow" in perms:
            perms.pop("allow")
            dropped.append("permissions.allow")
        for k in ("ask", "deny"):               # 只能追加规则；deep_merge 会整个替换列表，所以这里先并上用户的
            mine = list(up.get(k) or [])
            if k in perms:
                extra = perms[k] if isinstance(perms[k], list) else []
                perms[k] = mine + [r for r in extra if r not in mine]
            elif mine:
                perms[k] = mine
        for k in [k for k in perms if k not in ("mode", "ask", "deny")]:
            perms.pop(k)
            dropped.append(f"permissions.{k}")

    mcp = clean.get("mcp")
    if "mcp" in clean and not isinstance(mcp, dict):
        clean.pop("mcp")
        dropped.append("mcp")
    elif isinstance(mcp, dict):
        for k in ("servers", "allowed_project"):
            if k in mcp:
                mcp.pop(k)
                dropped.append(f"mcp.{k}")
        um = user_cfg.get("mcp") or {}
        for k in ("enabled", "project_json"):   # 只能关，不能开
            if k in mcp and bool(mcp[k]) and um.get(k) is False:
                mcp.pop(k)
                dropped.append(f"mcp.{k}")
    sb = clean.get("sandbox")
    if "sandbox" in clean and not isinstance(sb, dict):
        clean.pop("sandbox")
        dropped.append("sandbox")
    elif isinstance(sb, dict):
        us = user_cfg.get("sandbox") or {}
        # 沙箱只能开不能关；联网只能关不能开
        if "enabled" in sb and not bool(sb["enabled"]) and bool(us.get("enabled", False)):
            sb.pop("enabled")
            dropped.append("sandbox.enabled")
        if "network" in sb and bool(sb["network"]) and not bool(us.get("network", True)):
            sb.pop("network")
            dropped.append("sandbox.network")
        # 只读模式默认开沙箱 / 默认禁网这两条，项目层只能开不能关（关掉等于削弱隔离）
        if "readonly_default" in sb and not bool(sb["readonly_default"]):
            sb.pop("readonly_default")
            dropped.append("sandbox.readonly_default")
        if "readonly_network_default" in sb and bool(sb["readonly_network_default"]):
            sb.pop("readonly_network_default")
            dropped.append("sandbox.readonly_network_default")
        for k in list(sb):
            if k not in ("enabled", "network", "readonly_default", "readonly_network_default"):
                sb.pop(k)
                dropped.append(f"sandbox.{k}")
    bud = clean.get("budget")
    if "budget" in clean and not isinstance(bud, dict):
        clean.pop("budget")
        dropped.append("budget")
    elif isinstance(bud, dict):
        ub = user_cfg.get("budget") or {}
        for k in ("session", "daily", "monthly"):
            if k not in bud:
                continue
            try:
                want, have = float(bud[k]), float(ub.get(k) or 0)
            except (TypeError, ValueError):
                bud.pop(k)
                dropped.append(f"budget.{k}")
                continue
            # 0 表示“不限”。用户设了正数上限时，项目不能清零，也不能调大。
            if have > 0 and (want <= 0 or want > have):
                bud.pop(k)
                dropped.append(f"budget.{k}")
        if "action" in bud:
            rank = {"warn": 0, "pause": 1}
            if rank.get(str(bud["action"]), -1) < rank.get(str(ub.get("action") or "warn"), 0):
                bud.pop("action")
                dropped.append("budget.action")
        for k in [k for k in bud if k not in ("session", "daily", "monthly", "action")]:
            # warn_ratio / currency / unattended：项目层没有可安全放宽的方向，一律不采纳
            bud.pop(k)
            dropped.append(f"budget.{k}")
        if not bud:
            clean.pop("budget")
    return clean, dropped


def _read_toml_layer(p: Path) -> dict:
    """读项目层 TOML：容忍 UTF-8 BOM（Windows 记事本常见）和 CRLF。"""
    return tomllib.loads(p.read_bytes().decode("utf-8-sig"))


@dataclass
class ModelSpec:
    alias: str
    provider: str
    model: str
    base_url: str
    api_key_env: str
    profile: Profile
    headers: dict = field(default_factory=dict)
    stored_key: str = ""

    def api_key(self) -> str | None:
        return self.key_source()[1]

    def key_source(self) -> tuple[str, str | None]:
        """(来源, key)：来源 = store | plain | env | ""。"""
        from ee.core import secrets
        v = secrets.get(secrets.provider_key_name(self.provider)) if self.provider else None
        if v:
            return "store", v
        if self.stored_key:
            return "plain", self.stored_key
        env = os.environ.get(self.api_key_env) if self.api_key_env else None
        if env:
            secrets.remember_env_keys([self.api_key_env])
            return "env", env
        return "", None

    def key_hint(self) -> str:
        """缺 key 时给用户看的说明。"""
        if self.api_key_env:
            return f"环境变量 {self.api_key_env} 未设置，设置页里也没有保存 key"
        return "设置页里没有保存 key，也没有配置 key 环境变量"


@dataclass
class Config:
    data: dict
    cwd: Path
    sources: list[Path] = field(default_factory=list)
    project_ignored: list[str] = field(default_factory=list)   # 项目配置里因安全原因被忽略的字段

    @property
    def default_model(self) -> str:
        return self.data["default_model"]

    @property
    def max_steps(self) -> int:
        return int(self.data.get("max_steps", 50))

    @property
    def permissions(self) -> dict:
        return self.data.get("permissions", {})

    def model_names(self) -> list[str]:
        return list(self.data.get("models", {}).keys())

    def role_model(self, role: str) -> str | None:
        """某个用途配置的模型名；没配、配了不存在的模型都返回 None（跟随主模型）。"""
        name = str((self.data.get("roles") or {}).get(role) or "").strip()
        if not name:
            return None
        providers = self.data.get("providers") or {}
        if name in self.data.get("models", {}) or ("/" in name and name.split("/", 1)[0] in providers):
            return name
        return None

    def resolve_model(self, name: str | None = None) -> ModelSpec:
        name = name or self.default_model
        models = self.data.get("models", {})
        providers = self.data.get("providers", {})
        if name in models:
            m = dict(models[name])
            prov_name = m.pop("provider", None) or next(iter(providers), "deepseek")
            model_id = m.pop("model", name)
        elif "/" in name and name.split("/", 1)[0] in providers:
            prov_name, model_id = name.split("/", 1)
            m = {}
        else:
            prov_name = models.get(self.default_model, {}).get("provider") or next(iter(providers), "deepseek")
            model_id, m = name, {}
        prov = providers.get(prov_name)
        if prov is None:
            raise ValueError(f"模型 {name} 引用了未定义的 provider: {prov_name}")
        family = m.pop("family", None)
        headers = {**prov.get("headers", {}), **m.pop("headers", {})}
        profile = profile_for(model_id, family=family, overrides=m)
        return ModelSpec(alias=name, provider=prov_name, model=model_id,
                         base_url=prov.get("base_url", ""), api_key_env=prov.get("api_key_env", ""),
                         profile=profile, headers=headers, stored_key=str(prov.get("api_key", "") or ""))


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)   # JSON 字符串转义与 TOML basic string 兼容


def write_setting(dotted: str, value, path: Path | None = None) -> Path:
    """把一个设置写回 config.toml（默认 ~/.ee/config.toml），尽量保留原文件的其他内容和注释。"""
    path = path or (ee_home() / "config.toml")
    section, key = dotted.rsplit(".", 1) if "." in dotted else ("", dotted)
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    new_line = f"{key} = {_toml_value(value)}"
    cur, sec_start, sec_end, first_header = "", None, None, None
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("[") and not s.startswith("[["):
            if first_header is None:
                first_header = i
            if cur == section and sec_start is not None and sec_end is None:
                sec_end = i
            cur = s.strip("[]").strip()
            if cur == section:
                sec_start = i
            continue
        if cur == section and key_re.match(line):
            lines[i] = new_line
            break
    else:
        if section == "":
            idx = first_header if first_header is not None else len(lines)
            while idx > 0 and not lines[idx - 1].strip():
                idx -= 1
            lines.insert(idx, new_line)
        elif sec_start is not None:
            idx = sec_end if sec_end is not None else len(lines)
            while idx > sec_start + 1 and not lines[idx - 1].strip():
                idx -= 1
            lines.insert(idx, new_line)
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines += [f"[{section}]", new_line]
    from ee.core.atomic import write_text as atomic_write
    atomic_write(path, "\n".join(lines) + "\n")       # 临时文件 + 替换：写到一半崩溃也不会留下半截配置
    return path


# ------------------------------------------------------------------ 整文件读写（桌面端设置页用）

def user_config_path() -> Path:
    return ee_home() / "config.toml"


def load_user_file(path: Path | None = None) -> dict:
    """读用户级 config.toml；读之前先跑版本迁移（见 ee/core/migrate.py），迁移失败时内存里照样迁移。"""
    from ee.core.migrate import migrate_data, migrate_user_file

    path = path or user_config_path()
    if not path.is_file():
        return {}
    migrate_user_file(path)
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return migrate_data(data)[0]


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(k: str) -> str:
    return k if _BARE_KEY.match(k) else json.dumps(k, ensure_ascii=False)


def dump_toml(data: dict) -> str:
    """最小 TOML 序列化：标量/数组写成 key = value，dict 写成 [表]（递归），dict 列表写成 [[表数组]]。够 config.toml 用。"""
    out: list[str] = []

    def aot(v) -> bool:            # 表数组 [[a.b]]（如 hooks 的 [[hooks.PreToolUse]]）
        return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)

    def scalar(v) -> bool:
        return not isinstance(v, dict) and v is not None and not aot(v)

    def emit(table: dict, path: list[str], header: bool = False) -> None:
        simple = [(k, v) for k, v in table.items() if scalar(v)]
        subs = [(k, v) for k, v in table.items() if isinstance(v, dict)]
        arrays = [(k, v) for k, v in table.items() if aot(v)]
        if header:
            if out:
                out.append("")
            out.append("[[" + ".".join(_toml_key(p) for p in path) + "]]")
        elif path and (simple or not (subs or arrays)):
            if out:
                out.append("")
            out.append("[" + ".".join(_toml_key(p) for p in path) + "]")
        for k, v in simple:
            out.append(f"{_toml_key(k)} = {_toml_value(v)}")
        for k, v in subs:
            emit(v, path + [k])
        for k, v in arrays:
            for item in v:
                emit(item, path + [k], header=True)

    emit(data, [])
    return "\n".join(out) + "\n"


def save_user_file(data: dict, path: Path | None = None) -> Path:
    """整份写回用户级 config.toml（旧文件先备份为 config.toml.bak；注释不保留）。"""
    path = path or user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        try:                                    # 备份里绝不留明文 key
            from ee.core.secrets import strip_secret_lines
            (path.parent / (path.name + ".bak")).write_text(
                strip_secret_lines(path.read_text(encoding="utf-8")), encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            pass
    from ee.core.atomic import write_text as atomic_write
    atomic_write(path, dump_toml(data))
    return path


def load_config(cwd: Path | None = None) -> Config:
    cwd = Path(cwd or os.getcwd())
    data = copy.deepcopy(DEFAULTS)
    sources: list[Path] = []
    ignored: list[str] = []
    user_cfg = ee_home() / "config.toml"
    user_dirs = user_ee_dirs()          # 用户级 .ee 目录（含真实主目录），绝不当成项目级
    candidates = [user_cfg]
    proj = None
    cur = cwd.resolve()
    for d in [cur, *cur.parents]:
        p = d / ".ee" / "config.toml"
        try:
            if p.resolve().parent in user_dirs:
                continue                # 用户级配置，不是项目配置；继续往上但别叠它
        except OSError:
            continue
        if p.is_file():
            proj = p
            break
        if (d / ".git").exists():
            break
    if proj:
        candidates.append(proj)
    for p in candidates:
        if p.is_file():
            try:
                same_as_user = p.resolve() == user_cfg.resolve()
            except OSError:
                same_as_user = False
            if same_as_user:
                layer = normalize_roles(dict(load_user_file(p)))
            else:
                layer = normalize_roles(dict(_read_toml_layer(p)))
                layer, dropped = sanitize_project_layer(layer, data)
                PROJECT_IGNORED[str(p)] = dropped
                if dropped:
                    ignored += dropped
                    log.warning("项目配置 %s 里的这些字段只能在用户级配置里设置，已忽略：%s", p, ", ".join(dropped))
            data = deep_merge(data, layer)
            sources.append(p)
    return Config(data=data, cwd=cwd, sources=sources, project_ignored=ignored)
