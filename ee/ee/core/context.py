"""缓存工程：冻结前缀（system + tools，sha256 校验）、<context-update> 增量附录、命中率与花费统计。

规则（对所有前缀缓存模型通用）：
1. system 与 tools 在会话首个请求时冻结，之后不增删不重排；每次请求前重算 sha256，变了发警告。
2. system 里不放日期 / git / cwd / shell 等动态信息；这些做成 <context-update seq=N> 挂在最新 user 消息尾部：
   首次全量，之后只发变化项，全没变就发自闭合标记。
3. 历史只追加：消息一旦发出字节不变（wire_messages 总是复制）。
"""
from __future__ import annotations

import hashlib
import json
import platform
import time
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from ee.core import gitinfo


def load_prompt(rel_path: str) -> str:
    f = resources.files("ee") / "prompts" / rel_path
    return f.read_text(encoding="utf-8").strip()


def builtin_base(profile=None, agent: str = "full") -> str:
    """内置基础段：agent=full 用 system/base.md，lean（精简模式）用 system/lean.md。用户的 SYSTEM.md（core/sysprompt.py）替换的就是这一段。"""
    return load_prompt("system/lean.md" if agent == "lean" else "system/base.md")


def system_prompt(profile, style_segment: str = "", custom=None, tools=None, agent: str = "full") -> str:
    """style_segment：回答风格段（core/styles.py），附在最末尾；默认风格为空，system 与没有风格时逐字节相同。
    custom：sysprompt.SystemSources（SYSTEM.md 替换基础段、APPEND_SYSTEM.md 追加、模型补丁开关）；
    None 或没有任何自定义时与原来逐字节相同。
    tools：本会话启用的工具，它们各自的使用规则（prompts/tools/*.md 的「## 规则」）去重后拼成「工具使用规则」段，
    紧跟基础段。SYSTEM.md 只替换基础段；工具规则照常按启用的工具拼装追加（工具的使用约束跟着工具走，
    自定义提示词不需要也不应该重写它们）。
    agent：full / lean，决定内置基础段用哪份。"""
    base = builtin_base(profile, agent)
    use_patch = True
    if custom is not None:
        if custom.system is not None:
            base = custom.system.text
        use_patch = custom.model_patch
    if tools:
        from ee.tools import tool_rules
        rules = tool_rules(tools)
        if rules:
            base += "\n\n# 工具使用规则\n" + "\n".join(f"- {r}" for r in rules)
    text = base + "\n"
    if use_patch:
        patch_name = getattr(profile, "prompt_patch", "default") or "default"
        try:
            patch = load_prompt(f"system/{patch_name}.md")
        except (FileNotFoundError, OSError):
            patch = load_prompt("system/default.md")
        text = base + "\n\n" + patch + "\n"
    if custom is not None and custom.append is not None:
        text += "\n" + custom.append.text + "\n"
    if style_segment:
        text += "\n" + style_segment.strip() + "\n"
    return text


