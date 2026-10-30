"""模型家族档案：按模型名子串识别（走网关/中转时 base_url 不可信）。"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, fields
from importlib import resources

# 识别顺序有意义：deepseek-r1-distill-qwen 应归 deepseek。
FAMILY_ORDER = ["deepseek", "kimi", "glm", "minimax", "doubao", "qwen", "claude", "gemini", "gpt"]


@dataclass
class Price:
    input: float | None = None      # 每百万 token
    output: float | None = None
    cache_read: float | None = None
    currency: str = "¥"

    @property
    def known(self) -> bool:
        return self.input is not None and self.output is not None

    def cost(self, prompt: int, completion: int, hit: int | None, discount: float = 1.0) -> float | None:
        """按用量计价。discount 是错峰折扣系数（0<d<=1），只作用于输入+输出；
        缓存读取价（cache_read）不打折——它已经是错峰前价格的 1/50，再打折会失准。"""
        if not self.known:
            return None
        hit = hit or 0
        miss = max(0, prompt - hit)
        cr = self.cache_read if self.cache_read is not None else self.input
        d = min(1.0, max(0.0, discount))
        return (miss * self.input * d + hit * cr + completion * self.output * d) / 1_000_000


@dataclass
class Profile:
    family: str = "default"
    match: list = field(default_factory=list)
    context_window: int = 128000
    max_output: int = 8192
    parallel_tools: bool = True
    vision: bool = False
    reasoning: bool = False
    reasoning_passback: str = "none"
    edit_style: str = "edit"
    prompt_patch: str = "default"
    empty_assistant_content: str = "empty"
    quirks: list = field(default_factory=list)
    price: Price = field(default_factory=Price)
    temperature: float | None = None
    request_max_tokens: int | None = None   # 设了才在请求里发 max_tokens
    extra_body: dict = field(default_factory=dict)
    # 推理强度（D5-9）：effort_param = 请求里控制强度的参数名（"" = 不支持；reasoning_effort 走顶层，其它名字放 extra_body）；
    # effort_values = ee 档位 low/medium/high/max → 发给服务端的值（缺省原样）；
    # thinking_param = 关闭思考的写法：thinking_type（{"thinking": {"type": "disabled"}}）| enable_thinking（false）| ""
    effort_param: str = ""
    effort_values: dict = field(default_factory=dict)
    thinking_param: str = ""
    # effort_levels = 界面可选的强度档（缺省 low/medium/high；DeepSeek 只有 low/max，默认即 high）；
    # effort_aliases = 不在 effort_levels 里的旧档位怎么换（DeepSeek：medium → ""、high → max）；
    # effort_labels = 档位文字覆盖（"" 键是“默认”档）
    effort_levels: list = field(default_factory=list)
    effort_aliases: dict = field(default_factory=dict)
    effort_labels: dict = field(default_factory=dict)
    # 同家族内按模型名细分的覆盖项（[[variants]] match=[...] + 任意档案字段，price 按键合并）
    variants: list = field(default_factory=list)

    def has_quirk(self, q: str) -> bool:
        return q in self.quirks

    def merged(self, overrides: dict) -> "Profile":
        """返回应用了 config 覆盖项的新档案（不改原档案）。"""
        data = self.to_dict()
        for k, v in (overrides or {}).items():
            if k == "price" and isinstance(v, dict):
                data["price"] = {**data["price"], **v}
            elif k in data:
                data[k] = v
        return Profile.from_dict(data)

    def to_dict(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        d["price"] = dict(vars(self.price))
        d["match"] = list(self.match)
        d["quirks"] = list(self.quirks)
        d["extra_body"] = dict(self.extra_body)
        d["effort_values"] = dict(self.effort_values)
        d["effort_levels"] = list(self.effort_levels)
        d["effort_aliases"] = dict(self.effort_aliases)
        d["effort_labels"] = dict(self.effort_labels)
        d["variants"] = [dict(v) for v in self.variants]
        return d

    def for_model(self, model_name: str) -> "Profile":
        """应用第一个按模型名匹配上的 variant（不改原档案）。"""
        name = model_name.lower()
        for v in self.variants:
            if any(_matches(m, name) for m in (v.get("match") or [])):
                return self.merged({k: x for k, x in v.items() if k != "match"})
        return self

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        known = {f.name for f in fields(cls)}
        kw = {k: v for k, v in d.items() if k in known and k != "price"}
        p = d.get("price") or {}
        kw["price"] = Price(**{k: v for k, v in p.items() if k in {"input", "output", "cache_read", "currency"}})
        return cls(**kw)


_CACHE: dict[str, Profile] | None = None


def load_builtin() -> dict[str, Profile]:
    global _CACHE
    if _CACHE is None:
        out = {}
        pkg = resources.files("ee.core.providers") / "profiles"
        for entry in pkg.iterdir():
            if entry.name.endswith(".toml"):
                data = tomllib.loads(entry.read_text(encoding="utf-8"))
                out[data.get("family", entry.name[:-5])] = Profile.from_dict(data)
        _CACHE = out
    return _CACHE


def _matches(pattern: str, name: str) -> bool:
    if pattern.startswith("re:"):
        return re.search(pattern[3:], name) is not None
    return pattern in name


def detect_family(model_name: str) -> str:
    name = model_name.lower()
    profs = load_builtin()
    for fam in FAMILY_ORDER:
        p = profs.get(fam)
        if p and any(_matches(m, name) for m in p.match):
            return fam
    return "default"


def profile_for(model_name: str, family: str | None = None, overrides: dict | None = None) -> Profile:
    profs = load_builtin()
    fam = family or detect_family(model_name)
    base = (profs.get(fam) or profs["default"]).for_model(model_name)
    return base.merged(overrides or {})
