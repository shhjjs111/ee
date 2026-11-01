"""工具输出归档的保留策略（`~/.ee/tool-output`）。

**问题**：超长工具输出（`tools/truncate.py` 归档全文）和后台命令日志（`core/bgproc.py`）都写进
这个目录，此前只进不出，一次会话就能留下几个文件，长期累积。

**边界**：只按文件名白名单动手——我们自己写出的两种形态，其余文件一概不碰。目录在用户 home 下，
误删代价远大于多留几个旧文件：

  - 归档：`YYYYMMDD-HHMMSS-<tool>-<6 hex>.txt`（truncate_output_info）
  - 后台日志：`bg-<owner>-<id>.log`（BgManager.start）

**取舍**：超过保留期的旧会话里，工具结果那句「完整输出已保存到 …」会变成死链：界面上的
「完整输出」入口点不开（条目本身和会话都还在），模型重新 `read` 也会得到「文件不存在」。
默认 30 天（本机实测一天约 5.6MB，一个月百来兆量级）；`[tool_output] keep_days = 0` 关闭。
一天最多跑一次（stamp 文件），与影子仓库清理（`core/checkpoints.py`）同一套做法。
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger("ee.retention")

DEFAULT_DAYS = 30
INTERVAL = 86400                     # 两次清理的最小间隔（秒）
_STAMP = ".gc-stamp"

_ARCHIVE = re.compile(r"^\d{8}-\d{6}-[a-z_]+-[0-9a-f]{6}\.txt$")
_BGLOG = re.compile(r"^bg-[0-9a-zA-Z]+-[0-9a-zA-Z]+\.log$")


def settings(cfg_data: dict | None) -> float:
    """读 [tool_output] keep_days，默认 30；坏值回落默认，负数按 0（关闭）处理。"""
    sec = (cfg_data or {}).get("tool_output")
    raw = sec.get("keep_days", DEFAULT_DAYS) if isinstance(sec, dict) else DEFAULT_DAYS
    try:
        days = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return 0.0 if days < 0 else days


def _is_ours(name: str) -> bool:
    return bool(_ARCHIVE.match(name) or _BGLOG.match(name))


def gc(home: Path, keep_days: float | None = None, force: bool = False,
       now: float | None = None) -> dict:
    """删除 tool-output 里超过保留期、且文件名是本模块登记过的文件。返回统计。"""
    now = time.time() if now is None else now
    days = DEFAULT_DAYS if keep_days is None else float(keep_days)
    stats = {"skipped": False, "removed": 0, "kept": 0, "bytes": 0, "errors": []}
    if days <= 0:
        return {**stats, "skipped": True}
    root = Path(home) / "tool-output"
    if not root.is_dir():
        return {**stats, "skipped": True}
    stamp = root / _STAMP
    if not force:
        try:
            if now - stamp.stat().st_mtime < INTERVAL:
                return {**stats, "skipped": True}
        except OSError:
            pass
    cutoff = now - days * 86400
    for p in root.iterdir():
        try:
            if not p.is_file() or not _is_ours(p.name):
                continue
            st = p.stat()
            if st.st_mtime > cutoff:
                stats["kept"] += 1
                continue
            p.unlink()
            stats["removed"] += 1
            stats["bytes"] += st.st_size
        except OSError as e:
            stats["errors"].append(f"{p.name}：{e}")
    try:
        stamp.write_text(json.dumps({"t": now}), encoding="utf-8")
    except OSError:
        pass
    return stats
