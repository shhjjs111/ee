"""探针：语言服务器是不是「不查诊断就不起、用完会回收」（2026-11-12，LSP 按需取用）。

跑法（必须隔离 EE_HOME，否则会读你真实的 ~/.ee/config.toml）：

    EE_HOME=$(mktemp -d) PYTHONUTF8=1 PYTHONPATH=ee ./.venv/Scripts/python.exe \
        ee/scripts/lsp_process_probe.py

它回答四个问题（Windows 上用 PowerShell 数 `node.exe` 里命令行为 pyright 的进程）：

1. 5 个会话「开了 LSP 但没查诊断」时，有没有语言服务器进程？（期望 0）
2. 第一次 `lsp` 调用之后有几个？（期望 1）
3. 空闲超过 TTL 之后还在不在？（期望 0——回收要杀整棵进程树，
   Windows 上 `pyright-langserver` 是 `.CMD`，只 terminate cmd 会留孤儿 node）
4. 全部 `aclose()` 之后呢？（期望 0）

为什么不用 `LspManager.stats()` 自证：那是我们自己的账本，进程杀不干净它同样显示已关闭。
这个探针数的是操作系统里的真进程。
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _ps() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def count_servers() -> int:
    """当前在跑的语言服务器进程数（只认 pyright，够本探针用）。"""
    if sys.platform.startswith("win"):
        exe = _ps()
        if not exe:
            raise SystemExit("找不到 pwsh / powershell，无法在 Windows 上数进程")
        ps = ("(Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" "
              "| Where-Object { $_.CommandLine -like '*pyright*' } | Measure-Object).Count")
        out = subprocess.run([exe, "-NoProfile", "-Command", ps], capture_output=True,
                             text=True, timeout=60).stdout.strip()
        return int(out or 0)
    out = subprocess.run(["pgrep", "-fc", "pyright-langserver"], capture_output=True, text=True)
    return int(out.stdout.strip() or 0)


async def main() -> None:
    from ee.config import load_config
    from ee.core.session import Session
    from ee.core.lsp import servers as S
    from ee.tools.base import ToolContext, ToolState

    ws = Path(tempfile.mkdtemp()) / "ws"
    ws.mkdir(parents=True)
    (ws / "a.py").write_text("x: int = 'hello'\n", encoding="utf-8")
    spec = S.resolve("python", {})
    if not spec.available:
        raise SystemExit(f"pyright 不可用：{spec.reason}（先 npm i -g pyright）")

    cfg = load_config(ws)
    cfg.data.setdefault("lsp", {})["enabled"] = True
    ttl = 2.0
    sessions = []
    print("基线进程数:", count_servers())
    for _ in range(5):
        s = Session(cfg, cwd=ws, persist=False)
        await s._attach_lsp(lambda _e: None)
        mgr = s.lsp_manager
        assert mgr is not None, "LSP 没有接线"
        mgr.idle_ttl = ttl          # 探针里把 TTL 调短，不用等两分钟
        sessions.append(s)
    print("5 个会话接线后（未查诊断）:", count_servers())

    ctx = ToolContext(cwd=ws, state=ToolState(), output_dir=ws / ".out",
                      extra={"session": sessions[0]})
    r = await sessions[0].tool_map["lsp"].run({"path": "a.py"}, ctx)
    print("第一次查诊断:", r.meta.get("status"), "| 进程数:", count_servers())
    for _ in range(24):
        await asyncio.sleep(0.5)
        if count_servers() == 0:
            break
    print(f"空闲 {ttl:.0f}s（TTL）后:", count_servers())
    for s in sessions:
        await s.aclose()
    print("全部 aclose 后:", count_servers())


if __name__ == "__main__":
    asyncio.run(main())
