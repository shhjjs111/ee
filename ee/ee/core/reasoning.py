"""推理强度 / 思考开关（D5-9）。

档位：""（默认：不发参数，用服务端默认）、low、medium、high、max（最高）、off（关闭思考）。
每个模型能选哪些强度档看档案 effort_levels（缺省 低/中/高）：DeepSeek 只有 默认（high）/ 低 / 最高（max）/ 关闭思考
（官方 Thinking Mode 文档：reasoning_effort = low | high | max，思考默认开启、默认 high；
关闭用 extra_body {"thinking": {"type": "disabled"}}）。其它 provider 仍是 默认 / 低 / 中 / 高 / 关闭思考。
三层取值（越前越优先）：会话里选的 → 全局 [reasoning] effort（设置页/输入框改的就是它）。
档位是全局一个值，不按模型分别记：换模型时只按新模型支持的档位规整（见 canonical），
不支持的档位不发参数。[models.<名>] reasoning_effort 会盖掉面板的选择，已不再读取。

模型支不支持看档案：effort_param（强度参数名）决定强度档能不能用，thinking_param 决定能不能关闭思考。
不支持的档位不发任何参数（界面置灰）。服务端返回 400 时由 openai_chat 去掉参数重试并记住。

effort_param 可以带点写成嵌套路径（如 Claude 的 output_config.effort）：带点时整条走 extra_body
并按点拆成嵌套对象；"reasoning_effort" 这个名字特殊，OpenAI 兼容层认它在请求顶层。
"""
from __future__ import annotations

LEVELS = ("", "low", "medium", "high", "max", "off")
LABEL = {"": "默认", "low": "低", "medium": "中", "high": "高", "max": "最高", "off": "关闭思考"}
DEFAULT_EFFORT_LEVELS = ("low", "medium", "high")
_ALIASES = {"默认": "", "default": "", "auto": "", "低": "low", "中": "medium", "高": "high", "最高": "max",
            "关": "off", "关闭": "off", "关闭思考": "off", "none": "off", "minimal": "low", "xhigh": "high",
            "ultra": "max"}


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


def _nest(dst: dict, path: str, val) -> None:
    """把 "a.b.c" + 值写进 dst，沿途缺的层建成 dict。"""
    parts = [x for x in str(path).split(".") if x]
    cur = dst
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    if parts:
        cur[parts[-1]] = val


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
            # 带点的（output_config.effort / google.thinking_config.thinking_level）拆成嵌套对象
            _nest(body, name, val)
    elif lvl == "off" and not strip_thinking:
        if profile.thinking_param == "thinking_type":
            body["thinking"] = {"type": "disabled"}
        elif profile.thinking_param == "enable_thinking":
            body["enable_thinking"] = False
    return top, body


# ------------------------------------------------------------------ 取值


def configured(data: dict) -> str:
    """全局默认档位：[reasoning] effort。"""
    return normalize((data.get("reasoning") or {}).get("effort")) or ""


def resolve(data: dict) -> str:
    """新会话 / 换模型时的初始档位：全局一个值，不按模型分。"""
    return configured(data)
