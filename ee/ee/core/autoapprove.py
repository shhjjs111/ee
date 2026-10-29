"""自动审批的**建议态**（T10-c）：只提建议，绝不改决策。

方案原文两条硬约束：
    `stabilization-and-d6.md:118`
    > 自动审批必须服从确定性 deny、强制询问、显式 ask、只读/计划限制；
    > 不能仅「排除危险词后信任模型 allow」。建议先做建议展示，不默认自动放行。
    `stabilization-and-d6.md:313`
    > 先建议态、默认关闭；deny/force/显式 ask/只读限制不可由模型裁决覆盖。

所以本模块**没有能力**把 ask 变成 allow。它做三件事：
1. 判断「如果将来要自动放行，这一条够不够格」；
2. 够格时给出可写入的规则与影响面，让用户看过再决定；
3. 不够格时说明被哪一条挡住（force / deny / 只读 / 无前缀）。

`Suggestion.decision` 始终是**原样的 Decision**。任何调用方想拿它去放行，
必须先显式处理 `eligible`——而这需要用户自己打开开关（默认关）。

为什么不做「排除危险词后信任模型 allow」：危险词表永远不全，而漏一个就是
任意命令执行。规则由**确定性决策**产生（permissions 已经算过），本模块只做
资格筛选，不重新判断安全性。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 只读 / 计划模式下，这些工具连"建议放行"都不该出现
MUTATING_TOOLS = frozenset({"edit", "write", "apply_patch"})

# 这两个模式下一切写操作都被禁：与 permissions 的语义保持一致
READONLY_MODES = frozenset({"read-only", "plan"})


@dataclass
class Suggestion:
    """一条建议。`decision` 一定是传进来的那个 decision，原封不动。"""
    decision: object                     # permissions.Decision，原样保留
    eligible: bool = False               # 是否够格成为"以后都不问"的候选
    rule: str = ""                       # 可写入 permissions 的规则文本
    blocked_by: str = ""                 # 不够格时，被哪一条挡住
    summary: str = ""                    # 给用户看的一句话
    hits: int = 0                        # 这种情况已经出现过几次
    reason: str = ""                     # allow 时说明"无需建议"

    def as_dict(self) -> dict:
        return {"eligible": self.eligible, "rule": self.rule, "blocked_by": self.blocked_by,
                "summary": self.summary, "hits": self.hits, "reason": self.reason,
                "action": getattr(self.decision, "action", "")}


def enabled(config: dict | None) -> bool:
    """默认关闭。只有配置里显式 enabled=True 才算开。"""
    node = (config or {}).get("autoapprove") or {}
    return bool(node.get("enabled"))


def merge_config(user: dict | None, project: dict | None) -> dict:
    """合并用户与项目配置。**项目只能收紧**：

    - 项目可以把 enabled 关掉，不能把用户没开的打开（由调用方保证：本函数只合并字段，
      "不能打开"体现在 enabled 取 `user and project`）；
    - 项目不能往 tools 白名单里加工具（只能取交集）。
    """
    u = dict((user or {}).get("autoapprove") or {})
    p = dict((project or {}).get("autoapprove") or {})
    out: dict = {"enabled": bool(u.get("enabled"))}
    if "enabled" in p:
        # 项目只能关不能开：两者都真才算开
        out["enabled"] = bool(u.get("enabled")) and bool(p["enabled"])

    ut = list(u.get("tools") or [])
    if ut:
        if "tools" in p:
            pt = set(p.get("tools") or [])
            # 交集：项目只能缩小范围
            out["tools"] = [t for t in ut if t in pt]
        else:
            out["tools"] = ut
    else:
        out["tools"] = []
    return out


def suggest(decision, *, tool: str, args: dict | None = None, mode: str = "default",
            config: dict | None = None, hits: int = 0) -> Suggestion:
    """给一个 Decision 生成建议。**不修改 decision**。"""
    action = getattr(decision, "action", "")
    s = Suggestion(decision=decision, hits=hits)

    if action == "allow":
        s.reason = "已经是允许，不需要建议"
        return s

    # 先报「硬限制」，再报「功能没开」。顺序不能反：被 force / deny / 只读挡住时，
    # 用户即使打开开关也不该放行，报「功能未开启」会误导他去开开关。
    if getattr(decision, "force", False):
        s.blocked_by = "force：强制询问，任何模式下都要问"
        return s

    if action == "deny":
        s.blocked_by = f"deny：{getattr(decision, 'reason', '') or '确定性拒绝'}"
        return s

    if mode in READONLY_MODES and tool in MUTATING_TOOLS:
        s.blocked_by = f"只读/计划模式（{mode}）不允许写操作"
        return s

    rule = getattr(decision, "prefix", "") or ""
    if not rule:
        # 没有前缀的显式询问不能永久允许（permissions 的设计），因此没有可写入的规则
        s.blocked_by = "没有可写入的规则（显式询问不能永久允许）"
        return s

    # 到这里说明「这一条本身够格」；功能没开就不暴露建议。
    if not enabled(config):
        s.blocked_by = "autoapprove 未开启（默认关闭）"
        return s

    allowed_tools = (config or {}).get("autoapprove", {}).get("tools") or []
    if allowed_tools and tool not in allowed_tools:
        s.blocked_by = f"工具 {tool} 不在白名单里"
        return s

    s.eligible = True
    s.rule = _as_rule(tool, rule)
    s.summary = _summary(tool, args or {}, s.rule)
    return s


def _as_rule(tool: str, prefix: str) -> str:
    """prefix 已经是 `shell(git commit *)` 这种形态；不是的话按工具名兜底。"""
    p = prefix.strip()
    return p if p.startswith(f"{tool}(") or "(" in p else f"{tool}({p})"


def _summary(tool: str, args: dict, rule: str) -> str:
    shown = ""
    if tool == "shell":
        shown = str(args.get("command") or "")
    elif "path" in args:
        shown = str(args.get("path") or "")
    shown = shown.replace("\n", " ")[:120]
    return f"以后遇到相同模式（{rule}）不再询问" + (f"，例如：{shown}" if shown else "")
