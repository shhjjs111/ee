"""推理强度 / 思考开关（D5-9）。

档位：""（默认：不发参数，用服务端默认）、low、medium、high、max（最高）、off（关闭思考）。
每个模型能选哪些强度档看档案 effort_levels（缺省 低/中/高）：DeepSeek 只有 默认（high）/ 低 / 最高（max）/ 关闭思考
（官方 Thinking Mode 文档：reasoning_effort = low | high | max，思考默认开启、默认 high；
关闭用 extra_body {"thinking": {"type": "disabled"}}）。其它 provider 仍是 默认 / 低 / 中 / 高 / 关闭思考。
三层取值（越前越优先）：会话里选的 → 按模型记住的（~/.ee/state/model_prefs.json）→ [models.<名>] reasoning_effort
→ 全局 [reasoning] effort。

模型支不支持看档案：effort_param（强度参数名）决定强度档能不能用，thinking_param 决定能不能关闭思考。
不支持的档位不发任何参数（界面置灰）。服务端返回 400 时由 openai_chat 去掉参数重试并记住。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

LEVELS = ("", "low", "medium", "high", "max", "off")
LABEL = {"": "默认", "low": "低", "medium": "中", "high": "高", "max": "最高", "off": "关闭思考"}
DEFAULT_EFFORT_LEVELS = ("low", "medium", "high")
_ALIASES = {"默认": "", "default": "", "auto": "", "低": "low", "中": "medium", "高": "high", "最高": "max",
            "关": "off", "关闭": "off", "关闭思考": "off", "none": "off", "minimal": "low", "xhigh": "high",
            "ultra": "max"}
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


def levels(profile) -> list[str]:
    """这个模型可选的强度档（不含 默认 和 关闭思考）；不支持调强度时为空。"""
    if not getattr(profile, "effort_param", ""):
        return []
    lv = [x for x in (getattr(profile, "effort_levels", None) or DEFAULT_EFFORT_LEVELS) if x in LEVELS and x not in ("", "off")]
    return lv or list(DEFAULT_EFFORT_LEVELS)


def labels(profile) -> dict[str, str]:
    """档位 → 界面文字（档案 effort_labels 可覆盖，例如 DeepSeek 的 默认（high）/ 最高（max））。"""
    return {**LABEL, **{str(k): str(v) for k, v in (getattr(profile, "effort_labels", None) or {}).items()}}


def canonical(profile, level: str) -> str:
    """把档位规整成这个模型界面上有的档：旧版记住的 中/高 在 DeepSeek 上按档案 effort_aliases 换成 默认/最高；
    没有 max 档的模型把 最高 当 高。模型不支持调强度时原样返回（界面置灰、请求不发）。"""
    if level in ("", "off"):
        return level
    lv = levels(profile)
    if not lv or level in lv:
        return level
    aliases = getattr(profile, "effort_aliases", None) or {}
    if level in aliases:
        a = normalize(aliases[level])
        return a if a is not None else ""
    if level == "max" and "high" in lv:
        return "high"
    return level


def support(profile) -> dict:
    """{"effort": 能否调强度, "off": 能否关闭思考, "levels": 可选强度档, "labels": 档位文字}。"""
    return {"effort": bool(getattr(profile, "effort_param", "")),
            "off": bool(getattr(profile, "thinking_param", "")),
            "levels": levels(profile),
            "labels": labels(profile)}


def effective(profile, level: str) -> str:
    """模型不支持的档位按“默认”处理（不发参数）。"""
    sup = support(profile)
    if level == "off":
        return level if sup["off"] else ""
    if not level:
        return ""
    lvl = canonical(profile, level)
    return lvl if lvl in sup["levels"] else ""


def request_params(profile, level: str, strip_effort: bool = False, strip_thinking: bool = False) -> tuple[dict, dict]:
    """→ (顶层参数, extra_body 参数)。level 不被支持或被降级掉时返回空。"""
    top: dict = {}
    body: dict = {}
    lvl = effective(profile, level)
    if lvl and lvl != "off" and not strip_effort:
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
