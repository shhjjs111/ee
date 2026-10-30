"""shell 工具的「跑测试」引导（企业级：可追溯的提示词不变量）。

背景（真实评测发现）：eval 任务 03 full 模式，模型在修 bug 过程中跑了一次
`python -m pytest -q`（全量），撞上 shell 默认 120s 超时，多花了约 180s。
任务验收命令本身很轻（两个测试文件），是模型自己图省事跑了全量。

修复：在 shell 工具规则里明确引导——跑测试先定位相关测试文件、跑相关子集，
别一上来跑全量。锁住这条规则存在，防止将来被误删导致回归。
"""
from __future__ import annotations

from ee.tools.shell import ShellTool


def test_shell_rules_guide_test_scoping():
    """full 的 shell 规则必须引导模型「跑测试用相关子集，不要一上来全量」。"""
    rules = ShellTool().rules
    joined = "\n".join(rules)
    assert ("测试" in joined) and ("相关" in joined or "子集" in joined or "缩小" in joined), (
        f"shell 规则缺少跑测试的引导（应告诉模型跑相关子集而非全量）。当前规则：\n{joined}"
    )


def test_lean_omits_test_scoping_rule():
    """lean 没有 grep/glob 无法定位测试文件，且预算已满，不应加载「跑测试」这条规则。"""
    rules = ShellTool(bg_tools=False).rules
    joined = "\n".join(rules)
    assert "跑测试" not in joined, f"lean 不应加载跑测试规则（无定位工具 + 预算满）。当前：\n{joined}"

