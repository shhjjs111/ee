"""shell 选择：config.toml 的 shell = "auto" | "pwsh" | "powershell" | "git-bash" | "cmd" | "bash"。

Windows 是首要平台：auto = pwsh → powershell.exe；git-bash 在常见安装路径或 PATH 里找 bash.exe
（跳过 System32\\bash.exe，那是 WSL）。非 Windows 的 auto = bash（没有则 sh）。
"""
from __future__ import annotations

import os
import re
import tempfile
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

    def prepare(self, command: str) -> tuple[list[str], Path | None]:
        """返回 (argv, 用完要删的临时脚本)。

        PowerShell 用临时 .ps1 + -File：-EncodedCommand / -Command 在 stderr 被重定向时会把错误流
        序列化成 CLIXML（Windows 真机 CI 上踩到），-File 输出的是普通文本，也没有引号转义问题。
        """
        if self.syntax == "powershell":
            prelude = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
                       "$OutputEncoding=[System.Text.Encoding]::UTF8;$ProgressPreference='SilentlyContinue'\n")
            # 把最后一条命令的失败带成进程退出码（原生命令看 $LASTEXITCODE，cmdlet 看 $?）
            epilogue = ("\n$__ee_ok=$?\n"
                        "if ($LASTEXITCODE) { exit $LASTEXITCODE } elseif (-not $__ee_ok) { exit 1 }\n")
            fd, name = tempfile.mkstemp(prefix="ee-", suffix=".ps1")
            with os.fdopen(fd, "w", encoding="utf-8-sig", newline="\r\n") as f:  # 5.1 需要 BOM 才按 UTF-8 读
                f.write(prelude + command + epilogue)
            path = Path(name)
            return [self.exe, "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-File", str(path)], path
        return self.argv(command), None

    def argv(self, command: str) -> list[str]:
        """cmd / posix 的命令行；PowerShell 请用 prepare()。"""
        if self.syntax == "powershell":
            raise ValueError("PowerShell 需要用 prepare()（临时脚本）")
        if self.syntax == "cmd":
            return [self.exe, "/d", "/s", "/c", "chcp 65001>nul & " + command]
        return [self.exe, "-c", command]


_CLIXML_S = re.compile(r'<S S="Error">(.*?)</S>', re.S)


def strip_clixml(out: str) -> str:
    """兜底：万一输出里混进了 PowerShell 的 CLIXML 错误流，还原成普通文本。"""
    i = out.find("#< CLIXML")
    if i < 0:
        return out
    head, tail = out[:i], out[i + len("#< CLIXML"):]
    msgs = _CLIXML_S.findall(tail)
    text = "".join(msgs)
    text = re.sub(r"_x([0-9A-Fa-f]{4})_", lambda m: chr(int(m.group(1), 16)), text)
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&")
    end = tail.find("</Objs>")
    rest = tail[end + len("</Objs>"):] if end >= 0 else ""
    return head + text + rest


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
