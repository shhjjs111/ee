"""shell 选择：config.toml 的 shell = "auto" | "pwsh" | "powershell" | "git-bash" | "cmd" | "bash"。

Windows 是首要平台：auto = pwsh → powershell.exe；git-bash 在常见安装路径或 PATH 里找 bash.exe
（跳过 System32\\bash.exe，那是 WSL）。非 Windows 的 auto = bash（没有则 sh）。
"""
from __future__ import annotations

import base64
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

SHELL_CHOICES = ["auto", "pwsh", "powershell", "git-bash", "cmd", "bash"]
IS_WINDOWS = sys.platform.startswith("win")


class ShellNotFound(RuntimeError):
    pass


@dataclass(frozen=True)
class ShellInfo:
    kind: str        # pwsh | powershell | cmd | bash | git-bash
    exe: str
    display: str     # 给模型看的名字，如 "PowerShell 7 (pwsh)"

    @property
    def syntax(self) -> str:
        """命令语法家族：powershell | cmd | posix。"""
        if self.kind in ("pwsh", "powershell"):
            return "powershell"
        if self.kind == "cmd":
            return "cmd"
        return "posix"

    def argv(self, command: str) -> list[str]:
        if self.syntax == "powershell":
            prelude = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
                       "$OutputEncoding=[System.Text.Encoding]::UTF8;$ProgressPreference='SilentlyContinue';")
            # -EncodedCommand（UTF-16LE base64）避开 Windows 命令行引号转义的各种坑
            enc = base64.b64encode((prelude + command).encode("utf-16-le")).decode("ascii")
            return [self.exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc]
        if self.syntax == "cmd":
            return [self.exe, "/d", "/s", "/c", "chcp 65001>nul & " + command]
        return [self.exe, "-c", command]


def _which(name: str) -> str | None:
    return shutil.which(name)


def find_git_bash() -> str | None:
    cands = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(env)
        if base:
            cands.append(Path(base) / "Git" / "bin" / "bash.exe")
    la = os.environ.get("LOCALAPPDATA")
    if la:
        cands.append(Path(la) / "Programs" / "Git" / "bin" / "bash.exe")
    cands += [Path(r"C:\Program Files\Git\bin\bash.exe"), Path(r"C:\Program Files (x86)\Git\bin\bash.exe")]
    git = _which("git")
    if git:  # ...\Git\cmd\git.exe → ...\Git\bin\bash.exe
        cands.append(Path(git).resolve().parent.parent / "bin" / "bash.exe")
    for c in cands:
        if c.is_file():
            return str(c)
    b = _which("bash")
    if b and "system32" not in b.lower():
        return b
    return None


def resolve_shell(name: str = "auto") -> ShellInfo:
    name = (name or "auto").lower().replace("_", "-")
    if name == "gitbash":
        name = "git-bash"
    if name not in SHELL_CHOICES:
        raise ShellNotFound(f"未知 shell 设置：{name}（可选：{', '.join(SHELL_CHOICES)}）")
    if name == "auto":
        if IS_WINDOWS:
            for n in ("pwsh", "powershell"):
                try:
                    return resolve_shell(n)
                except ShellNotFound:
                    continue
            return resolve_shell("cmd")
        for n in ("bash",):
            exe = _which(n)
            if exe:
                return ShellInfo("bash", exe, "bash")
        return ShellInfo("bash", _which("sh") or "/bin/sh", "sh")
    if name == "pwsh":
        exe = _which("pwsh")
        if not exe:
            raise ShellNotFound("找不到 pwsh（PowerShell 7）。可安装：winget install Microsoft.PowerShell，或把 shell 设为 powershell。")
        return ShellInfo("pwsh", exe, "PowerShell 7 (pwsh)")
    if name == "powershell":
        exe = _which("powershell") or _which("powershell.exe")
        if not exe:
            raise ShellNotFound("找不到 powershell.exe（Windows PowerShell 5.1）。")
        return ShellInfo("powershell", exe, "Windows PowerShell 5.1")
    if name == "cmd":
        exe = os.environ.get("COMSPEC") or _which("cmd") or "cmd.exe"
        return ShellInfo("cmd", exe, "cmd.exe")
    if name == "git-bash":
        exe = find_git_bash()
        if not exe:
            raise ShellNotFound(r"找不到 Git Bash：已检查 C:\Program Files\Git\bin\bash.exe、"
                                r"%LOCALAPPDATA%\Programs\Git\bin\bash.exe 和 PATH 里的 bash.exe。"
                                "请安装 Git for Windows，或把 shell 改回 auto / pwsh。")
        return ShellInfo("git-bash", exe, "Git Bash")
    exe = _which("bash")
    if not exe or (IS_WINDOWS and "system32" in exe.lower()):
        raise ShellNotFound("找不到 bash。Windows 上请用 git-bash 或 pwsh。")
    return ShellInfo("bash", exe, "bash")
