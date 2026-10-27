"""永久允许规则（D4-8）：审批时选「本项目以后都允许」，规则写进 ~/.ee/permissions.json，按项目目录生效。

- 和 config.toml 的 permissions.allow 用同一套规则语法（shell(npm run *)、write(D:/data/*)、mcp__srv__tool…），
  会话开始时追加到 PermissionPolicy.allow。
- 只存在用户目录，不写进项目（不会被提交进仓库）；设置页可查看和删除。
- 由审批时的 Decision 推出规则：shell 用命令前缀（git commit → shell(git commit *)），工作区外写文件用目录，
  其他工具用工具名；没有前缀的强制询问（git push --force、改 .git 内部等）不能永久允许。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

from ee.config import ee_home

_LOCK = threading.Lock()


def _file(home: Path | None = None) -> Path:
    return (home or ee_home()) / "permissions.json"


def project_key(path: Path | str) -> str:
    p = os.path.normpath(str(Path(path).expanduser().resolve()))
    p = p.replace("\\", "/")
    return p.lower() if sys.platform == "win32" else p


def _load(home: Path | None = None) -> dict:
    f = _file(home)
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("projects", {})
            return data
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001  坏文件：当作空，不阻断
        pass
    return {"version": 1, "projects": {}}


def _save(data: dict, home: Path | None = None) -> None:
    from ee.core.atomic import write_text as atomic_write
    atomic_write(_file(home), json.dumps(data, ensure_ascii=False, indent=2))   # 原子写，Windows 上替换失败会重试


def rules_for(cwd: Path | str, home: Path | None = None) -> list[str]:
    """cwd 及其上级目录登记过的规则（在子目录开会话也生效）。"""
    data = _load(home)
    key = project_key(cwd)
    out: list[str] = []
    for proj, entry in (data.get("projects") or {}).items():
        if key == proj or key.startswith(proj.rstrip("/") + "/"):
            out += [str(r.get("rule")) for r in (entry.get("allow") or []) if r.get("rule")]
    return out


def add(cwd: Path | str, rule: str, home: Path | None = None, title: str = "") -> bool:
    """登记一条规则；已存在返回 False。"""
    rule = rule.strip()
    if not rule:
        return False
    with _LOCK:
        data = _load(home)
        entry = data["projects"].setdefault(project_key(cwd), {"allow": []})
        allow = entry.setdefault("allow", [])
        if any(r.get("rule") == rule for r in allow):
            return False
        allow.append({"rule": rule, "added": int(time.time()), "title": title[:200]})
        _save(data, home)
    return True


def remove(project: str, rule: str, home: Path | None = None) -> bool:
    with _LOCK:
        data = _load(home)
        entry = (data.get("projects") or {}).get(project)
        if not entry:
            return False
        old = entry.get("allow") or []
        new = [r for r in old if r.get("rule") != rule]
        if len(new) == len(old):
            return False
        if new:
            entry["allow"] = new
        else:
            data["projects"].pop(project, None)
        _save(data, home)
    return True


def list_all(home: Path | None = None) -> list[dict]:
    data = _load(home)
    out = []
    for proj, entry in sorted((data.get("projects") or {}).items()):
        for r in entry.get("allow") or []:
            out.append({"project": proj, "rule": r.get("rule", ""), "added": r.get("added"), "title": r.get("title", "")})
    return out


def rule_from_decision(tool: str, prefix: str) -> str | None:
    """审批时的 Decision → 永久规则；不适合永久允许的返回 None。"""
    prefix = (prefix or "").strip()
    if not prefix:
        return None
    if tool == "shell":
        return f"shell({prefix} *)"
    if tool in ("write", "edit", "apply_patch"):
        d = prefix.replace("\\", "/").rstrip("/")
        return f"{tool}({d}/*)"
    if prefix == tool:
        return tool
    return None
