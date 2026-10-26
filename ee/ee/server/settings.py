"""设置页 <-> ~/.ee/config.toml。

- get：内置默认 + 用户级 config.toml 合并后的值（不含项目级覆盖；项目级文件路径单独列出）。
  API key 永远不回传原文，只给 api_key_set / api_key_preview（末四位）/ api_key_source（store|plain|env|""）
  / env_present，以及 secrets 存储说明。
- key 一律存进凭据存储（ee/core/secrets.py，Windows 凭据管理器），config.toml 里不再写明文。
- set：在用户级文件原内容上覆盖界面管理的键（未知键原样保留），整份写回（旧文件备份为 .bak）。
"""
from __future__ import annotations

import copy
import os

from ee.config import DEFAULTS, ROLES, deep_merge, load_user_file, normalize_roles, save_user_file, user_config_path
from ee.core import queue as Q
from ee.core import reasoning as RZ
from ee.core import secrets
from ee.core.migrate import CURRENT_VERSION
from ee.core.permissions import MODES
from ee.core.providers.profiles import FAMILY_ORDER, profile_for
from ee.tools.shellenv import SHELL_CHOICES
from ee.tools.web import FETCH_BACKENDS, KEY_ENV, SEARCH_BACKENDS
from ee.tools.web import deepseek_key as web_deepseek_key

THEMES = ["system", "dark", "light"]
BUDGET_ACTIONS = ["warn", "pause"]
NOTIFY_MODES = ["never", "unfocused", "always"]
FAMILIES = ["auto", *FAMILY_ORDER, "default"]
MODEL_KEYS = {"provider", "model", "family", "context_window", "price", "vision"}
PROVIDER_KEYS = {"base_url", "api_key_env", "api_key"}


def _preview(key: str) -> str:
    return ("…" + key[-4:]) if len(key) > 8 else ("已设置" if key else "")


def effective() -> dict:
    return deep_merge(DEFAULTS, normalize_roles(load_user_file()))


def get_settings(project_sources: list[str] | None = None) -> dict:
    data = effective()
    providers = {}
    for name, p in (data.get("providers") or {}).items():
        stored = secrets.get(secrets.provider_key_name(name)) or ""
        plain = str(p.get("api_key") or "")
        key = stored or plain
        env = str(p.get("api_key_env") or "")
        env_present = bool(env and os.environ.get(env))
        providers[name] = {"base_url": p.get("base_url", ""), "api_key_env": env, "api_key_set": bool(key),
                           "api_key_preview": _preview(key), "env_present": env_present,
                           "api_key_source": "store" if stored else "plain" if plain else "env" if env_present else "",
                           "builtin": name in DEFAULTS["providers"]}
    models = {}
    for name, m in (data.get("models") or {}).items():
        prof = profile_for(m.get("model", name), family=m.get("family"))
        base = profile_for(m.get("model", name))
        price = m.get("price") or {}
        models[name] = {
            "provider": m.get("provider", ""), "model": m.get("model", name), "family": m.get("family") or "auto",
            "detected_family": base.family,
            "context_window": m.get("context_window"),
            "price": {k: price.get(k) for k in ("input", "output", "cache_read", "currency")},
            "default_price": {"input": prof.price.input, "output": prof.price.output,
                              "cache_read": prof.price.cache_read, "currency": prof.price.currency},
            "default_context_window": prof.context_window,
            "vision": m.get("vision"),                  # None = 按模型档案自动
            "default_vision": prof.vision,
            "builtin": name in DEFAULTS["models"],
        }
    return {
        "path": str(user_config_path()),
        "project_sources": project_sources or [],
        "settings": {
            "default_model": data.get("default_model"),
            "shell": data.get("shell", "auto"),
            "max_steps": int(data.get("max_steps", 50)),
            "bash_timeout": int(data.get("bash_timeout", 120)),
            "permissions": {"mode": (data.get("permissions") or {}).get("mode", "default")},
            "ui": _ui_out(data.get("ui") or {}),
            "network": _network_out(data.get("network") or {}),
            **_d2_out(data),
            "mcp": _mcp_out(data),
            "hooks": _hooks_out(data),
            "queue": Q.mode_settings(data),             # 运行中排队消息的送入方式（one / all）
            **_answer_out(data),
            "secrets": _secrets_out(),
            "providers": providers,
            "models": models,
        },
        "choices": {"shells": SHELL_CHOICES, "modes": MODES, "families": FAMILIES, "themes": THEMES,
                    "notify_modes": NOTIFY_MODES, "search_backends": SEARCH_BACKENDS, "fetch_backends": FETCH_BACKENDS,
                    "efforts": list(RZ.LEVELS), "roles": list(ROLES), "budget_actions": list(BUDGET_ACTIONS),
                    "styles": _styles_choices()},
    }


