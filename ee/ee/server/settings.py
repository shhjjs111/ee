"""设置页 <-> ~/.ee/config.toml。

- get：内置默认 + 用户级 config.toml 合并后的值（不含项目级覆盖；项目级文件路径单独列出）。
  API key 永远不回传原文，只给 api_key_set / api_key_preview（末四位）/ env_present。
- set：在用户级文件原内容上覆盖界面管理的键（未知键原样保留），整份写回（旧文件备份为 .bak）。
"""
from __future__ import annotations

import copy
import os

from ee.config import DEFAULTS, deep_merge, load_user_file, save_user_file, user_config_path
from ee.core.permissions import MODES
from ee.core.providers.profiles import FAMILY_ORDER, profile_for
from ee.tools.shellenv import SHELL_CHOICES

THEMES = ["system", "dark", "light"]
FAMILIES = ["auto", *FAMILY_ORDER, "default"]
MODEL_KEYS = {"provider", "model", "family", "context_window", "price"}
PROVIDER_KEYS = {"base_url", "api_key_env", "api_key"}


def _preview(key: str) -> str:
    return ("…" + key[-4:]) if len(key) > 8 else ("已设置" if key else "")


def effective() -> dict:
    return deep_merge(DEFAULTS, load_user_file())


def get_settings(project_sources: list[str] | None = None) -> dict:
    data = effective()
    providers = {}
    for name, p in (data.get("providers") or {}).items():
        key = str(p.get("api_key") or "")
        env = str(p.get("api_key_env") or "")
        providers[name] = {"base_url": p.get("base_url", ""), "api_key_env": env, "api_key_set": bool(key),
                           "api_key_preview": _preview(key), "env_present": bool(env and os.environ.get(env)),
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
            "ui": {"theme": (data.get("ui") or {}).get("theme", "system")},
            "providers": providers,
            "models": models,
        },
        "choices": {"shells": SHELL_CHOICES, "modes": MODES, "families": FAMILIES, "themes": THEMES},
    }


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
    user = load_user_file()
    out = copy.deepcopy(user)
    merged_before = effective()

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
                    entry["api_key"] = str(p["api_key"]).strip()
            else:
                prev = (old.get(name) or old_eff.get(name) or {}).get("api_key")
                if prev:
                    entry["api_key"] = prev
            provs[name] = entry
        out["providers"] = provs

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
    save_user_file(out)
    return get_settings()