def prefix_hash(system: dict, tools: list[dict]) -> str:
    blob = json.dumps({"system": system, "tools": tools}, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class FrozenPrefix:
    system: dict
    tools: list[dict]
    sha256: str

    @classmethod
    def freeze(cls, system_text: str, tools: list[dict]) -> "FrozenPrefix":
        sysmsg = {"role": "system", "content": system_text}
        tools_copy = json.loads(json.dumps(tools, ensure_ascii=False))
        return cls(sysmsg, tools_copy, prefix_hash(sysmsg, tools_copy))

    def verify(self) -> bool:
        return prefix_hash(self.system, self.tools) == self.sha256


# ------------------------------------------------------------------ 动态环境块

PLAN_NOTE = ("计划模式：现在只调研、出方案，不修改任何文件（写文件和非只读命令都会被拒绝）。\n"
             "1. 先读代码、搜索、必要时上网，把要改哪里、为什么弄清楚；需求有关键歧义时直接用文字问用户，然后结束回合。\n"
             "2. 想清楚后调用 present_plan 提交方案（markdown）：目标、要改的文件和具体改法、步骤顺序、风险和验证方法；"
             "要具体到别人照着就能做，不要写空话。\n"
             "3. 提交后本回合结束，等用户批准；用户可能提修改意见，按意见改完再次 present_plan。\n")


PLAN_NOTE_TEXT = ("计划模式：现在只调研、出方案，不修改任何文件（写文件和非只读命令都会被拒绝）。"
                  "先读代码弄清要改哪里、为什么；想清楚后直接用文字给出方案（目标、要改的文件和改法、步骤、风险和验证方法），"
                  "然后结束回合，等用户批准并切换到可写模式后再动手。\n")


def env_parts(cwd: Path, shell, git: gitinfo.GitStatus | None, gh_available: bool, mode_label: str,
              today: str | None = None, plan_tool: bool = True) -> dict[str, str]:
    """返回 name → 内容。每个子块独立比较变化；量化到“天”，避免无谓抖动。
    plan_tool=False：没有 present_plan 工具（关掉或精简模式），计划模式改为用文字给方案。"""
    osname = {"Windows": "Windows", "Darwin": "macOS", "Linux": "Linux"}.get(platform.system(), platform.system())
    parts = {
        "env": (f"<env>\n工作目录：{cwd}\n操作系统：{osname} {platform.release()}\n"
                f"日期：{today or time.strftime('%Y-%m-%d')}\n</env>"),
        "shell": (f"<shell>\n当前 shell：{shell.display}（{_syntax_hint(shell)}）\n</shell>" if shell else
                  "<shell>\n当前 shell：未知\n</shell>"),
        "permissions": f"<permissions>\n权限模式：{mode_label}\n{(PLAN_NOTE if plan_tool else PLAN_NOTE_TEXT) if mode_label == '计划' else ''}</permissions>",
    }
    if git is not None and git.is_repo:
        dirty = f"{git.changed} 个已跟踪文件有改动，{git.untracked} 个未跟踪文件" if git.dirty else "工作区干净"
        ab = f"（{git.ahead_behind}）" if git.ahead_behind else ""
        parts["git"] = f"<git>\n这是 git 仓库，根目录：{git.root}\n当前分支：{git.branch}{ab}\n{dirty}\n</git>"
    else:
        parts["git"] = "<git>\n当前目录不是 git 仓库\n</git>"
    if gh_available:
        parts["tools"] = ("<dev-tools>\n已安装 gh（GitHub CLI）：可用 gh pr view/list、gh issue view/list、"
                          "gh run list 等查询 PR、issue、CI；gh 的写操作（创建/评论/合并）需要用户要求并会请求批准。\n</dev-tools>")
    return parts


def _syntax_hint(shell) -> str:
    syn = getattr(shell, "syntax", "posix")
    if syn == "powershell":
        return "PowerShell 语法：用 ; 分隔命令，$env:NAME 读环境变量，路径可用 \\ 或 /"
    if syn == "cmd":
        return "cmd 语法：用 & 或 && 连接命令，%NAME% 读环境变量"
    return "POSIX shell 语法"


class ContextUpdates:
    """<context-update seq=N> 增量附录（天枢做法）。"""

    def __init__(self) -> None:
        self.seq = 0
        self.last: dict[str, str] = {}
        self.baseline_sent = False

    def reset(self) -> None:
        """压缩或换模型后调用：下一次补发全量。"""
        self.baseline_sent = False
        self.last = {}

    def build(self, parts: dict[str, str]) -> str:
        self.seq += 1
        if not self.baseline_sent:
            self.last, self.baseline_sent = dict(parts), True
            return f'<context-update seq="{self.seq}">\n' + "\n".join(parts.values()) + "\n</context-update>"
        changed = [v for k, v in parts.items() if self.last.get(k) != v]
        removed = [k for k in self.last if k not in parts]
        self.last = dict(parts)
        if not changed and not removed:
            return f'<context-update seq="{self.seq}"/>'
        body = "\n".join(changed + [f"<{k}/>" for k in removed])
        return f'<context-update seq="{self.seq}" mode="delta">\n' + body + "\n</context-update>"


# ------------------------------------------------------------------ 统计

@dataclass
class UsageStats:
    requests: int = 0
    prompt: int = 0
    completion: int = 0
    hit: int = 0
    hit_known_prompt: int = 0      # 返回了命中字段的那些请求的 prompt 合计
    reasoning: int = 0             # 推理 token（包含在 completion 里；服务端给了才有）
    reasoning_known: bool = False
    cost: float = 0.0
    cost_known: bool = True
    last_prompt: int = 0
    last_completion: int = 0
    usage_missing: int = 0
    currency: str = "¥"
    # 每次请求一条：{prompt, completion, hit, reasoning, cost, currency, model}（子代理并入主会话时按条复制，花费不重算）
    records: list = field(default_factory=list)
    by_model: dict = field(default_factory=dict)
    pruned_tokens: int = 0         # 修剪旧工具输出累计省下的 token（估计，D5-10）
    prunes: int = 0
    compacted_tokens: int = 0      # 压缩累计省下的 token（估计：压缩前 − 压缩后）

    def add(self, usage: dict | None, price, model: str = "") -> float | None:
        """记一次请求：price 是这次请求实际用的模型的单价，model 是它的名字（按模型细分统计）。

        缺 usage 时也要落一条记录（cost=None）：否则这次请求会从账里彻底消失，
        用户看到的总额就成了「看起来完整、其实漏了若干次」的假精确。
        """
        if not usage:
            self.requests += 1
            self.usage_missing += 1
            self.records.append({"prompt": 0, "completion": 0, "hit": None, "reasoning": None,
                                 "cost": None, "currency": getattr(price, "currency", "¥") if price is not None else "¥",
                                 "model": model, "usage_missing": True})
            self.cost_known = False
            return None
        cost = price.cost(usage["prompt"], usage["completion"], usage.get("hit")) if price is not None else None
        self.add_record({"prompt": usage["prompt"], "completion": usage["completion"], "hit": usage.get("hit"),
                         "reasoning": usage.get("reasoning"), "cost": cost,
                         "currency": getattr(price, "currency", "¥") if price is not None else "¥", "model": model})
        return cost

    def add_record(self, r: dict) -> None:
        self.requests += 1
        p, c, h = int(r["prompt"]), int(r["completion"]), r.get("hit")
        self.prompt += p
        self.completion += c
        self.last_prompt, self.last_completion = p, c
        if h is not None:
            self.hit += h
            self.hit_known_prompt += p
        if r.get("reasoning") is not None:
            self.reasoning += int(r["reasoning"])
            self.reasoning_known = True
        cost = r.get("cost")
        if cost is None:
            self.cost_known = False
        else:
            self.cost += cost
            self.currency = r.get("currency") or self.currency
        m = r.get("model") or ""
        if m:
            b = self.by_model.setdefault(m, {"requests": 0, "prompt": 0, "completion": 0, "reasoning": 0, "cost": 0.0,
                                              "currency": r.get("currency") or "¥", "cost_known": True})
            b["requests"] += 1
            b["prompt"] += p
            b["completion"] += c
            b["reasoning"] += int(r.get("reasoning") or 0)
            if cost is None:
                b["cost_known"] = False
            else:
                b["cost"] += cost
        self.records.append(dict(r))

    def spent(self, currency: str) -> float:
        """某币种的已知花费合计（预算判断用）。"""
        return sum(float(r["cost"]) for r in self.records
                   if r.get("cost") is not None and (r.get("currency") or "¥") == currency)

    @property
    def hit_rate(self) -> float | None:
        if self.hit_known_prompt <= 0:
            return None
        return self.hit / self.hit_known_prompt

    def context_used(self, window: int) -> float:
        if window <= 0:
            return 0.0
        return min(1.0, (self.last_prompt + self.last_completion) / window)

    def totals(self) -> dict:
        """对外口径：v2 字段（分币种、完整性、命中覆盖率）+ 兼容的单值 cost/currency。

        单值 `cost` 只在「唯一币种且金额完整」时给出；混币或缺用量一律 None。
        消费方如果只读 `cost`，会自然退化成「不知道总额」而不是「错的总览」。
        """
        from ee.core import usage as U

        roll = U.UsageRollup(self.records)
        out = roll.totals_v2(requests=self.requests, prompt=self.prompt, completion=self.completion,
                             hit=self.hit, hit_known_prompt=self.hit_known_prompt,
                             usage_missing=self.usage_missing)
        if self.reasoning_known:
            out["reasoning"] = self.reasoning
        if self.pruned_tokens:
            out["pruned"] = self.pruned_tokens
        if self.compacted_tokens:
            out["compacted"] = self.compacted_tokens
        if self.by_model:
            out["by_model"] = {k: {**v, "cost": v["cost"] if v["cost_known"] else None}
                               for k, v in self.by_model.items()}
            for v in out["by_model"].values():
                v.pop("cost_known", None)
        return out
