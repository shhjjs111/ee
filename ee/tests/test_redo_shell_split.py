"""权限命令拆分 / 归一重做（fix/redo-shell-split）。

1) 全自动模式下“强制确认 / 拒绝”不能被引号拆写、PowerShell 参数缩写、git 全局选项等绕过；
2) 按实际执行的 shell（posix / powershell / cmd）用对应语法拆分，解析不了按需要确认处理；
3) pytest 会执行仓库里的 conftest.py，不再算只读命令。
"""
import pytest

from ee.core.permissions import PermissionPolicy


def pol(ws, mode="default", shell=None, **kw):
    if shell is not None:
        kw["shell"] = shell
    return PermissionPolicy(mode=mode, workspace=ws, **kw)


# ------------------------------------------------------------------ 3) pytest 不再免问

@pytest.mark.parametrize("cmd", ["pytest -q", "python -m pytest tests", "py.test", "python3 -m pytest -x"])
def test_pytest_needs_confirm(tmp_path, cmd):
    assert pol(tmp_path).check("shell", {"command": cmd}).action == "ask"
    assert pol(tmp_path, "read-only").check("shell", {"command": cmd}).action == "deny"
    assert pol(tmp_path, "plan").check("shell", {"command": cmd}).action == "deny"
