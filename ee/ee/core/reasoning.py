"""推理强度 / 思考开关（D5-9）。

档位：""（默认：不发参数，用服务端默认）、low、medium、high、off（关闭思考）。
三层取值（越前越优先）：会话里选的 → 按模型记住的（~/.ee/state/model_prefs.json）→ [models.<名>] reasoning_effort
→ 全局 [reasoning] effort。

模型支不支持看档案：effort_param（强度参数名）决定 低/中/高 能不能用，thinking_param 决定能不能关闭思考。
不支持的档位不发任何参数（界面置灰）。服务端返回 400 时由 openai_chat 去掉参数重试并记住。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

LEVELS = ("", "low", "medium", "high", "off")
LABEL = {"": "默认", "low": "低", "medium": "中", "high": "高", "off": "关闭思考"}
_ALIASES = {"默认": "", "default": "", "auto": "", "低": "low", "中": "medium", "高": "high", "关": "off",
            "关闭": "off", "关闭思考": "off", "none": "off", "minimal": "low", "max": "high", "xhigh": "high"}
_LOCK = threading.Lock()


def normalize(level) -> str | None:
    """把用户写法规整成档位；认不出返回 None。"""
    if level is None:
        return ""
    v = str(level).strip()
    low = v.lower()
    if low in LEVELS:
        return low
    if v in _ALIASES:
        return _ALIASES[v]
    if low in _ALIASES:
        return _ALIASES[low]
    return None


def support(profile) -> dict:
    """{"effort": 能否调 低/中/高, "off": 能否关闭思考}。"""
    return {"effort": bool(getattr(profile, "effort_param", "")),
            "off": bool(getattr(profile, "thinking_param", ""))}


def effective(profile, level: str) -> str:
    """模型不支持的档位按“默认”处理（不发参数）。"""
    sup = support(profile)
    if level in ("low", "medium", "high"):
        return level if sup["effort"] else ""
    if level == "off":
        return level if sup["off"] else ""
    return ""


def request_params(profile, level: str, strip_effort: bool = False, strip_thinking: bool = False) -> tuple[dict, dict]:
    """→ (顶层参数, extra_body 参数)。level 不被支持或被降级掉时返回空。"""
    top: dict = {}
    body: dict = {}
    lvl = effective(profile, level)
    if lvl in ("low", "medium", "high") and not strip_effort:
        name = profile.effort_param
        val = (profile.effort_values or {}).get(lvl, lvl)
        if name == "reasoning_effort":
            top[name] = val
        else:
            body[name] = val
    elif lvl == "off" and not strip_thinking:
        if profile.thinking_param == "thinking_type":
            body["thinking"] = {"type": "disabled"}
        elif profile.thinking_param == "enable_thinking":
            body["enable_thinking"] = False
    return top, body


# ------------------------------------------------------------------ 按模型记住

def _prefs_path(home: Path) -> Path:
    return Path(home) / "state" / "model_prefs.json"


def _load(home: Path) -> dict:
    p = _prefs_path(home)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def remembered(home: Path) -> dict[str, str]:
    eff = _load(home).get("effort") or {}
    return {str(k): str(v) for k, v in eff.items() if normalize(v) is not None}


def remember(home: Path, alias: str, level: str) -> None:
    """记住某个模型的档位；level="" 表示忘掉（跟随全局默认）。原子写。"""
    with _LOCK:
        d = _load(home)
        eff = dict(d.get("effort") or {})
        if level:
            eff[alias] = level
        else:
            eff.pop(alias, None)
        d["effort"] = eff
        p = _prefs_path(home)
        p.parent.mkdir(parents=True, exist_ok=True)
        from ee.core.atomic import write_text as atomic_write
        atomic_write(p, json.dumps(d, ensure_ascii=False, indent=2))


def configured(data: dict, alias: str) -> str:
    """配置里给这个模型的档位：[models.<名>] reasoning_effort，否则全局 [reasoning] effort。"""
    m = (data.get("models") or {}).get(alias) or {}
    v = normalize(m.get("reasoning_effort")) if isinstance(m, dict) and m.get("reasoning_effort") is not None else None
    if v:
        return v
    return normalize((data.get("reasoning") or {}).get("effort")) or ""


def resolve(data: dict, home: Path, alias: str) -> str:
    """新会话 / 换模型时的初始档位：按模型记住的 → 配置。"""
    r = remembered(home).get(alias)
    if r is not None:
        return normalize(r) or ""
    return configured(data, alias)
