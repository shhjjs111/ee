"""repo_map 工具：在 token 预算内输出按引用关系排名的“文件 → 关键定义签名”地图（只读）。"""
from __future__ import annotations

import asyncio

from ee.tools.base import Tool, ToolContext, ToolError, ToolResult

MAX_TOKENS_LIMIT = 8000


class RepoMapTool(Tool):
    name = "repo_map"
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "只看这个子目录，默认整个项目"},
            "focus": {"type": "array", "items": {"type": "string"},
                      "description": "关注的文件或符号名，相关定义排在前面"},
            "max_tokens": {"type": "integer", "description": "输出预算（token），默认约 1500，最大 8000"},
        },
    }

    def __init__(self, cfg: dict | None = None):
        super().__init__()
        from ee.core.repomap import repo_config
        self.cfg = repo_config({"repo_map": cfg or {}})

    def summary(self, args: dict) -> str:
        p = args.get("path")
        return "Map repo" + (f" {p}" if p else "")

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        from ee.config import ee_home
        from ee.core import repomap as RM
        home = (ctx.extra or {}).get("home") or ee_home()
        try:
            budget = int(args.get("max_tokens") or self.cfg.get("max_tokens", 1536))
        except (TypeError, ValueError):
            raise ToolError("max_tokens 必须是整数")
        budget = max(200, min(budget, MAX_TOKENS_LIMIT))
        focus = args.get("focus") or []
        if isinstance(focus, str):
            focus = [focus]
        focus = [str(f) for f in focus if str(f).strip()][:20]
        subdir = ""
        cwd = ctx.cwd
        root = RM.map_root(cwd)
        if args.get("path"):
            p = ctx.resolve(str(args["path"]))
            if not p.is_dir():
                raise ToolError(f"目录不存在：{p}")
            if root is None:          # 工作目录太大（主目录/盘根）：以给定目录为根
                cwd, root = p, p
            try:
                subdir = p.resolve().relative_to(root).as_posix()
            except ValueError:
                cwd, subdir = p, ""   # 在项目外：以该目录（或它所在的 git 项目）为根
                r2 = RM.map_root(p)
                if r2 is not None and r2 != p.resolve():
                    subdir = p.resolve().relative_to(r2).as_posix()
        text, info = await asyncio.to_thread(RM.build_map, cwd, home, budget, focus, subdir, self.cfg)
        summ = self.summary(args)
        if info.get("unavailable"):
            return ToolResult(f"仓库地图不可用：{info['unavailable']}。请改用 glob / grep / read 浏览项目。",
                              summary=summ, meta={"kind": "repo_map", "unavailable": True})
        if not text:
            return ToolResult("没有找到可解析的源代码文件（支持的语言见工具说明），请改用 glob / grep。",
                              summary=summ, meta={"kind": "repo_map", "files": 0})
        notes = [f"仓库地图（{info['root']}；{info['files_listed']} 个源文件，显示 {info['files_shown']} 个文件里"
                 f"排名最前的 {info['defs_shown']}/{info['defs_total']} 个定义，约 {info['tokens']} token）"]
        if info["files_total"] > info["files_listed"]:
            notes.append(f"（项目共 {info['files_total']} 个源文件，超过上限只分析了浅层的 {info['files_listed']} 个；"
                         "可用 path 缩小范围）")
        if info.get("timed_out"):
            notes.append("（解析超时，部分文件没有进入地图；再次调用会继续解析）")
        if info.get("missing_langs"):
            notes.append(f"（这些语言的解析器不可用，已跳过：{', '.join(info['missing_langs'])}）")
        return ToolResult("\n".join(notes) + "\n\n" + text.rstrip(), summary=summ,
                          meta={"kind": "repo_map", "files": info["files_shown"], "defs": info["defs_shown"],
                                "tokens": info["tokens"]})
