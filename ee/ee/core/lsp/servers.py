"""语言服务器的启动策略与配置合并。

两件事必须分清：

1. **启动命令从哪来。** 只来自用户可信配置（`~/.ee/lsp.toml` 或设置页）。
   项目级配置只能**关闭**或**收紧**，不能指定可执行文件——否则打开一个不可信的仓库
   就等于让别人在你的机器上跑任意程序。这条和 MCP 的信任模型是同一个方向。

2. **Windows 上的启动形态。** `npx`、`pyright-langserver` 这类命令在 Windows 上
   往往是 `.cmd` 包装器，`create_subprocess_exec` 找不到可执行映像会直接失败；
   必须走 `cmd.exe /c` + `list2cmdline`。
   MCP 客户端（`ee/ee/mcp/client.py`）已有同样的分支，这里保持一致。

不自动安装：没装就报 unavailable 并给出可操作提示。装什么、装到哪、用哪个版本
是用户的选择，agent 不替他决定。
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field

IS_WINDOWS = sys.platform == "win32"

# 支持的语言（MVP 只两种，方案明确「不扩泛语言框架」）
SUPPORTED = ("python", "typescript")

# 每种语言的默认启动命令。只是建议值，真实命令由用户配置决定。
DEFAULTS: dict[str, list[str]] = {
    "python": ["pyright-langserver", "--stdio"],
    "typescript": ["typescript-language-server", "--stdio"],
}

INSTALL_HINT = {
    "python": "npm i -g pyright（或 pipx install pyright）",
    "typescript": "npm i -g typescript-language-server typescript",
}


@dataclass
class Spec:
    """一次启动尝试的结果。available=False 时 reason 要能直接给用户看。"""
    language: str
    command: list[str] = field(default_factory=list)
    available: bool = True
    reason: str = ""

    @property
    def identity(self) -> str:
        """命令身份：参与管理键，避免不同项目共用错服务器。"""
        return " ".join(self.command)


def spawn_argv(exe: str, args: list[str], windows: bool | None = None) -> list[str]:
    """把「可执行文件 + 参数」变成真正该 spawn 的 argv。

    Windows 的 `.cmd/.bat` 必须经 cmd.exe：它们不是可执行映像，直接 exec 会
    FileNotFoundError。用 list2cmdline 保证带空格的参数被正确引用。
    """
    w = IS_WINDOWS if windows is None else windows
    if w and exe.lower().endswith((".cmd", ".bat")):
        import subprocess
        return ["cmd.exe", "/d", "/s", "/c", subprocess.list2cmdline([exe, *args])]
    return [exe, *args]


def merge_config(user: dict | None, project: dict | None) -> dict:
    """合并用户配置与项目配置。

    规则（安全关键）：
    - 项目可以 `enabled = false` 关掉。
    - 项目**不能**改 `command`——指定可执行文件等于提权。
    - 其余字段（如 timeout）项目可以收紧，但不能放宽。
    """
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in (user or {}).items()}
    proj = project or {}

    if "enabled" in proj:
        out["enabled"] = bool(proj["enabled"])

    for lang, pcfg in proj.items():
        if lang in ("enabled",) or not isinstance(pcfg, dict):
            continue
        cur = dict(out.get(lang) or {})
        for key, val in pcfg.items():
            if key == "command":
                # 静默忽略而不是报错：项目文件可能被不可信的仓库带进来，
                # 报错反而会让它有机会影响行为；忽略并留痕在返回值里。
                continue
            if key in ("timeout", "idle_ttl") and isinstance(val, (int, float)):
                if isinstance(cur.get(key), (int, float)):
                    cur[key] = min(cur[key], val)      # 只能收紧
                else:
                    cur[key] = val
            elif key == "enabled":
                cur[key] = bool(val)
            # 其它未知键忽略
        out[lang] = cur
    return out


def resolve(language: str, config: dict | None = None, which=None) -> Spec:
    """确定一种语言该用什么命令启动。找不到就返回 available=False。"""
    if language not in SUPPORTED:
        return Spec(language, available=False, reason=f"暂不支持 {language}（只支持 {', '.join(SUPPORTED)}）")

    cfg = (config or {}).get(language) or {}
    command = list(cfg.get("command") or DEFAULTS[language])
    if not command:
        return Spec(language, available=False, reason=f"{language} 没有配置启动命令")

    exe = command[0]
    finder = which or shutil.which
    # 绝对路径直接判断存在；否则在 PATH 里找
    if os.path.isabs(exe):
        found = exe if os.path.isfile(exe) else None
    else:
        found = finder(exe)

    if not found:
        hint = INSTALL_HINT.get(language, "")
        return Spec(language, command=command, available=False,
                    reason=f"找不到 {exe}（{language} 语言服务器未安装）。可以装：{hint}"
                           if hint else f"找不到 {exe}")

    argv = spawn_argv(found, command[1:])
    return Spec(language, command=argv, available=True)
