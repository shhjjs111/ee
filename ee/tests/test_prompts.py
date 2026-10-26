"""D3-B：自定义提示 / 斜杠命令。"""
import pytest
from conftest import run

from ee.core import prompts as P
from ee.server.app import Backend


def test_builtin_init(ee_home, workspace):
    ps = P.load_prompts(ee_home, workspace)
    assert "init" in ps and ps["init"].source == "builtin" and "AGENTS.md" in ps["init"].description
    text = P.expand(ps["init"], "重点写测试命令")
    assert "AGENTS.md" in text and text.endswith("重点写测试命令") and "$ARGUMENTS" not in text


def test_user_and_project_prompts(ee_home, workspace):
    (workspace / ".git").mkdir()
    sub = workspace / "pkg"
    sub.mkdir()
    (ee_home / "prompts").mkdir()
    (ee_home / "prompts" / "audit.md").write_text(
        "---\ndescription: 代码审查\nargument-hint: <文件>\n---\n请审查 $ARGUMENTS 的代码。\n", encoding="utf-8")
    (ee_home / "prompts" / "explain.md").write_text("# 解释代码\n解释一下这段代码", encoding="utf-8")
    (ee_home / "prompts" / "bad name.md").write_text("x", encoding="utf-8")
    (ee_home / "prompts" / "compact.md").write_text("不能覆盖内置命令", encoding="utf-8")
    (ee_home / "prompts" / "review.md").write_text("/review 是内置命令，也不能覆盖", encoding="utf-8")
    pd = workspace / ".ee" / "prompts"
    pd.mkdir(parents=True)
    (pd / "audit.md").write_text("项目版审查：$ARGUMENTS\r\n", encoding="utf-8")
    ps = P.load_prompts(ee_home, sub)          # 从子目录也能找到项目根
    assert set(ps) == {"init", "audit", "explain"}
    assert ps["audit"].source == "project" and P.expand(ps["audit"], "a.py") == "项目版审查：a.py"
    assert ps["explain"].description == "解释代码"
    assert P.expand(ps["explain"], "foo()") == "# 解释代码\n解释一下这段代码\n\nfoo()"
    assert P.expand(ps["explain"]) == "# 解释代码\n解释一下这段代码"


def test_prompts_rpc(ee_home, workspace):
    (ee_home / "prompts").mkdir()
    (ee_home / "prompts" / "fix.md").write_text("---\ndescription: 修 bug\n---\n修一下：$ARGUMENTS", encoding="utf-8")
    b = Backend(home=ee_home, persist=False)
    r = run(b.call("prompts/list", {"cwd": str(workspace)}))
    names = {p["name"]: p for p in r["prompts"]}
    assert names["fix"]["description"] == "修 bug" and names["fix"]["source"] == "user"
    assert r["dirs"]["user"].endswith("prompts")
    r = run(b.call("prompts/expand", {"name": "/fix", "args": "登录失败", "cwd": str(workspace)}))
    assert r == {"name": "fix", "text": "修一下：登录失败"}
    with pytest.raises(Exception, match="没有这个命令"):
        run(b.call("prompts/expand", {"name": "nope"}))