# ------------------------------------------------------------------ 回答与花费（D5-9）

def _styles_choices() -> list[dict]:
    from ee.config import ee_home
    from ee.core import styles as ST
    return [x.summary() for x in ST.available(ee_home())]


def _answer_out(data: dict) -> dict:
    from ee.core.budget import settings as budget_settings
    b = budget_settings(data)
    b.pop("unattended", None)
    roles = data.get("roles") or {}
    return {
        "reasoning": {"effort": RZ.normalize((data.get("reasoning") or {}).get("effort")) or ""},
        "roles": {r: str(roles.get(r) or "") for r in ROLES},
        "title": {"ai": bool((data.get("title") or {}).get("ai", False))},
        "style": {"default": str((data.get("style") or {}).get("default") or "default")},
        "budget": b,
    }


def _answer_in(new: dict, out: dict, final_models: dict) -> None:
    from ee.config import ee_home
    from ee.core import styles as ST
    if isinstance(new.get("reasoning"), dict) and "effort" in new["reasoning"]:
        lv = RZ.normalize(new["reasoning"]["effort"])
        if lv is None:
            raise ValueError(f"未知推理强度：{new['reasoning']['effort']}")
        out.setdefault("reasoning", {})["effort"] = lv
    if isinstance(new.get("roles"), dict):
        roles = dict(out.get("roles") or {})
        for r, v in new["roles"].items():
            if r not in ROLES:
                raise ValueError(f"未知用途：{r}")
            v = str(v or "").strip()
            if v and v not in final_models:
                raise ValueError(f"用途「{r}」选的模型 {v} 不在模型列表里")
            roles[r] = v
        out["roles"] = roles
        if isinstance(out.get("models"), dict):
            out["models"].pop("roles", None)       # 旧写法 [models.roles] 已并进 [roles]
    if isinstance(new.get("title"), dict) and new["title"].get("ai") is not None:
        out.setdefault("title", {})["ai"] = bool(new["title"]["ai"])
    if isinstance(new.get("style"), dict) and new["style"].get("default") is not None:
        sid = str(new["style"]["default"] or "default").strip() or "default"
        if not ST.exists(ee_home(), sid):
            raise ValueError(f"没有这个回答风格：{sid}")
        out.setdefault("style", {})["default"] = sid
    if isinstance(new.get("budget"), dict):
        n = new["budget"]
        b = dict(out.get("budget") or {})
        for k in ("session", "daily", "monthly"):
            if k in n:
                v = _num(n.get(k))
                v = 0 if v is None else v
                if v < 0 or v > 1_000_000:
                    raise ValueError("花费上限必须在 0～1000000 之间（0 = 不限）")
                b[k] = v
        if n.get("action") is not None:
            if n["action"] not in BUDGET_ACTIONS:
                raise ValueError(f"未知的超限处理方式：{n['action']}")
            b["action"] = n["action"]
        if n.get("warn_ratio") not in (None, ""):
            r = float(n["warn_ratio"])
            if not 0.1 <= r <= 0.99:
                raise ValueError("提醒比例必须在 0.1～0.99 之间")
            b["warn_ratio"] = r
        if n.get("currency") not in (None, ""):
            b["currency"] = str(n["currency"]).strip()[:4]
        out["budget"] = b


def _ui_out(ui: dict) -> dict:
    """界面设置：主题、桌面通知（never|unfocused|always）、关窗后托盘常驻（D4-5）。"""
    notify = ui.get("notify", "unfocused")
    return {"theme": ui.get("theme", "system"),
            "notify": notify if notify in NOTIFY_MODES else "unfocused",
            "notify_approval": bool(ui.get("notify_approval", True)),
            "close_to_tray": bool(ui.get("close_to_tray", True))}


def _secrets_out() -> dict:
    try:
        return secrets.info()
    except Exception as e:  # noqa: BLE001
        return {"backend": "error", "label": f"凭据存储不可用：{type(e).__name__}", "secure": False}


