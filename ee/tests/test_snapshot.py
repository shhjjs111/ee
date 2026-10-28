"""D4-1：磁盘快照（影子 git 仓库）。"""
import asyncio
import shutil
import subprocess

import pytest
from conftest import finish, text_chunks, tool_chunks
from test_server import make_backend, wait_for

from ee.core import snapshot

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")


def _ds(workspace, ee_home):
    return snapshot.DiskSnapshot(workspace, ee_home)


def test_capture_changed_restore(workspace, ee_home):
    (workspace / "a.txt").write_text("a1", encoding="utf-8")
    (workspace / "c.txt").write_text("c1", encoding="utf-8")
    (workspace / "子目录").mkdir()
    (workspace / "子目录" / "中文.txt").write_text("旧", encoding="utf-8")
    ds = _ds(workspace, ee_home)
    t1 = ds.capture()
    assert t1 and ds.gitdir.is_dir() and not (workspace / ".git").exists()   # 不碰工作区
    (workspace / "a.txt").write_text("a2", encoding="utf-8")
    (workspace / "b.txt").write_text("new", encoding="utf-8")
    (workspace / "c.txt").unlink()
    (workspace / "子目录" / "中文.txt").write_text("新", encoding="utf-8")
    (workspace / "新目录").mkdir()
    (workspace / "新目录" / "x.txt").write_text("x", encoding="utf-8")
    t2 = ds.capture()
    changed = set(ds.changed(t1, t2))
    assert changed == {"a.txt", "b.txt", "c.txt", "子目录/中文.txt", "新目录/x.txt"}
    restored = ds.restore(t1, sorted(changed))
    assert len(restored) == 5
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "a1"
    assert (workspace / "c.txt").read_text(encoding="utf-8") == "c1"
    assert (workspace / "子目录" / "中文.txt").read_text(encoding="utf-8") == "旧"
    assert not (workspace / "b.txt").exists() and not (workspace / "新目录").exists()
    assert ds.changed(t1, ds.capture()) == []


def test_crlf_bytes_kept(workspace, ee_home):
    (workspace / "w.txt").write_bytes(b"line1\r\nline2\r\n")
    ds = _ds(workspace, ee_home)
    t1 = ds.capture()
    (workspace / "w.txt").write_bytes(b"changed\n")
    ds.restore(t1, ds.changed(t1, ds.capture()))
    assert (workspace / "w.txt").read_bytes() == b"line1\r\nline2\r\n"


def test_respects_gitignore_and_skips_big_files(workspace, ee_home):
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    (workspace / ".gitignore").write_text("out/\n", encoding="utf-8")
    (workspace / "out").mkdir()
    (workspace / "out" / "build.bin").write_text("x", encoding="utf-8")
    (workspace / "big.dat").write_bytes(b"0" * (snapshot.MAX_UNTRACKED_BYTES + 10))
    (workspace / "src.py").write_text("print(1)\n", encoding="utf-8")
    ds = _ds(workspace, ee_home)
    t = ds.capture()
    files = set(ds._run("ls-tree", "-r", "--name-only", t).decode().split())
    assert "src.py" in files and ".gitignore" in files
    assert "out/build.bin" not in files and "big.dat" not in files


def test_non_git_project_default_excludes(workspace, ee_home):
    (workspace / "node_modules" / "pkg").mkdir(parents=True)
    (workspace / "node_modules" / "pkg" / "i.js").write_text("x", encoding="utf-8")
    (workspace / "app.js").write_text("y", encoding="utf-8")
    ds = _ds(workspace, ee_home)
    files = set(ds._run("ls-tree", "-r", "--name-only", ds.capture()).decode().split())
    assert files == {"app.js"}


