"""固定前缀（system + tools）的 token 预算：加工具或把描述写长时这里会先失败。

计数用字符数近似：中文工具 JSON 按约 2.2 字符/token（偏保守；DeepSeek-V3 tokenizer 实测本仓库的
system+tools 约 2.4～2.6 字符/token），这样 CI 不依赖 tokenizer 文件。tools 按 json.dumps(ensure_ascii=False) 计。

实测（2026-10-26，全新安装、无 MCP、仓库地图可用，DeepSeek-V3 tokenizer；以 main 14ce3c1 为改前）：
  改前 full  18 个工具：system   903 + tools 6,555 = 7,458 token（16,423 字符）
  改后 full  18 个工具：system 1,178 + tools 2,644 = 3,822 token（9,675 字符，近似 4,398）
  改后 lean   4 个工具：system   467 + tools   621 = 1,088 token（2,606 字符，近似 1,185）
"""
import json
import math

import pytest
from conftest import make_session

APPROX_CHARS_PER_TOKEN = 2.2
LEAN_LIMIT = 1250        # 近似 token（2026-10-30 read 加 as_image 参数 +26，预算随之放宽；仍是防 schema 膨胀的守卫）
FULL_LIMIT = 4500        # 近似 token（实测约 4,398；真实 tokenizer 约 3,822）


def _prefix(workspace, **data):
    s, _ = make_session(workspace, [])
    s.config.data.update(data)
    if "profile" in data:
        from ee.tools import agent_profile, build_tools
        s.agent_profile = agent_profile(s.config)
        s.tools = build_tools(s.profile, s.config, agent=s.agent_profile)
        s.tool_map = {t.name: t for t in s.tools}
    return s, s.current_prefix()


def approx_tokens(p) -> int:
    chars = len(p.system["content"]) + len(json.dumps(p.tools, ensure_ascii=False))
    return math.ceil(chars / APPROX_CHARS_PER_TOKEN)


def test_lean_budget(ee_home, workspace):
    s, p = _prefix(workspace, profile="lean")
    assert [t["function"]["name"] for t in p.tools] == ["read", "write", "edit", "shell"]
    n = approx_tokens(p)
    print(f"lean 近似 {n} token")
    assert n <= LEAN_LIMIT


def test_full_budget(ee_home, workspace):
    s, p = _prefix(workspace)
    names = [t["function"]["name"] for t in p.tools]
    assert names[:8] == ["read", "write", "edit", "shell", "grep", "glob", "todo", "present_plan"]
    n = approx_tokens(p)
    print(f"full 近似 {n} token（{len(names)} 个工具）")
    assert n <= FULL_LIMIT


@pytest.mark.parametrize("profile", ["full", "lean"])
def test_every_tool_description_is_short(ee_home, workspace, profile):
    """工具描述一两句（细则进系统提示词的规则段），参数描述不超过一行。"""
    _, p = _prefix(workspace, profile=profile)
    for t in p.tools:
        f = t["function"]
        if f["name"] == "apply_patch":          # 补丁格式说明本身就是参数语法，不压
            continue
        assert len(f["description"]) <= 140, f["name"]
        assert f["description"].count("。") + f["description"].count("；") <= 3, f["name"]
        for k, v in (f["parameters"].get("properties") or {}).items():
            assert len(v.get("description", "")) <= 70, (f["name"], k)


def test_lsp_tool_schema_is_small():
    """lsp 工具的 schema 开销要小（它是可选项，不能挤占基础预算太多）。

    背景：full 工具表已用到 4421/4500，只剩 79 token 余量。lsp 打开后是 4583，
    超了 83。这不是措辞问题——schema 本身 315 字符（约 143 token）就已超余量。
    所以这里只管住"别再变大"，并把这个事实记下来：**加任何新工具前先看预算**。
    """
    import json as _json

    from ee.tools.lsp import LspTool

    schema = _json.dumps(LspTool().schema(), ensure_ascii=False)
    assert len(schema) <= 400, f"lsp schema 变大到 {len(schema)} 字符，会进一步挤压预算：{schema[:200]}"


def test_full_budget_headroom_is_known():
    """把 full 工具表的余量钉住：余量小于 100 token 时，加工具前必须重新评估。

    这条不是"必须通过"的功能断言，而是**变更护栏**：如果某天有人加了个工具
    把余量吃到 0 以下，这条会先失败，而不是等到用户开了某个可选工具才崩。
    """
    s, p = _prefix(workspace_fixture())
    n = approx_tokens(p)
    assert n <= FULL_LIMIT, f"full 前缀 {n} 已超上限 {FULL_LIMIT}"
    headroom = FULL_LIMIT - n
    # 只记录，不强制要求正余量——但小于 0 一定失败（上面已断言）
    print(f"full 前缀 {n}/{FULL_LIMIT}，余量 {headroom} token")


def workspace_fixture():
    import tempfile
    from pathlib import Path
    p = Path(tempfile.mkdtemp())
    return p


def test_lsp_on_keeps_budget_and_drops_repo_map():
    """开 lsp 时预算不能超，且被去掉的必须是 repo_map（不是 memory）。

    背景：full 工具表已经用到 4421/4500，只剩 79 余量；lsp 的 schema 约 143 token，
    直接加会超 83。FULL_LIMIT 是 2026-10-26 用真实 tokenizer 实测（3,822）反推的
    护栏（近似 4,398，取 4,500 留余量），不该为了提高它而改动——那会把它从
    "实测基准"退化成"当前值记录"。

    所以改成能力互斥：repo_map 和 lsp 都是"理解代码"，有 lsp 的实时诊断时
    静态仓库地图价值下降。**memory 不能动**——它和 lsp 毫无交集，砍掉会让用户
    遇到"开了 A 结果 B 没了"这种最难排查的问题。
    """
    from ee.tools import build_tools, agent_profile

    ws = workspace_fixture()
    s, _ = make_session(ws, [])
    s.config.data["lsp"] = {"enabled": True}
    s.agent_profile = agent_profile(s.config)
    s.tools = build_tools(s.profile, s.config, agent=s.agent_profile)
    s.tool_map = {t.name: t for t in s.tools}
    p = s.current_prefix()

    names = [t["function"]["name"] for t in p.tools]
    n = approx_tokens(p)
    assert n <= FULL_LIMIT, f"开 lsp 后前缀 {n} 超上限 {FULL_LIMIT}"
    assert "lsp" in names, f"开了 lsp 但工具表里没有：{names}"
    assert "repo_map" not in names, f"开 lsp 后 repo_map 应当让位：{names}"
    assert "memory" in names, f"memory 不该被 lsp 挤掉：{names}"


def test_lsp_off_restores_repo_map():
    """关掉 lsp 后 repo_map 要回来（互斥是单向的，不是永久损失）。"""
    from ee.tools import build_tools, agent_profile

    ws = workspace_fixture()
    s, _ = make_session(ws, [])
    s.config.data["lsp"] = {"enabled": False}
    s.agent_profile = agent_profile(s.config)
    s.tools = build_tools(s.profile, s.config, agent=s.agent_profile)
    names = [t.name for t in s.tools]
    assert "repo_map" in names, f"关 lsp 后 repo_map 应当恢复：{names}"
    assert "lsp" not in names
