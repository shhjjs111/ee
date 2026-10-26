"""配置：内置默认 ← ~/.ee/config.toml ← 项目 .ee/config.toml（越近越优先）。

API key：凭据存储（设置页保存的，见 ee/core/secrets.py：Windows 凭据管理器）> 旧版 config.toml 明文
providers.<名>.api_key（启动时会迁移进凭据存储）> 环境变量 api_key_env。
任何地方都不打印 key 本身（设置接口只返回来源和末四位）。
Windows 上 ~ 即 %USERPROFILE%；可用环境变量 EE_HOME 改位置（测试也用它）。
"""
from __future__ import annotations

import copy
import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ee.core.providers.profiles import Profile, profile_for

DEFAULTS: dict = {
    "default_model": "deepseek-flash",
    "max_steps": 50,
    "shell": "auto",                 # auto | pwsh | powershell | cmd | bash | gitbash
    "bash_timeout": 120,
    "background_shell": True,      # shell 工具可后台运行 + shell_output/shell_write/shell_kill（D4-6）
    # 上下文压缩：上下文用量 ≥ threshold × 模型窗口时自动压缩（回合开始时和回合内每步之前检查）
    "compact": {"auto": True, "threshold": 0.8, "keep_user_messages": 3, "keep_user_tokens": 20000},
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
    "subagent": {"enabled": True, "max_steps": 30, "max_steps_limit": 100},
    # 长期记忆：~/.ee/memory.md + 工作区 .ee/memory.md，会话开始时注入（总大小上限）
    "memory": {"enabled": True, "workspace": True, "max_bytes": 8192},
    # 模型请求的网络：proxy = system（跟随系统/环境变量）| direct（直连）| 代理地址；超时单位秒
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


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
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
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(dump_toml(data), encoding="utf-8")
    os.replace(tmp, path)
    return path


def load_config(cwd: Path | None = None) -> Config:
    cwd = Path(cwd or os.getcwd())
    data = copy.deepcopy(DEFAULTS)
    sources: list[Path] = []
    user_cfg = ee_home() / "config.toml"
    candidates = [user_cfg]
    proj = None
    cur = cwd.resolve()
    for d in [cur, *cur.parents]:
        p = d / ".ee" / "config.toml"
        if p.is_file() and p.resolve() != user_cfg.resolve():
            proj = p
            break
        if (d / ".git").exists():
            break
    if proj:
        candidates.append(proj)
    for p in candidates:
        if p.is_file():
            if p is user_cfg:
                layer = load_user_file(p)
            else:
                with open(p, "rb") as f:
                    layer = tomllib.load(f)
            data = deep_merge(data, layer)
            sources.append(p)
    return Config(data=data, cwd=cwd, sources=sources)
