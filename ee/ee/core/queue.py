"""运行中排队的用户消息：插话（steer）和完成后追加（follow）。

- 插话：每一步工具执行完、或模型这一步不再调工具时送入，模型下一次请求就能看到（纠偏用）。
- 待追加：只在模型不再调工具、回合本该结束时送入（排在插话之后、Stop Hook 之前），像在回合结束后接着发一条新消息。
- 送入方式：one = 每次只送最早的一条；all = 把同类的全部合成一条 user 消息一次送入。
  默认插话 all、追加 one：插话通常是对“此刻”的几句补充和纠正，分开送会让后面的纠正晚一整步才被看到；
  追加通常是彼此独立的后续任务，逐条送能让模型一件一件做完（每件都走完结束检查），不会把几件事揉成一团。
- 回合结束时还没送入的（中断、出错、present_plan 等用户批准、Hook 结束回合、达到步数/花费上限……）
  通过 QueueReturned 事件退回界面输入框，不丢。
"""
from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass

STEER = "steer"
FOLLOW = "follow"
KINDS = (STEER, FOLLOW)
MODES = ("one", "all")
DEFAULT_MODES = {"steering_mode": "all", "follow_up_mode": "one"}
LABEL = {STEER: "插话", FOLLOW: "待追加"}

_order = itertools.count()


@dataclass
class Queued:
    id: str
    text: str
    kind: str = STEER
    order: int = 0

    def to_dict(self) -> dict:
        return {"id": self.id, "text": self.text, "queue": self.kind}


def mode_settings(data: dict | None) -> dict:
    """配置 [queue] 里的两个送入方式；缺省 / 非法值用默认。"""
    q = (data or {}).get("queue") or {}
    out = {}
    for k, dv in DEFAULT_MODES.items():
        v = str(q.get(k) or "").strip().lower() if isinstance(q, dict) else ""
        out[k] = v if v in MODES else dv
    return out


def settings_in(new: dict, out: dict) -> None:
    """设置页写入 queue.steering_mode / queue.follow_up_mode（校验后写进 out）。"""
    n = new.get("queue")
    if not isinstance(n, dict):
        return
    q = dict(out.get("queue") or {})
    for k in DEFAULT_MODES:
        if n.get(k) in (None, ""):
            continue
        v = str(n[k]).strip().lower()
        if v not in MODES:
            raise ValueError(f"未知的排队送入方式：{n[k]}（可选 one / all）")
        q[k] = v
    out["queue"] = q


class InputQueue:
    """两类排队消息放在一个按到达顺序排列的列表里；取的时候按类型挑。"""

    def __init__(self) -> None:
        self.items: list[Queued] = []

    def __bool__(self) -> bool:
        return bool(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def has(self, kind: str) -> bool:
        return any(q.kind == kind for q in self.items)

    def add(self, text: str, kind: str = STEER, qid: str | None = None) -> Queued:
        if kind not in KINDS:
            raise ValueError(f"未知的排队类型：{kind}")
        q = Queued(qid or f"q_{uuid.uuid4().hex[:10]}", text, kind, next(_order))
        self.items.append(q)
        return q

    def withdraw(self, qid: str) -> Queued | None:
        for i, q in enumerate(self.items):
            if q.id == qid:
                return self.items.pop(i)
        return None

    def take(self, kind: str, mode: str) -> list[Queued]:
        """取出 kind 类的排队消息：one 只取最早一条，all 全取。"""
        mine = [q for q in self.items if q.kind == kind]
        if mode != "all":
            mine = mine[:1]
        ids = {q.id for q in mine}
        self.items = [q for q in self.items if q.id not in ids]
        return mine

    def drain(self) -> list[Queued]:
        out, self.items = self.items, []
        return out

    def clear(self) -> None:
        self.items = []

    def texts(self) -> list[str]:
        return [q.text for q in self.items]


def join(items: list[Queued]) -> str:
    return "\n\n".join(q.text for q in items)