def _key_out(w: dict, name: str) -> dict:
    stored = secrets.get(secrets.web_key_name(name)) or ""
    plain = str(w.get(f"{name}_api_key") or "")
    key = stored or plain
    env = str(w.get(f"{name}_api_key_env") or KEY_ENV[name])
    env_present = bool(os.environ.get(env))
    return {"api_key_set": bool(key), "api_key_preview": _preview(key), "api_key_env": env,
            "api_key_source": "store" if stored else "plain" if plain else "env" if env_present else "",
            "env_present": env_present}


def _d2_out(data: dict) -> dict:
    """网页搜索 / 子代理 / 记忆 三组设置（key 只回传是否已设置和末四位）。"""
    w = {**DEFAULTS.get("web", {}), **(data.get("web") or {})}
    sa = {**DEFAULTS.get("subagent", {}), **(data.get("subagent") or {})}
    mem = {**DEFAULTS.get("memory", {}), **(data.get("memory") or {})}
    return {
        "web": {"search_backend": w.get("search_backend", "exa"), "fetch_backend": w.get("fetch_backend", "direct"),
                "search_fallback": [b for b in (w.get("search_fallback") or []) if b in SEARCH_BACKENDS],
                "timeout": w.get("timeout", 20), "max_results": w.get("max_results", 8),
                "deepseek_key_available": bool(web_deepseek_key(data)),
                "keys": {"tavily": _key_out(w, "tavily"), "brave": _key_out(w, "brave")}},
        "subagent": {"enabled": bool(sa.get("enabled", True)), "max_steps": int(sa.get("max_steps", 30)),
                     "max_parallel": int(sa.get("max_parallel", 3) or 3), "worktree": bool(sa.get("worktree", True))},
        "memory": {"enabled": bool(mem.get("enabled", True)), "workspace": bool(mem.get("workspace", True)),
                   "max_bytes": int(mem.get("max_bytes", 8192)),
                   "inject": bool(mem.get("inject", True)), "tool": bool(mem.get("tool", True))},
        "skills": _skills_out(data),
        "profile": _agent_profile(data),                       # 精简模式 full / lean（新对话生效）
        "todo": bool(data.get("todo", True)),
        "present_plan": bool(data.get("present_plan", True)),
    }


def _skills_out(data: dict) -> dict:
    from ee.core.skills import settings as skills_settings
    sk = skills_settings(data)
    return {"enabled": bool(sk.get("enabled", True)), "compat": bool(sk.get("compat", True)),
            "disabled": list(sk["disabled"]), "max_chars": int(sk.get("max_chars") or 8000)}


def _skills_in(n: dict, out: dict) -> None:
    """[skills]：enabled / compat 开关、disabled 名字列表、清单长度上限 max_chars。"""
    sk = dict(out.get("skills") or {})
    for k in ("enabled", "compat"):
        if n.get(k) is not None:
            sk[k] = bool(n[k])
    if n.get("disabled") is not None:
        if not isinstance(n["disabled"], list):
            raise ValueError("skills.disabled 必须是技能名列表")
        sk["disabled"] = sorted({str(x).strip() for x in n["disabled"] if str(x).strip()})
    if n.get("max_chars") not in (None, ""):
        v = int(float(n["max_chars"]))
        if not 1000 <= v <= 50000:
            raise ValueError("技能清单长度上限必须在 1000～50000 字符之间")
        sk["max_chars"] = v
    out["skills"] = sk


def _agent_profile(data: dict) -> str:
    from ee.tools import agent_profile
    return agent_profile(data)


