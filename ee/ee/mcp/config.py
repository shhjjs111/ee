"""MCP 配置。

config.toml：
  [mcp]
  enabled = true
  startup_timeout = 20          # 每个服务器连接 + 列工具的超时（秒）
  tool_timeout = 120            # 单次工具调用超时
  project_json = true           # 读取项目里的 .mcp.json
  allowed_project = ["名字:sha8", ...]   # 用户允许过的项目级服务器（配置变了要重新允许）

  [mcp.servers.<名>]
  command = "npx" ; args = [...] ; env = {..} ; cwd = "..."      # stdio
  url = "https://..." ; headers = {..} ; type = "http" | "sse"  # 远程
  enabled = true ; trusted = false                              # trusted：调用不再询问

项目 .mcp.json（从工作区往上找到 git 根为止的第一个）：{"mcpServers": {"名": {同上字段}}}
同名时用户配置覆盖项目配置。值里支持 ${VAR} 和 ${VAR:-默认}。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MCP = {"enabled": True, "startup_timeout": 20, "tool_timeout": 120, "project_json": True,
               "allowed_project": []}
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_vars(v, env: dict | None = None):
    env = os.environ if env is None else env
    if isinstance(v, str):
        return _VAR.sub(lambda m: env.get(m.group(1)) or (m.group(2) or ""), v)
    if isinstance(v, list):
        return [expand_vars(x, env) for x in v]
    if isinstance(v, dict):
        return {k: expand_vars(x, env) for k, x in v.items()}
    return v


@dataclass
class ServerConfig:
    name: str
    transport: str                    # stdio | http | sse
    command: str = ""
    args: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: dict = field(default_factory=dict)
    enabled: bool = True
    trusted: bool = False
    source: str = "user"              # user | project
    raw: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        """配置指纹（项目服务器允许记录、连接缓存都用它）。"""
        blob = json.dumps(self.raw, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:8]

    @property
    def allow_id(self) -> str:
        return f"{self.name}:{self.key}"

    def summary(self) -> dict:
        return {"name": self.name, "transport": self.transport, "command": self.command, "args": self.args,
                "url": self.url, "enabled": self.enabled, "trusted": self.trusted, "source": self.source,
                "allow_id": self.allow_id}


def parse_server(name: str, d: dict, source: str = "user") -> ServerConfig:
    if not NAME_RE.match(name):
        raise ValueError(f"MCP 服务器名只能用字母、数字、_ 和 -（≤40 字）：{name}")
    d = dict(d or {})
    raw = {k: v for k, v in d.items() if k not in ("enabled", "trusted")}
    e = expand_vars(d)
    url = str(e.get("url") or e.get("serverUrl") or "").strip()
    typ = str(e.get("type") or e.get("transport") or "").strip().lower()
    if url:
        transport = "sse" if typ == "sse" or (not typ and url.rstrip("/").endswith("/sse")) else "http"
    else:
        transport = "stdio"
        if not str(e.get("command") or "").strip():
            raise ValueError(f"MCP 服务器 {name} 缺少 command 或 url")
    args = e.get("args") or []
    if isinstance(args, str):
        args = args.split()
    return ServerConfig(name=name, transport=transport, command=str(e.get("command") or "").strip(),
                        args=[str(a) for a in args], env={str(k): str(v) for k, v in (e.get("env") or {}).items()},
                        cwd=str(e.get("cwd") or ""), url=url,
                        headers={str(k): str(v) for k, v in (e.get("headers") or {}).items()},
                        enabled=bool(d.get("enabled", True)), trusted=bool(d.get("trusted", False)),
                        source=source, raw=raw)


def mcp_settings(data: dict) -> dict:
    return {**DEFAULT_MCP, **(data.get("mcp") or {})}


def find_project_json(cwd: Path | None) -> Path | None:
    if cwd is None:
        return None
    try:
        cur = Path(cwd).resolve()
    except OSError:
        return None
    for d in [cur, *cur.parents]:
        p = d / ".mcp.json"
        if p.is_file():
            return p
        if (d / ".git").exists():
            break
    return None


def load_servers(data: dict, cwd: Path | None) -> tuple[list[ServerConfig], list[str]]:
    """返回 (服务器列表, 出错说明)。项目服务器未被允许时 enabled=False（界面可一键允许）。"""
    m = mcp_settings(data)
    errors: list[str] = []
    out: dict[str, ServerConfig] = {}
    if m.get("project_json", True):
        pj = find_project_json(cwd)
        if pj is not None:
            try:
                servers = (json.loads(pj.read_text(encoding="utf-8")) or {}).get("mcpServers") or {}
            except (OSError, ValueError) as e:
                errors.append(f"读取 {pj} 失败：{e}")
                servers = {}
            allowed = set(m.get("allowed_project") or [])
            for name, d in servers.items():
                try:
                    sc = parse_server(str(name), d, source="project")
                except ValueError as e:
                    errors.append(str(e))
                    continue
                if sc.allow_id not in allowed:
                    sc.enabled = False
                out[sc.name] = sc
    for name, d in (m.get("servers") or {}).items():
        try:
            out[str(name)] = parse_server(str(name), d, source="user")
        except ValueError as e:
            errors.append(str(e))
    return list(out.values()), errors