def test_unavailable_for_home_dir(ee_home, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    ds = snapshot.DiskSnapshot(tmp_path, ee_home)
    assert not ds.available and ds.capture() is None


SHELL_EDIT = "echo changed-by-shell > s.txt"


def test_undo_restores_shell_changes_and_survives_restart(ee_home, workspace):
    """内存快照只管 write/edit；磁盘快照连 shell 改的文件也能撤销，重启后照样可用、不会重复撤销。"""
    (workspace / "s.txt").write_text("old", encoding="utf-8")
    script = [
        tool_chunks(0, "c1", "write", {"path": "a.txt", "content": "v1"}) + finish("tool_calls"),
        text_chunks("ok1") + finish("stop"),
        tool_chunks(0, "c2", "shell", {"command": SHELL_EDIT}) + finish("tool_calls"),
        text_chunks("ok2") + finish("stop"),
    ]

    async def go():
        b, _, ev = make_backend(ee_home, script)
        tid = (await b.call("thread/start", {"cwd": str(workspace), "mode": "auto"}))["thread"]["id"]
        for text in ("写 a", "用 shell 改 s"):
            ev.clear()
            await b.call("turn/start", {"thread_id": tid, "text": text})
            await wait_for(ev, "turn/completed")
        assert (workspace / "s.txt").read_bytes() != b"old"          # PowerShell 可能写成 UTF-16，只看变了没有
        users = [it for it in (await b.call("thread/get", {"thread_id": tid}))["thread"]["items"] if it["type"] == "user"]
        assert all(u.get("tree") and u.get("tree_after") for u in users)

        # 重启后撤销最近一回合（shell 改的 s.txt）
        b2, _, _ = make_backend(ee_home, [])
        r = await b2.call("thread/undo", {"thread_id": tid})
        assert any(p.endswith("s.txt") for p in r["restored"])
        assert (workspace / "s.txt").read_text(encoding="utf-8") == "old"
        assert (workspace / "a.txt").read_text(encoding="utf-8") == "v1"
        # 再重启：已撤销的不重复，下一次撤销第一回合（删掉新建的 a.txt）
        b3, _, _ = make_backend(ee_home, [])
        r = await b3.call("thread/undo", {"thread_id": tid})
        assert any(p.endswith("a.txt") for p in r["restored"]) and not (workspace / "a.txt").exists()
        assert (workspace / "s.txt").read_text(encoding="utf-8") == "old"
        r = await b3.call("thread/undo", {"thread_id": tid})
        assert r["restored"] == []
    asyncio.run(go())


def test_undo_keeps_user_edits_to_other_files(ee_home, workspace):
    """撤销只动那一回合改过的文件；用户之后手改的其他文件不受影响。"""
    script = [tool_chunks(0, "c1", "write", {"path": "a.txt", "content": "v1"}) + finish("tool_calls"),
              text_chunks("ok") + finish("stop")]

    async def go():
        b, _, ev = make_backend(ee_home, script)
        tid = (await b.call("thread/start", {"cwd": str(workspace), "mode": "auto"}))["thread"]["id"]
        await b.call("turn/start", {"thread_id": tid, "text": "写 a"})
        await wait_for(ev, "turn/completed")
        (workspace / "mine.txt").write_text("用户自己写的", encoding="utf-8")
        r = await b.call("thread/undo", {"thread_id": tid})
        assert [p for p in r["restored"] if p.endswith("a.txt")] and not (workspace / "a.txt").exists()
        assert (workspace / "mine.txt").read_text(encoding="utf-8") == "用户自己写的"
    asyncio.run(go())


def test_shadow_repo_config_is_written_and_valid(workspace, ee_home):
    """影子仓库的固定配置必须真的写进去、且 git 能解析。

    这条守的是一个性能优化的正确性：原先 `_ensure()` 用 5 次 `git config` 逐个写，
    每个 git 子进程在 Windows 上恒定 70–100ms（与工作量无关），5 次就是 450ms。
    现在改成 init 之后直接把这几个常量键追加进 .git/config（实测 656ms → 312ms），
    但"直接写文件"一旦格式写坏，后续每次 capture 都会失败——所以既要有校验，
    也要有兜底路径。这里把结果钉住。"""
    d = _ds(workspace, ee_home)
    d._ensure()
    cfg = (d.gitdir / "config").read_text(encoding="utf-8")
    assert "[core]" in cfg
    assert "\tautocrlf = false" in cfg
    assert "\tlongpaths = true" in cfg
    assert "\tquotepath = false" in cfg
    # gc.auto 必须在 [gc] 段里，不能塞在 [core] 下
    assert "[gc]" in cfg and "\tauto = 0" in cfg
    core_part = cfg.split("[gc]")[0]
    assert "auto = 0" not in core_part


def test_shadow_repo_config_survives_empty_config(workspace, ee_home):
    """config 被清空（或 git init 没写出来）时必须能自愈，不能留一个坏仓库。

    空文件没有段头，直接追加会写出 git 解析不了的内容；这条路径会重新 init
    再走逐个 config 的兜底。"""
    d = _ds(workspace, ee_home)
    d.gitdir.mkdir(parents=True, exist_ok=True)
    d._run("init", "-q")
    (d.gitdir / "config").write_text("", encoding="utf-8")
    d._write_config()
    for key, want in (("core.autocrlf", "false"), ("core.longpaths", "true"), ("gc.auto", "0")):
        cur = d._run("config", key).decode().strip()
        assert cur == want, f"{key} 应为 {want}，实际 {cur!r}"
    # 仓库仍然可用
    assert d.capture()


def test_shadow_repo_config_idempotent(workspace, ee_home):
    """重复调用不能把同一个键写两遍（git 会取最后一个，但配置会越来越脏）。"""
    d = _ds(workspace, ee_home)
    d._ensure()
    first = (d.gitdir / "config").read_text(encoding="utf-8")
    d._ready = False
    d._ensure()
    second = (d.gitdir / "config").read_text(encoding="utf-8")
    assert first == second
    assert second.count("autocrlf") == 1
    assert second.count("auto = 0") == 1


def test_shadow_repo_costs_at_most_two_git_calls(workspace, ee_home):
    """建影子仓库时最多启动 2 个 git 进程（init + 一次校验），而不是 6–7 个。

    这是真正的性能不变量：Windows 上每个 git 子进程恒定 70–100ms，与工作量无关
    （实测 init 203ms、每次 config 78–109ms）。原先 init + 5 次 config = 656ms，
    现在 init + 一次 `config --list` 校验 = 312ms。
    上面几条测试只验"配置最终对不对"，回滚成逐个 config 它们照样绿——
    所以要直接数进程次数，否则这条优化可以在无人察觉时被改回去。"""
    d = _ds(workspace, ee_home)
    calls = []
    orig = d._run

    def counting(*args, **kw):
        calls.append(args[0] if args else "")
        return orig(*args, **kw)

    d._run = counting
    d._ensure()
    assert calls.count("init") == 1
    assert calls.count("config") == 1, f"应为 1 次 config（校验），实际 {calls.count('config')} 次：{calls}"
    assert len(calls) <= 2, f"建仓启动了 {len(calls)} 个 git 进程：{calls}"