def _d2_in(new: dict, out: dict, deletes: list | None = None) -> None:
    from ee.tools import AGENT_PROFILES
    if new.get("profile") is not None:
        v = str(new["profile"]).strip().lower()
        if v not in AGENT_PROFILES:
            raise ValueError(f"未知模式：{new['profile']}（可选 full / lean）")
        out["profile"] = v
    for k in ("todo", "present_plan"):
        if new.get(k) is not None:
            out[k] = bool(new[k])
    if isinstance(new.get("web"), dict):
        n = new["web"]
        w = dict(out.get("web") or {})
        if n.get("search_backend") is not None:
            if n["search_backend"] not in SEARCH_BACKENDS:
                raise ValueError(f"未知搜索后端：{n['search_backend']}")
            w["search_backend"] = n["search_backend"]
        if n.get("fetch_backend") is not None:
            if n["fetch_backend"] not in FETCH_BACKENDS:
                raise ValueError(f"未知抓取方式：{n['fetch_backend']}")
            w["fetch_backend"] = n["fetch_backend"]
        if n.get("search_fallback") is not None:
            fb = n["search_fallback"]
            if isinstance(fb, str):
                fb = [x.strip() for x in fb.split(",") if x.strip()]
            fb = [str(x) for x in fb]
            bad = [x for x in fb if x not in SEARCH_BACKENDS or x == "off"]
            if bad:
                raise ValueError(f"未知回退搜索后端：{'、'.join(bad)}")
            w["search_fallback"] = list(dict.fromkeys(fb))
        for k, lo, hi in (("timeout", 3, 120), ("max_results", 1, 20)):
            if n.get(k) not in (None, ""):
                v = int(float(n[k]))
                if not lo <= v <= hi:
                    raise ValueError(f"web.{k} 必须在 {lo}～{hi} 之间")
                w[k] = v
        for name, kv in (n.get("keys") or {}).items():
            if name not in KEY_ENV or not isinstance(kv, dict):
                continue
            if kv.get("api_key") is not None:          # "" = 清除；None/缺省 = 不变
                if str(kv["api_key"]).strip():
                    w[f"{name}_api_key"] = str(kv["api_key"]).strip()
                else:
                    w.pop(f"{name}_api_key", None)
                    if deletes is not None:
                        deletes.append(secrets.web_key_name(name))
            if kv.get("api_key_env") is not None:
                env = str(kv["api_key_env"]).strip()
                if env and env != KEY_ENV[name]:
                    w[f"{name}_api_key_env"] = env
                else:
                    w.pop(f"{name}_api_key_env", None)
        out["web"] = w
    if isinstance(new.get("subagent"), dict):
        n = new["subagent"]
        sa = dict(out.get("subagent") or {})
        if n.get("enabled") is not None:
            sa["enabled"] = bool(n["enabled"])
        if n.get("max_steps") not in (None, ""):
            v = int(float(n["max_steps"]))
            if not 1 <= v <= 100:
                raise ValueError("子代理最大步数必须在 1～100 之间")
            sa["max_steps"] = v
        if n.get("max_parallel") not in (None, ""):
            v = int(float(n["max_parallel"]))
            if not 1 <= v <= 8:
                raise ValueError("子代理同时运行数必须在 1～8 之间")
            sa["max_parallel"] = v
        if n.get("worktree") is not None:
            sa["worktree"] = bool(n["worktree"])
        out["subagent"] = sa
    if isinstance(new.get("memory"), dict):
        n = new["memory"]
        mem = dict(out.get("memory") or {})
        for k in ("enabled", "workspace", "inject", "tool"):
            if n.get(k) is not None:
                mem[k] = bool(n[k])
        if n.get("max_bytes") not in (None, ""):
            v = int(float(n["max_bytes"]))
            if not 512 <= v <= 65536:
                raise ValueError("记忆大小上限必须在 512～65536 字节之间")
            mem["max_bytes"] = v
        out["memory"] = mem
    if isinstance(new.get("skills"), dict):
        _skills_in(new["skills"], out)


SERVER_KEYS = ("command", "args", "env", "cwd", "url", "headers", "type", "enabled", "trusted")


def _mcp_out(data: dict) -> dict:
    from ee.mcp.config import mcp_settings
    m = mcp_settings(data)
    servers = {}
    for name, d in (m.get("servers") or {}).items():
        d = d or {}
        servers[name] = {"command": d.get("command", ""), "args": list(d.get("args") or []),
                         "env": dict(d.get("env") or {}), "cwd": d.get("cwd", ""), "url": d.get("url", ""),
                         "headers": dict(d.get("headers") or {}), "type": d.get("type", ""),
                         "enabled": bool(d.get("enabled", True)), "trusted": bool(d.get("trusted", False))}
    return {"enabled": bool(m.get("enabled", True)), "startup_timeout": m.get("startup_timeout", 20),
            "tool_timeout": m.get("tool_timeout", 120), "project_json": bool(m.get("project_json", True)),
            "allowed_project": list(m.get("allowed_project") or []), "servers": servers}


