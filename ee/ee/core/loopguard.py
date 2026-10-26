"""防死循环：一个回合内检测
1. 连续重复：同名同参数的工具调用连续出现 repeat_limit 次（默认 3）→ 不执行，回喂纠正提示；
   连续到 repeat_hard 次（默认 5）→ 停止本回合。
2. 无进展：连续 no_progress_steps 步（默认 8）没有任何“新的且成功”的工具调用 → 追加一条系统提醒；
   连续到 no_progress_hard 步（默认 16）→ 停止本回合。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

REPEAT_NOTE = ("[系统提示] 你已经连续 {n} 次用完全相同的参数调用 {name}，这次没有执行——结果不会变。"
               "不要再重复同一个调用：先看上面已有的结果，换一种做法（换参数、换工具、先读相关代码），"
               "如果确实卡住了，就停下来用文字向用户说明情况和需要什么帮助。")
REPEAT_STOP = "已因重复调用被系统停止，未执行。"
NO_PROGRESS_NOTE = ("[系统提示] 最近 {n} 步都没有新的进展（工具调用要么失败，要么在重复之前做过的事）。"
                    "请停下来重新想一想：目前卡在哪里、已经试过什么。换一个不同的思路；"
                    "如果需要用户的信息或决定，就直接用文字问用户，不要继续盲目尝试。")


def signature(name: str, args) -> str:
    try:
        a = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        a = str(args)
    return f"{name}\x00{a}"


@dataclass
class LoopGuard:
    repeat_limit: int = 3
    repeat_hard: int = 5
    no_progress_steps: int = 8
    no_progress_hard: int = 16
    last_sig: str | None = None
    streak: int = 0
    seen: set = field(default_factory=set)
    stale_steps: int = 0
    warned: bool = False
    halt: str | None = None

    @classmethod
    def from_config(cls, cfg: dict | None) -> "LoopGuard":
        cfg = cfg or {}
        g = cls()
        for k in ("repeat_limit", "repeat_hard", "no_progress_steps", "no_progress_hard"):
            if k in cfg:
                setattr(g, k, max(0, int(cfg[k])))
        if g.repeat_hard and g.repeat_limit and g.repeat_hard < g.repeat_limit:
            g.repeat_hard = g.repeat_limit
        return g

    def check(self, sig: str) -> tuple[str, int]:
        """调用执行前检查。返回 (ok|block|stop, 连续次数)。"""
        if sig == self.last_sig:
            self.streak += 1
        else:
            self.last_sig, self.streak = sig, 1
        if self.repeat_hard and self.streak >= self.repeat_hard:
            self.halt = (f"模型连续 {self.streak} 次用相同参数调用同一个工具，已停止本回合。"
                         "可以补充信息或换个说法再试。")
            return "stop", self.streak
        if self.repeat_limit and self.streak >= self.repeat_limit:
            return "block", self.streak
        return "ok", self.streak

    def end_step(self, outcomes: list[tuple[str, bool]]) -> str | None:
        """一步的工具都跑完后调用。outcomes = [(sig, ok)]。返回需要追加给模型的提醒（或 None）。"""
        progress = False
        for sig, ok in outcomes:
            if ok and sig not in self.seen:
                progress = True
            self.seen.add(sig)
        if progress:
            self.stale_steps, self.warned = 0, False
            return None
        self.stale_steps += 1
        if self.no_progress_hard and self.stale_steps >= self.no_progress_hard:
            self.halt = (f"连续 {self.stale_steps} 步没有进展（工具调用失败或在重复），已停止本回合。"
                         "可以补充信息或换个说法再试。")
            return None
        if self.no_progress_steps and self.stale_steps >= self.no_progress_steps and not self.warned:
            self.warned = True
            return NO_PROGRESS_NOTE.format(n=self.stale_steps)
        return None
