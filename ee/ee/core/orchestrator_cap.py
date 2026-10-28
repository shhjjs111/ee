"""D6-1 U4：子会话能力上限。

子会话是独立 Thread，会在新 cwd 下重新 load_config。如果不管，它可能拿到比父更宽的
权限：父是"需审批"，子却是"全自动"；父 deny 了 `rm *`，子没有；父只读默认开沙箱，子没开。

这里把父会话的有效能力算成子会话的上限。`subagent.access()` 的思路可借，但 task 子代理是
同回合临时 Session，不能直接套到独立 Thread 上。

另一条硬规则：共享 cwd 的子会话只能是只读。会改文件就必须独立 worktree——
自动化隔离失败后退回主目录那类问题（T5）不要在编排里重演。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from ee.core.permissions import READONLY_MODES

MODE_RANK = {"read-only": 0, "plan": 0, "default": 1, "auto": 2}


class CapabilityError(ValueError):
    """子会话能力请求不合法（比父宽、共享 cwd 写文件等）。消息直接给模型看。"""


@dataclass
class ChildCapability:
    mode: str
    allow: list[str] = field(default_factory=list)
    ask: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    sandbox: dict = field(default_factory=dict)
    network: dict = field(default_factory=dict)
    readonly: bool = False

    @property
    def requires_worktree(self) -> bool:
        return not self.readonly


def child_mode(parent_mode: str | None, requested: str | None) -> str:
    """取父与请求里更窄的那个。未知模式直接报错，不静默降级成 default。"""
    parent = parent_mode if parent_mode in MODE_RANK else "default"
    if not requested:
        return parent
    if requested not in MODE_RANK:
        raise CapabilityError(f"未知权限模式：{requested}")
    return requested if MODE_RANK[requested] <= MODE_RANK[parent] else parent


def child_capability(*, parent_mode: str | None, parent_policy=None, data: dict | None = None,
                     requested_mode: str | None = None, worktree: bool = True) -> ChildCapability:
    """算出子会话能拥有的能力。调用方必须用它的结果去构造子会话，不能只比较模式名。"""
    mode = child_mode(parent_mode, requested_mode)
    readonly = mode in READONLY_MODES
    if not worktree and not readonly:
        raise CapabilityError("共享工作区的子会话只能是只读；会改文件的任务必须用独立 worktree")

    p = parent_policy
    cap = ChildCapability(
        mode=mode,
        allow=list(getattr(p, "allow", []) or []),
        ask=list(getattr(p, "ask", []) or []),
        deny=list(getattr(p, "deny", []) or []),
        sandbox=copy.deepcopy(((data or {}).get("sandbox") or {})),
        network=copy.deepcopy(((data or {}).get("network") or {})),
        readonly=readonly,
    )
    if readonly:
        # 只读子会话继承父的沙箱/网络设定；父没开的不要替它开
        cap.sandbox.setdefault("readonly_default", bool(cap.sandbox.get("readonly_default", True)))
    return cap


def merge_child_rules(cap: ChildCapability, parent_policy) -> list[str]:
    """子会话最终要写进的 deny 规则：父的 + 子自带的，去重保序。"""
    out = list(getattr(parent_policy, "deny", []) or [])
    for r in cap.deny:
        if r not in out:
            out.append(r)
    return out