def _mcp_in(new: dict, out: dict) -> None:
    n = new.get("mcp")
    if not isinstance(n, dict):
        return
    from ee.mcp.config import parse_server
    m = dict(out.get("mcp") or {})
    for k in ("enabled", "project_json"):
        if n.get(k) is not None:
            m[k] = bool(n[k])
    for k, lo, hi in (("startup_timeout", 3, 300), ("tool_timeout", 5, 3600)):
        v = _num(n.get(k))
        if v is not None:
            if not lo <= v <= hi:
                raise ValueError(f"mcp.{k} 应在 {lo}–{hi} 秒之间")
            m[k] = int(v)
    if n.get("allowed_project") is not None:
        m["allowed_project"] = [str(x) for x in n["allowed_project"] if str(x).strip()]
    if isinstance(n.get("servers"), dict):
        servers = {}
        for name, d in n["servers"].items():
            name = str(name).strip()
            d = {k: v for k, v in (d or {}).items() if k in SERVER_KEYS}
            for k in ("command", "cwd", "url", "type"):
                if k in d:
                    d[k] = str(d[k] or "").strip()
                    if not d[k]:
                        d.pop(k)
            for k in ("env", "headers"):
                if k in d:
                    d[k] = {str(a).strip(): str(b) for a, b in (d[k] or {}).items() if str(a).strip()}
                    if not d[k]:
                        d.pop(k)
            if "args" in d:
                a = d["args"]
                d["args"] = a.split() if isinstance(a, str) else [str(x) for x in (a or [])]
                if not d["args"]:
                    d.pop("args")
            for k in ("enabled", "trusted"):
                if k in d:
                    d[k] = bool(d[k])
            parse_server(name, d)                  # 校验（名字、command/url）
            servers[name] = d
        m["servers"] = servers
    out["mcp"] = m


def _hooks_out(data: dict) -> dict:
    from ee.core.hooks import settings_out
    return settings_out(data)


def _hooks_in(new: dict, out: dict) -> None:
    from ee.core.hooks import settings_in
    settings_in(new, out)


def _network_out(n: dict) -> dict:
    proxy = str(n.get("proxy") or "system").strip()
    mode = proxy.lower() if proxy.lower() in ("system", "direct") else "custom"
    return {"proxy_mode": mode, "proxy_url": proxy if mode == "custom" else "",
            "connect_timeout": n.get("connect_timeout", 15), "read_timeout": n.get("read_timeout", 90),
            "retries": n.get("retries", 2), "system_proxy": _system_proxy()}


def _system_proxy() -> str:
    """当前“跟随系统”会用到的代理（给设置页显示）。"""
    try:
        from ee.core.providers.net import plan
        return plan("https://api.deepseek.com", {"proxy": "system"}).describe
    except Exception:  # noqa: BLE001
        return ""


def _num(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"不是数字：{v}")
    return int(f) if f.is_integer() and not isinstance(v, float) else f


