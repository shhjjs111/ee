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


def system_prompt(profile) -> str:
    base = load_prompt("system/base.md")
    patch_name = getattr(profile, "prompt_patch", "default") or "default"
    try:
        patch = load_prompt(f"system/{patch_name}.md")
    except (FileNotFoundError, OSError):
        patch = load_prompt("system/default.md")
    return base + "\n\n" + patch + "\n"


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


def env_parts(cwd: Path, shell, git: gitinfo.GitStatus | None, gh_available: bool, mode_label: str,
              today: str | None = None) -> dict[str, str]:
    """返回 name → 内容。每个子块独立比较变化；量化到“天”，避免无谓抖动。"""
    osname = {"Windows": "Windows", "Darwin": "macOS", "Linux": "Linux"}.get(platform.system(), platform.system())
    parts = {
        "env": (f"<env>\n工作目录：{cwd}\n操作系统：{osname} {platform.release()}\n"
                f"日期：{today or time.strftime('%Y-%m-%d')}\n</env>"),
        "shell": (f"<shell>\n当前 shell：{shell.display}（{_syntax_hint(shell)}）\n</shell>" if shell else
                  "<shell>\n当前 shell：未知\n</shell>"),
        "permissions": f"<permissions>\n权限模式：{mode_label}\n{PLAN_NOTE if mode_label == '计划' else ''}</permissions>",
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
    cost: float = 0.0
    cost_known: bool = True
    last_prompt: int = 0
    last_completion: int = 0
    usage_missing: int = 0
    currency: str = "¥"
    history: list = field(default_factory=list)

    def add(self, usage: dict | None, price) -> float | None:
        self.requests += 1
        if not usage:
            self.usage_missing += 1
            return None
        p, c, h = usage["prompt"], usage["completion"], usage.get("hit")
        self.prompt += p
        self.completion += c
        self.last_prompt, self.last_completion = p, c
        if h is not None:
            self.hit += h
            self.hit_known_prompt += p
        cost = price.cost(p, c, h) if price is not None else None
        if cost is None:
            self.cost_known = False
        else:
            self.cost += cost
            self.currency = price.currency
        self.history.append((p, c, h))
        return cost

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
        return {"requests": self.requests, "prompt": self.prompt, "completion": self.completion,
                "hit": self.hit, "hit_rate": self.hit_rate, "cost": self.cost if self.cost_known else None,
                "currency": self.currency, "usage_missing": self.usage_missing}
