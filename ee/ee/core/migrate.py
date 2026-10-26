"""用户级 config.toml 的版本迁移（D3-C）。

- 文件顶层 `config_version = N` 记录版本；缺省视为 1（D2 及以前写出的文件）。
- MIGRATIONS 是版本链 [(目标版本, 函数)]，函数收到 (data, writes) ：改 data（内存里的值），
  并把要写回文件的改动 append 到 writes（("web.search_backend", "exa") 这种点路径）。
- 写回用 config.write_setting 逐键改行，保留用户文件原有注释；第一次迁移前把原文件备份为
  config.toml.pre-v<目标版本>.bak（已存在则不覆盖）。
- 只迁移用户级文件；文件不存在（新用户）什么都不做。新写出的文件由 settings.set_settings
  直接带上 CURRENT_VERSION，所以不会被迁移二次改动。
- 迁移失败（文件坏了、只读等）只记日志，不阻止启动。
"""
from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Callable

log = logging.getLogger("ee.migrate")

Writes = list[tuple[str, object]]


def _v2(data: dict, writes: Writes) -> None:
    """D2 早期默认用 DeepSeek 付费搜索；现在默认免费的 Exa + 直接抓取。"""
    w = data.get("web")
    if not isinstance(w, dict):
        return
    if w.get("search_backend") == "deepseek":
        w["search_backend"] = "exa"
        writes.append(("web.search_backend", "exa"))
    if w.get("fetch_backend") == "auto":
        w["fetch_backend"] = "direct"
        writes.append(("web.fetch_backend", "direct"))


STRIP_SECRETS = "__strip_secret_values__"


def _v3(data: dict, writes: Writes) -> None:
    """D3-F：config.toml 里的明文 key 迁进凭据存储（Windows 凭据管理器），文件里删掉那几行。"""
    from ee.core.secrets import move_plain_keys
    n, moved = move_plain_keys(data)
    if moved:
        writes.append((STRIP_SECRETS, moved))
        log.info("已把 %d 个 API key 迁移到凭据存储", n)


MIGRATIONS: list[tuple[int, Callable[[dict, Writes], None]]] = [
    (2, _v2),
    (3, _v3),
]
CURRENT_VERSION = MIGRATIONS[-1][0]


def file_version(data: dict) -> int:
    try:
        return int(data.get("config_version", 1))
    except (TypeError, ValueError):
        return 1


def migrate_data(data: dict) -> tuple[dict, Writes, int]:
    """纯函数式地跑迁移链：返回 (迁移后的 data, 需写回的改动, 起始版本)。data 原地修改。"""
    start = file_version(data)
    writes: Writes = []
    for ver, fn in MIGRATIONS:
        if start < ver:
            fn(data, writes)
    if start < CURRENT_VERSION:
        data["config_version"] = CURRENT_VERSION
    return data, writes, start


def migrate_user_file(path: Path | None = None) -> bool:
    """迁移用户级 config.toml（就地写回）。返回是否改动了文件。"""
    from ee.config import user_config_path, write_setting

    path = path or user_config_path()
    if not path.is_file():
        return False
    try:
        raw = path.read_bytes()
        data = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        log.warning("config 迁移跳过：读取 %s 失败：%s", path, e)
        return False
    _, writes, start = migrate_data(data)
    if start >= CURRENT_VERSION:
        return False
    from ee.core.secrets import scrub_backups, strip_secret_lines
    try:
        bak = path.parent / f"{path.name}.pre-v{CURRENT_VERSION}.bak"
        if not bak.exists():
            bak.write_text(strip_secret_lines(raw.decode("utf-8")), encoding="utf-8")   # 备份不留明文 key
        for dotted, value in writes:
            if dotted == STRIP_SECRETS:
                path.write_text(strip_secret_lines(path.read_text(encoding="utf-8"), value), encoding="utf-8")
            else:
                write_setting(dotted, value, path)
        write_setting("config_version", CURRENT_VERSION, path)
    except OSError as e:
        log.warning("config 迁移写回失败（本次仅在内存里生效）：%s", e)
        return False
    scrub_backups(path)
    log.info("config.toml 已从 v%s 迁移到 v%s（%d 处改动）", start, CURRENT_VERSION, len(writes))
    return True