def set_settings(new: dict) -> dict:
    """new 的结构同 get_settings()['settings']；缺的键保持不变。返回新的 get_settings()。"""
    user = normalize_roles(load_user_file())
    out = copy.deepcopy(user)
    merged_before = effective()
    deletes: list[str] = []          # 校验全部通过后才从凭据存储删除的 key

    if "shell" in new:
        if new["shell"] not in SHELL_CHOICES:
            raise ValueError(f"未知 shell：{new['shell']}")
        out["shell"] = new["shell"]
    for k in ("max_steps", "bash_timeout"):
        if new.get(k) is not None:
            v = int(new[k])
            if v <= 0:
                raise ValueError(f"{k} 必须大于 0")
            out[k] = v
    if (new.get("permissions") or {}).get("mode"):
        mode = new["permissions"]["mode"]
        if mode not in MODES:
            raise ValueError(f"未知权限模式：{mode}")
        out.setdefault("permissions", {})["mode"] = mode
    if (new.get("ui") or {}).get("theme"):
        theme = new["ui"]["theme"]
        if theme not in THEMES:
            raise ValueError(f"未知主题：{theme}")
        out.setdefault("ui", {})["theme"] = theme
    if (new.get("ui") or {}).get("notify") is not None:
        nm = new["ui"]["notify"]
        if nm not in NOTIFY_MODES:
            raise ValueError(f"未知通知方式：{nm}")
        out.setdefault("ui", {})["notify"] = nm
    for k in ("close_to_tray", "notify_approval"):
        if (new.get("ui") or {}).get(k) is not None:
            out.setdefault("ui", {})[k] = bool(new["ui"][k])

    if isinstance(new.get("network"), dict):
        n = new["network"]
        net = dict(out.get("network") or {})
        mode = str(n.get("proxy_mode") or "").strip()
        if mode:
            if mode in ("system", "direct"):
                net["proxy"] = mode
            elif mode == "custom":
                url = str(n.get("proxy_url") or "").strip()
                if not url:
                    raise ValueError("选了自定义代理，但没有填代理地址（例如 http://127.0.0.1:7890）")
                if "://" not in url:
                    url = "http://" + url
                if url.split("://", 1)[0].lower() not in ("http", "https"):
                    raise ValueError(f"暂只支持 http/https 代理（Clash、v2rayN 等的 HTTP 端口即可）：{url}")
                net["proxy"] = url
            else:
                raise ValueError(f"未知代理模式：{mode}")
        for k, lo, hi in (("connect_timeout", 1, 300), ("read_timeout", 5, 3600), ("retries", 0, 10)):
            v = _num(n.get(k))
            if v is not None:
                if not (lo <= v <= hi):
                    raise ValueError(f"{k} 应在 {lo}–{hi} 之间")
                net[k] = int(v)
        out["network"] = net

    if "providers" in new:
        old = user.get("providers") or {}
        old_eff = merged_before.get("providers") or {}
        provs: dict = {}
        for name, p in (new["providers"] or {}).items():
            name = str(name).strip()
            if not name:
                raise ValueError("供应商名字不能为空")
            entry = {k: v for k, v in (old.get(name) or {}).items() if k not in PROVIDER_KEYS}
            entry["base_url"] = str(p.get("base_url") or "").strip()
            if not entry["base_url"]:
                raise ValueError(f"供应商 {name} 缺少 base_url")
            entry["api_key_env"] = str(p.get("api_key_env") or "").strip()
            if "api_key" in p and p["api_key"] is not None:      # "" = 清除；None/缺省 = 不变
                if str(p["api_key"]).strip():
                    entry["api_key"] = str(p["api_key"]).strip()   # 暂存，保存前移进凭据存储
                else:
                    deletes.append(secrets.provider_key_name(name))
            else:
                prev = (old.get(name) or old_eff.get(name) or {}).get("api_key")
                if prev:
                    entry["api_key"] = prev
            provs[name] = entry
        out["providers"] = provs
        for gone in set(merged_before.get("providers") or {}) - set(provs):
            deletes.append(secrets.provider_key_name(gone))

    if "models" in new:
        old = user.get("models") or {}
        provs_all = set((deep_merge(DEFAULTS, out).get("providers") or {}).keys())
        models: dict = {}
        for name, m in (new["models"] or {}).items():
            name = str(name).strip()
            if not name:
                raise ValueError("模型名字不能为空")
            entry = {k: v for k, v in (old.get(name) or {}).items() if k not in MODEL_KEYS}
            prov = str(m.get("provider") or "").strip()
            if prov not in provs_all:
                raise ValueError(f"模型 {name} 引用了不存在的供应商：{prov or '（空）'}")
            entry["provider"] = prov
            entry["model"] = str(m.get("model") or name).strip()
            fam = m.get("family")
            if fam and fam != "auto":
                if fam not in FAMILIES:
                    raise ValueError(f"未知模型家族：{fam}")
                entry["family"] = fam
            if m.get("vision") is not None and m.get("vision") != "auto":
                entry["vision"] = bool(m["vision"])
            cw = _num(m.get("context_window"))
            if cw:
                entry["context_window"] = int(cw)
            price = {k: _num((m.get("price") or {}).get(k)) for k in ("input", "output", "cache_read")}
            price = {k: v for k, v in price.items() if v is not None}
            cur = ((m.get("price") or {}).get("currency") or "").strip()
            if price:
                if cur:
                    price["currency"] = cur
                entry["price"] = price
            models[name] = entry
        out["models"] = models

    if new.get("default_model"):
        out["default_model"] = new["default_model"]
    final = deep_merge(DEFAULTS, out)
    if final.get("default_model") not in (final.get("models") or {}):
        raise ValueError(f"默认模型 {final.get('default_model')} 不在模型列表里")
    for name, m in (final.get("models") or {}).items():
        if m.get("provider") not in (final.get("providers") or {}):
            raise ValueError(f"模型 {name} 引用了不存在的供应商：{m.get('provider')}")
    _d2_in(new, out, deletes)
    _mcp_in(new, out)
    _hooks_in(new, out)
    Q.settings_in(new, out)
    _answer_in(new, out, final.get("models") or {})
    out["config_version"] = CURRENT_VERSION
    # 校验都过了：先删要清除的，再把新填的 key 存进凭据存储（config.toml 里不留明文）
    for d in dict.fromkeys(deletes):
        secrets.delete(d)
    try:
        secrets.move_plain_keys(out, overwrite=True)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"保存 API key 到{secrets.info().get('label', '凭据存储')}失败：{e}")
    if secrets.plain_keys(out):
        raise ValueError("API key 没能存进凭据存储，设置未保存（详见 ee-backend.log）")
    save_user_file(out)
    return get_settings()
