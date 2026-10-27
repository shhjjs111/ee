"""只读 / 计划模式：PowerShell 管道操作符不能用参数偷偷执行动作。

缺陷（探针已复现）：`_READ_ONLY` 白名单里有 foreach-object / where-object / select-object /
measure-object 等，`_segment_safe()` 对它们直接返回 True（`SAFE_COMMANDS[name] is None`
就放行），**完全不看参数**。于是这些命令可以把“动作”藏在参数里：

    Get-ChildItem C:\\ -Recurse -File | ForEach-Object Delete      → 删光
    Get-Process | ForEach-Object Kill                             → 杀进程
    Get-Process | ForEach-Object -MemberName Kill                 → 同上，更隐蔽

`{ }` 脚本块会被拦（已有测试覆盖），但**不带脚本块的成员调用形式**从来没有检查过。

修法：这几个 cmdlet 的“成员调用名”参数一律视为不安全；只有纯投影/过滤用法
（-Property、-First、-Last、-Skip、比较参数等不带裸标识符的参数）才放行。
"""
import pytest

from ee.core.permissions import PermissionPolicy


def _policy(tmp_path, mode="read-only", shell="powershell"):
    return PermissionPolicy(mode=mode, workspace=tmp_path, shell=shell)


# 每个 cmdlet 都有多个别名，全都要覆盖
DANGEROUS = [
    # ForEach-Object / %
    "Get-ChildItem C:\\ -Recurse -File | ForEach-Object Delete",
    "Get-Process | ForEach-Object Kill",
    "Get-Process | ForEach-Object -MemberName Kill",
    "Get-Process | ForEach-Object -MemberName Stop-Process",
    "Get-ChildItem | foreach Delete",
    "Get-ChildItem | % Delete",
    "Get-Process | ForEach-Object -Process Kill",
    # Where-Object / ?：过滤只是表象，把动作放在 -Value/-FilterScript 之外一样能生效
    "Get-ChildItem | Where-Object -InputObject (Get-Item x) Remove-Item",
    "Get-Process | ? Kill",
    # Select-Object 的 -ExpandProperty 也能取到方法
    "Get-Process | Select-Object -ExpandProperty Kill",
    # PowerShell 的冒号赋值形式：-MemberName:Kill（值直接跟在参数名后面）
    "Get-Process | ForEach-Object -MemberName:Kill",
    "Get-Process | ForEach-Object -MemberName:Stop-Process",
    "Get-Process | ForEach-Object -Process:Stop-Process",
    "Get-Process | ForEach-Object -Process:Kill",
    "Get-Process | Select-Object -ExpandProperty:Kill",
    # 冒号形式 + 简写
    "Get-Process | % -MemberName:Kill",
    "Get-Process | ForEach-Object -memb:Kill",
    # 变量 / 动态表达式当成员名：值在运行期才知道，静态看必须是动作
    # （$env:MYACTION 可以是任意命令名；$x.Kill 是属性取值再当命令）
    "Get-Process | ForEach-Object $env:MYACTION",
    "Get-Process | ForEach-Object $env:X",
    "Get-Process | ForEach-Object $x.Kill",
    "Get-Process | ForEach-Object $x",
    "Get-Process | % $env:MYACTION",
    "Get-Process | ForEach-Object -MemberName $env:X",
    "Get-Process | ForEach-Object -MemberName $x",
    "Get-Process | Select-Object -ExpandProperty $env:X",
    # 子表达式 / 表达式形参
    "Get-Process | ForEach-Object $(echo Kill)",
    "Get-Process | ForEach-Object ${env:X}",
    "Get-Process | Where-Object $env:X",
]


@pytest.mark.parametrize("cmd", DANGEROUS)
def test_readonly_blocks_member_invocation(tmp_path, cmd):
    """只读模式：带动作成员名的管道操作符必须被拒。"""
    assert _policy(tmp_path).check("shell", {"command": cmd}).action == "deny", cmd


@pytest.mark.parametrize("cmd", DANGEROUS)
def test_plan_blocks_member_invocation(tmp_path, cmd):
    """计划模式同样只读。"""
    assert _policy(tmp_path, "plan").check("shell", {"command": cmd}).action == "deny", cmd


@pytest.mark.parametrize("cmd", DANGEROUS)
def test_default_mode_asks(tmp_path, cmd):
    """默认模式下不能再“直接放行”，至少要弹确认。"""
    assert _policy(tmp_path, "default").check("shell", {"command": cmd}).action == "ask", cmd


# ---------------------------------------------------------------- 不能修坏正常用法


SAFE = [
    "Get-ChildItem -Recurse | Select-Object Name",
    "Get-ChildItem -Recurse | Select-Object -First 10",
    "Get-ChildItem | Select-Object -Property Name, Length",
    "Get-ChildItem | Where-Object Name -like '*.py'",
    "Get-ChildItem | ForEach-Object Name",
    "Get-ChildItem | ForEach-Object -MemberName Name",
    "Get-ChildItem | Measure-Object",
    "Get-ChildItem | Sort-Object Name",
    "Get-ChildItem | Format-Table Name",
]


@pytest.mark.parametrize("cmd", SAFE)
def test_normal_pipelines_still_allowed(tmp_path, cmd):
    """正常的投影 / 过滤 / 统计不能因为收紧而全被拒。"""
    p = _policy(tmp_path, "read-only")
    got = p.check("shell", {"command": cmd}).action
    assert got == "allow", f"{cmd} -> {got}"
