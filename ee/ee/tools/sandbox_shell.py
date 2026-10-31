"""只读沙箱下的 shell 选择。

背景：Git Bash（cygwin）在 Windows 受限令牌下起不来。cygwin1.dll 启动时要给自己的
共享内存对象（名字形如 S-1-5-21-...-1001.1）设 DACL，而它不知道自己运行在受限令牌
里，写出的 DACL 不满足 WRITE_RESTRICTED 的检查，于是 CreateFileMapping 直接
`Win32 error 5`。本机实测：同一沙箱里 cmd.exe 和 powershell.exe 都能正常执行，
只有 bash 族失败——所以这不是「沙箱坏了」，是 cygwin 与受限令牌的模型冲突。

处理：沙箱启用且当前 shell 是 bash 族时，换成 powershell，并给出明确原因。
**不静默替换**：命令语法从 POSIX 变成 PowerShell，用户必须知道。
"""
from __future__ import annotations

# bash 族：在受限令牌下都会走 cygwin 的共享内存初始化
_BASH_KINDS = ("bash", "git-bash", "sh")

# 换过去的目标（按可用性从前往后挑）
_PS_KINDS = ("pwsh", "powershell")


def _available(kind: str) -> bool:
    try:
        from ee.tools.shellenv import resolve_shell
        resolve_shell(kind)
        return True
    except Exception:  # noqa: BLE001  没装就是不可用
        return False


def readonly_shell_override(kind: str, sandbox_active: bool) -> tuple[str, str]:
    """返回 (最终 shell kind, 给用户看的说明)。说明为空表示没有替换。

    sandbox_active=False（主会话 default 模式）时原样返回——那里 bash 又快又好，
    没有理由动它。
    """
    k = (kind or "auto").strip().lower().replace("_", "-")
    if k == "gitbash":
        k = "git-bash"
    if not sandbox_active or k not in _BASH_KINDS:
        return k, ""
    for cand in _PS_KINDS:
        if _available(cand):
            return cand, ("只读/计划模式的沙箱用的是受限令牌，Git Bash（cygwin）在受限令牌下"
                          f"起不来（CreateFileMapping 失败），已自动改用 {cand} 执行命令。"
                          "注意命令语法是 PowerShell 的，不是 bash 的。")
    # 一个 PowerShell 都没有：退回 cmd（也是 Win32 原生，能在沙箱里跑）
    if _available("cmd"):
        return "cmd", ("只读/计划模式的沙箱用的是受限令牌，Git Bash 在受限令牌下起不来，"
                       "系统里也没有 PowerShell，已改用 cmd 执行命令。")
    # 什么都没有：保持原样，让上层如实报错（不假装能用）
    return k, ""
