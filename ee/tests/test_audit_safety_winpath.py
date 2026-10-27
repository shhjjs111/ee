"""Windows 路径归一（.eeignore / 工作区边界）：结尾点/空格、::$DATA 备用数据流、\\\\?\\ 前缀、8.3 短名。
纯函数部分在所有平台都测；真实文件系统部分只在 Windows 上跑。"""
import os
from pathlib import Path

import pytest

from ee.core.winpath import normalize_win_path as N

win_only = pytest.mark.skipif(os.name != "nt", reason="只在 Windows 上有意义")


@pytest.mark.parametrize("raw,want", [
    (r"C:\ws\secret.env.", r"C:\ws\secret.env"),
    (r"C:\ws\secret.env. . ", r"C:\ws\secret.env"),
    (r"C:\ws\secret.env::$DATA", r"C:\ws\secret.env"),
    (r"C:\ws\secret.env:hidden:$DATA", r"C:\ws\secret.env"),
    (r"C:\ws\secret.env:hidden", r"C:\ws\secret.env"),
    (r"C:\ws\keys. \a.pem", r"C:\ws\keys\a.pem"),
    ("C:/ws/secret.env.", r"C:\ws\secret.env"),
    (r"\\?\C:\ws\secret.env", r"C:\ws\secret.env"),
    (r"\\?\UNC\srv\share\a.txt", r"\\srv\share\a.txt"),
    (r"\\srv\share\a.txt.", r"\\srv\share\a.txt"),
    (r"C:\ws\..\x\.", r"C:\ws\..\x\."),
    (r"secret.env.", "secret.env"),
    (r"C:\ws\a b.txt", r"C:\ws\a b.txt"),
    ("C:", "C:"),
    ("C:\\", "C:\\"),
])
def test_normalize_pure(raw, want):
    assert N(raw, long_path=lambda p: None) == want


def test_short_name_expanded_on_longest_existing_prefix():
    table = {r"C:\ws\SECRET~1": r"C:\ws\secrets-dir", r"C:\ws\SECRET~1.ENV": r"C:\ws\secret.env"}
    lp = table.get
    assert N(r"C:\ws\SECRET~1.ENV", long_path=lp) == r"C:\ws\secret.env"
    assert N(r"C:\ws\SECRET~1\new.txt", long_path=lp) == r"C:\ws\secrets-dir\new.txt"
    assert N(r"C:\ws\SECRET~1\new.txt.", long_path=lp) == r"C:\ws\secrets-dir\new.txt"
    called = []
    N(r"C:\ws\plain.txt", long_path=lambda p: called.append(p))
    assert called == []                     # 没有 ~ 就不调系统 API


# ---------------------------------------------------------------- 真实 Windows
def _short(p: Path) -> str | None:
    import ctypes
    from ctypes import wintypes
    f = ctypes.windll.kernel32.GetShortPathNameW
    f.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    f.restype = wintypes.DWORD
    buf = ctypes.create_unicode_buffer(1024)
    n = f(str(p), buf, 1024)
    return buf.value if n and buf.value != str(p) else None


@win_only
def test_eeignore_windows_bypasses(tmp_path):
    from ee.core.eeignore import EeIgnore
    (tmp_path / ".git").mkdir()
    (tmp_path / ".eeignore").write_text("secret-credentials.env\nprivate-folder/\n", encoding="utf-8")
    f = tmp_path / "secret-credentials.env"
    f.write_text("k=v")
    (tmp_path / "private-folder").mkdir()
    (tmp_path / "private-folder" / "a.txt").write_text("x")
    ig = EeIgnore.load(tmp_path)
    for raw in ["secret-credentials.env.", "secret-credentials.env ", "secret-credentials.env::$DATA",
                "SECRET-CREDENTIALS.ENV", "private-folder.\\a.txt", "private-folder\\a.txt::$DATA"]:
        assert ig.ignored(tmp_path / raw), raw
    assert ig.ignored("\\\\?\\" + str(f))
    assert not ig.ignored(tmp_path / "other.txt")
    s = _short(f)
    if s is None:
        pytest.skip("这个卷没有生成 8.3 短名")
    assert ig.ignored(Path(s))
    sd = _short(tmp_path / "private-folder")
    if sd:
        assert ig.ignored(Path(sd) / "a.txt")


@win_only
def test_permissions_windows_bypasses(tmp_path):
    from ee.core.permissions import PermissionPolicy
    (tmp_path / ".git").mkdir()
    (tmp_path / ".eeignore").write_text("secret-credentials.env\n", encoding="utf-8")
    (tmp_path / "secret-credentials.env").write_text("k=v")
    pol = PermissionPolicy(workspace=tmp_path)
    for p in ["secret-credentials.env.", "secret-credentials.env::$DATA", "secret-credentials.env  "]:
        assert pol.check("read", {"path": p}).action == "deny", p
        assert pol.check("write", {"path": p, "content": "x"}).action == "deny", p
    for p in [".git.\\config", ".GIT\\config", ".git::$INDEX_ALLOCATION\\config"]:
        assert pol.check("write", {"path": p, "content": "x"}).action != "allow", p
