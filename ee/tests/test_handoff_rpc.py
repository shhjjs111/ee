"""文件交接的 RPC（T10-e 界面侧）：handoff/manifest 与 handoff/verify。

界面要能让用户在两处之间搬东西前**先看见清单**。这一层守的是：

1. 不传参数只返回清单（不写盘、不动源）；
2. 清单里每条都带哈希，界面能显示"可恢复"的依据；
3. verify 只在用户给了目标路径时才跑；
4. 路径必须存在，否则明确报错——不能返回空清单冒充"没东西要搬"。
"""
from __future__ import annotations

import subprocess

import pytest

from ee.server.app import Backend


@pytest.fixture
def backend(tmp_path):
    return Backend(home=tmp_path, persist=False)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    for a in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"],
              ["git", "config", "user.name", "t"]):
        subprocess.run(a, cwd=r, check=True)
    (r / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (r / "a.py").write_text("x=1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=r, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=r, check=True)
    (r / "b.py").write_text("y=2\n", encoding="utf-8")      # untracked
    (r / "debug.log").write_text("log\n", encoding="utf-8")  # ignored
    return r


def _call(backend, method, **params):
    import asyncio

    return asyncio.run(backend.methods[method](**params))


def test_manifest_returns_entries(backend, repo):
    res = _call(backend, "handoff/manifest", path=str(repo))
    assert res["root"] == str(repo.resolve())
    assert res["entries"], "清单是空的，但工作区有未跟踪文件"
    e = res["entries"][0]
    for k in ("path", "category", "kind", "size", "sha256", "binary", "selected"):
        assert k in e, f"条目缺字段 {k}"


def test_manifest_includes_summary(backend, repo):
    res = _call(backend, "handoff/manifest", path=str(repo))
    s = res["summary"]
    assert s["total"] == len(res["entries"])
    assert "bytes" in s and "by_category" in s
    assert res["ignored_policy"] in ("skip", "include")


def test_manifest_does_not_write_anything(backend, repo):
    """生成清单不能改工作区（有未跟踪文件时最容易出错）。"""
    before = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                            capture_output=True, text=True).stdout
    _call(backend, "handoff/manifest", path=str(repo))
    after = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                           capture_output=True, text=True).stdout
    assert before == after


def test_manifest_rejects_missing_path(backend, tmp_path):
    with pytest.raises(Exception) as ei:
        _call(backend, "handoff/manifest", path=str(tmp_path / "nope"))
    assert "不存在" in str(ei.value) or "not found" in str(ei.value).lower()


def test_manifest_rejects_non_git_dir(backend, tmp_path):
    """不是 git 仓库时明确报错，不返回空清单冒充'没东西要搬'。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "x.txt").write_text("x", encoding="utf-8")
    with pytest.raises(Exception) as ei:
        _call(backend, "handoff/manifest", path=str(plain))
    assert "git" in str(ei.value).lower() or "仓库" in str(ei.value)


def test_verify_reports_problems_for_empty_target(backend, repo, tmp_path):
    """目标目录空着时，核对必须报缺失，不能报通过。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    res = _call(backend, "handoff/verify", path=str(repo), dest=str(dest))
    assert res["problems"], "空目标却报核对通过"
    assert res["ok"] is False


def test_verify_ok_for_exact_copy(backend, repo, tmp_path):
    """逐字节复制后核对应通过。"""
    import shutil

    from ee.core.handoff import build_manifest

    dest = tmp_path / "dest"
    dest.mkdir()
    m = build_manifest(repo)
    for e in m.entries:
        if not e.selected or e.kind != "file":
            continue
        dst = dest / e.path
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo / e.path, dst)

    res = _call(backend, "handoff/verify", path=str(repo), dest=str(dest))
    assert res["ok"] is True, res["problems"]
    assert res["problems"] == []


def test_verify_rejects_missing_dest(backend, repo, tmp_path):
    with pytest.raises(Exception):
        _call(backend, "handoff/verify", path=str(repo), dest=str(tmp_path / "nope"))
