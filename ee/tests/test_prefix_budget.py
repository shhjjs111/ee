"""固定前缀（system + tools）的 token 预算：加工具或把描述写长时这里会先失败。

计数用字符数近似：中文工具 JSON 按约 2.2 字符/token（偏保守；DeepSeek-V3 tokenizer 实测本仓库的
system+tools 约 2.4～2.6 字符/token），这样 CI 不依赖 tokenizer 文件。tools 按 json.dumps(ensure_ascii=False) 计。

实测（2026-10-26，全新安装、无 MCP、仓库地图可用，DeepSeek-V3 tokenizer；以 main 4e69340 为改前）：
  改前 full  18 个工具：system   903 + tools 6,555 = 7,458 token（16,423 字符）
  改后 full  18 个工具：system 1,178 + tools 2,644 = 3,822 token（9,675 字符，近似 4,398）
  改后 lean   4 个工具：system   467 + tools   621 = 1,088 token（2,606 字符，近似 1,185）
"""
import json
import math

import pytest
from conftest import make_session

APPROX_CHARS_PER_TOKEN = 2.2
LEAN_LIMIT = 1200        # 近似 token
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
