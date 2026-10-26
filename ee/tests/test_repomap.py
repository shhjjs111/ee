"""仓库地图（D5-6）：多语言定义提取、排名、预算裁剪、缓存、忽略目录、降级、工具与自动注入。"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import collect, finish, make_session, run, text_chunks

from ee.core import repomap as R
from ee.tools.base import ToolContext, ToolError, ToolState

pytestmark = pytest.mark.skipif(not R.available(), reason="tree-sitter 未安装")


def write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def names(lang: str, src: str) -> list[str]:
    res = R.extract(lang, src.encode("utf-8"))
    assert res is not None, f"{lang} 不可用"
    return [d[0] for d in res[0]]


def test_extract_many_languages():
    assert names("python", "@dec\ndef f(a):\n    pass\nclass A(B):\n    def m(self):\n        return f(1)\nLIMIT = 3\n") \
        == ["f", "A", "m", "LIMIT"]
    assert set(names("javascript", "function f(){}\nclass A { m(){ return f(); } }\nconst g = () => 1;\n")) \
        == {"f", "A", "m", "g"}
    assert set(names("typescript", "interface I { a: number }\ntype T = string;\nenum E { A }\n"
                                   "export function f(x: I): T { return ''; }\nabstract class A { abstract m(): void; }\n")) \
        == {"I", "T", "E", "f", "A", "m"}
    assert set(names("tsx", "export const App = () => <div/>;\nfunction Comp(p: Props) { return <App/>; }\n")) \
        == {"App", "Comp"}
    assert set(names("go", "package main\ntype S struct{}\nfunc F() {}\nfunc (s *S) M() {}\n")) == {"S", "F", "M"}
    assert set(names("rust", "struct S;\nenum E {A}\ntrait T { fn t(&self); }\nimpl S { fn m(&self) {} }\nfn f() {}\n")) \
        == {"S", "E", "T", "t", "m", "f"}
    assert {"A", "m", "I"} <= set(names("java", "public class A { void m(){} }\ninterface I {}\n"))
    assert {"S", "f", "MAX"} <= set(names("c", "struct S { int a; };\nint f(int a) { return a; }\n#define MAX(a,b) a\n"))
    assert {"C", "k", "f"} <= set(names("cpp", "class C { int k() { return 1; } };\nint f() { return 0; }\n"))
    assert {"A", "M", "I"} <= set(names("csharp", "namespace N { public class A { public void M(){} } interface I {} }\n"))
    assert {"M", "A", "m"} <= set(names("ruby", "module M\n  class A\n    def m; end\n  end\nend\n"))
    assert {"A", "m", "f"} <= set(names("php", "<?php\nclass A { function m() {} }\nfunction f() {}\n"))
    assert {"A", "m", "f"} <= set(names("kotlin", "class A { fun m() {} }\nfun f() {}\n"))
    assert {"f", "g"} <= set(names("lua", "function f() end\nfunction M.g() end\n"))
    # 签名行取定义起始行（装饰器之后的 def 行），行号从 1 起
    defs = R.extract("python", b"@dec\ndef f(a):\n    pass\n")[0]
    assert defs[0][2] == 2 and defs[0][3] == "def f(a):"
    # 引用 = 标识符（不含定义名本身）
    refs = R.extract("python", b"def compute_total(items):\n    return helper_sum(items)\n")[1]
    assert refs.get("helper_sum") == 1 and "compute_total" not in refs


def make_project(root: Path) -> None:
    write(root, "core.py", "class CoreEngine:\n    def run_engine(self):\n        return 1\n\n"
                           "def helper_unused():\n    return 0\n")
    for i, name in enumerate(["a.py", "b.py", "pkg/c.py"]):
        write(root, name, f"from core import CoreEngine\n\ndef use_{i}():\n    e = CoreEngine()\n"
                          f"    return e.run_engine()\n")
    write(root, "lonely.py", "class LonelyThing:\n    pass\n")


def test_ranking_prefers_referenced(tmp_path):
    root = tmp_path / "proj"
    make_project(root)
    rm = R.RepoMap(root, tmp_path / "home")
    rm.refresh()
    ranked = rm.ranked_defs()
    order = [d[0] for _, _, d in ranked]
    assert order[0] == "CoreEngine"
    assert order.index("run_engine") < order.index("LonelyThing")
    assert order.index("CoreEngine") < order.index("helper_unused")
    # 关注的文件 / 符号排到最前
    assert rm.ranked_defs({"lonely.py"}, set())[0][2][0] == "LonelyThing"
    assert rm.ranked_defs(set(), {"helper_unused"})[0][2][0] == "helper_unused"
    text, info = rm.render(2000, focus=["lonely.py"])
    assert text.startswith("lonely.py:\n") and "class LonelyThing:" in text
    # 文件名只给 basename 也能识别；子目录过滤
    text, info = rm.render(2000, subdir="pkg")
    assert text.startswith("pkg/c.py:") and "core.py" not in text


def test_budget_trimming(tmp_path):
    root = tmp_path / "big"
    for i in range(60):
        write(root, f"mod{i}.py", "\n".join(f"def function_number_{i}_{j}(argument_one, argument_two):\n    return {j}\n"
                                            for j in range(8)))
    rm = R.RepoMap(root, tmp_path / "home")
    small, si = rm.render(200)
    big, bi = rm.render(3000)
    assert 0 < si["tokens"] <= 200 and bi["tokens"] <= 3000
    assert si["defs_shown"] < bi["defs_shown"] < bi["defs_total"] == 480
    assert R._fit([], 100) == 0 and R._fit(rm.ranked_defs(), 0) == 0


def test_cache_hit_and_invalidation(tmp_path):
    root = tmp_path / "proj"
    make_project(root)
    home = tmp_path / "home"
    rm = R.RepoMap(root, home)
    st = rm.refresh()
    assert st.parsed == 5 and st.cached == 0
    assert rm.cache_file.is_file() and rm.cache_file.parent == home / "cache" / "repomap"
    st = rm.refresh()
    assert st.parsed == 0 and st.cached == 5
    # 新实例（相当于重启）从磁盘缓存读，不重新解析
    rm2 = R.RepoMap(root, home)
    st = rm2.refresh()
    assert st.parsed == 0 and st.cached == 5
    # 改文件：只重新解析这一个，新定义出现
    p = write(root, "lonely.py", "class LonelyThing:\n    pass\n\nclass BrandNewThing:\n    pass\n")
    os.utime(p, ns=(p.stat().st_atime_ns, p.stat().st_mtime_ns + 5_000_000_000))
    st = rm2.refresh()
    assert st.parsed == 1 and st.cached == 4
    assert "BrandNewThing" in [d[0] for d in rm2.files["lonely.py"].defs]
    # 删文件：从地图里消失
    (root / "b.py").unlink()
    rm2.refresh()
    assert "b.py" not in rm2.files
    # 缓存版本不符（如语言包升级）时整体失效
    data = rm.cache_file.read_text(encoding="utf-8").replace('"version":1', '"version":0')
    rm.cache_file.write_text(data, encoding="utf-8")
    st = R.RepoMap(root, home).refresh()
    assert st.parsed == 4


def test_time_limit_partial(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    make_project(root)
    rm = R.RepoMap(root, tmp_path / "home")
    t = [0.0]

    def fake_monotonic():
        t[0] += 1.0
        return t[0]
    monkeypatch.setattr(R.time, "monotonic", fake_monotonic)
    st = rm.refresh(time_limit=2.0)
    assert st.timed_out and 0 < st.parsed < 5
    monkeypatch.undo()
    st = rm.refresh()                         # 下次继续把剩下的补上
    assert not st.timed_out and st.parsed + st.cached == 5


def test_ignored_dirs_without_git(tmp_path):
    root = tmp_path / "proj"
    write(root, "src/app.py", "def real_app():\n    pass\n")
    write(root, "node_modules/lib/index.js", "function vendored() {}\n")
    write(root, ".venv/lib/x.py", "def venv_thing():\n    pass\n")
    write(root, "build/gen.py", "def built():\n    pass\n")
    write(root, ".hidden/h.py", "def hidden():\n    pass\n")
    write(root, "secret/key.py", "def secret_fn():\n    pass\n")
    write(root, "README.md", "# 不是代码\n")
    write(root, ".eeignore", "secret/\n")
    files, total = R.list_files(root, 100)
    assert files == ["src/app.py"] and total == 1
    # 文件数上限：浅层优先
    write(root, "top.py", "def top():\n    pass\n")
    files, total = R.list_files(root, 1)
    assert files == ["top.py"] and total == 2


@pytest.mark.skipif(shutil.which("git") is None, reason="没有 git")
def test_gitignore_respected(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    write(root, ".gitignore", "generated/\n*.gen.ts\n")
    write(root, "main.py", "def main_entry():\n    pass\n")
    write(root, "generated/out.py", "def generated_fn():\n    pass\n")
    write(root, "x.gen.ts", "export function genTs() {}\n")
    write(root, "web/ui.tsx", "export function Ui() { return <div/>; }\n")   # 未跟踪但没被忽略：要包含
    files, _ = R.list_files(root, 100)
    assert sorted(files) == ["main.py", "web/ui.tsx"]


def test_unavailable_degrades(tmp_path, monkeypatch, ee_home, workspace):
    broken = R._TS()
    broken.loaded, broken.ts, broken.error = True, None, "ModuleNotFoundError: No module named 'tree_sitter'"
    monkeypatch.setattr(R, "_ts", broken)
    assert not R.available()
    assert R.extract("python", b"def f(): pass\n") is None
    text, info = R.build_map(workspace, ee_home, 1000)
    assert text == "" and "tree_sitter" in info["unavailable"]
    assert R.startup_message(workspace, ee_home, {"repo_map": {"auto_inject": True}}) is None
    from ee.core.providers.profiles import profile_for
    from ee.tools import build_tools
    assert "repo_map" not in [t.name for t in build_tools(profile_for("deepseek-flash"), {})]
    from ee.tools.repomap import RepoMapTool
    res = run(RepoMapTool().run({}, ToolContext(cwd=workspace, state=ToolState(), output_dir=tmp_path,
                                                extra={"home": ee_home})))
    assert res.ok and "不可用" in res.output and res.meta["unavailable"]
    # 单种语言加载失败：只是这种语言没有
    part = R._TS()
    part.load = lambda: True
    part.missing = {"go"}
    monkeypatch.setattr(R, "_ts", part)
    assert R.extract("go", b"package main\nfunc F() {}\n") is None


def test_home_dir_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert R.map_root(tmp_path) is None
    (tmp_path / "proj").mkdir()
    assert R.map_root(tmp_path / "proj") == (tmp_path / "proj").resolve()


def test_tool_output(tmp_path, ee_home):
    from ee.tools.repomap import RepoMapTool
    root = tmp_path / "proj"
    make_project(root)
    tool = RepoMapTool()
    assert tool.read_only and tool.schema()["function"]["name"] == "repo_map"
    ctx = ToolContext(cwd=root, state=ToolState(), output_dir=tmp_path / "out", extra={"home": ee_home})
    res = run(tool.run({}, ctx))
    assert res.ok and res.meta["kind"] == "repo_map"
    lines = res.output.splitlines()
    assert lines[0].startswith("仓库地图（") and "5 个源文件" in lines[0]
    assert "core.py:" in res.output and "    1│class CoreEngine:" in res.output
    assert res.output.index("core.py:") < res.output.index("lonely.py:")
    res = run(tool.run({"path": "pkg", "focus": "use_2", "max_tokens": 300}, ctx))
    assert "pkg/c.py:" in res.output and "core.py:" not in res.output
    with pytest.raises(ToolError):
        run(tool.run({"path": "nope"}, ctx))
    with pytest.raises(ToolError):
        run(tool.run({"max_tokens": "很多"}, ctx))
    empty = tmp_path / "empty"
    empty.mkdir()
    res = run(tool.run({}, ToolContext(cwd=empty, state=ToolState(), output_dir=tmp_path, extra={"home": ee_home})))
    assert "没有找到可解析" in res.output
    # 缓存写在 ee 主目录下
    assert list((ee_home / "cache" / "repomap").glob("*.json"))


def test_permissions_read_only_and_explore():
    from ee.core.permissions import PermissionPolicy
    for mode in ("read-only", "plan", "default"):
        assert PermissionPolicy(mode=mode).check("repo_map", {}).action == "allow"
    from ee.core.subagent import BUILTIN
    assert "repo_map" in BUILTIN["explore"].tools


def test_registered_and_auto_inject(ee_home, workspace):
    make_project(workspace)
    s, fake = make_session(workspace, [text_chunks("好") + finish("stop")])
    run(collect(s, "看看项目"))
    req = fake.calls[0]
    assert "repo_map" in [t["function"]["name"] for t in req["tools"]]
    assert not any("<repo-map>" in (m.get("content") or "") for m in req["messages"])     # 默认不注入

    s, fake = make_session(workspace, [text_chunks("好") + finish("stop"), text_chunks("嗯") + finish("stop")])
    s.config.data["repo_map"] = {**s.config.data["repo_map"], "auto_inject": True, "inject_tokens": 300}
    run(collect(s, "看看项目"))
    msgs = fake.calls[0]["messages"]
    inj = [m for m in msgs if "<repo-map>" in (m.get("content") or "")]
    assert len(inj) == 1 and msgs.index(inj[0]) == 1 and "class CoreEngine:" in inj[0]["content"]
    # 会话内不变：第二回合前缀里还是同一份（字节相同），哪怕文件改了
    write(workspace, "core.py", "class Renamed:\n    pass\n")
    run(collect(s, "再看"))
    assert fake.calls[1]["messages"][1] == inj[0]

    # 关掉 enabled：不注册工具也不注入
    s, fake = make_session(workspace, [text_chunks("好") + finish("stop")])
    s.config.data["repo_map"] = {"enabled": False, "auto_inject": True}
    from ee.tools import build_tools
    assert "repo_map" not in [t.name for t in build_tools(s.profile, s.config)]
    assert R.startup_message(workspace, ee_home, s.config.data) is None


def test_selftest():
    out = R.selftest()
    assert out["ok"], out
    assert out["missing_langs"] == []
