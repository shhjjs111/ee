"""主循环（asyncio）：一个用户回合 = 组装请求 → 流式调用 → 收集 tool_calls → 执行 → 追加结果 → 再调用。

- 结束：模型这一步没有工具调用（且没有排队的用户输入）。
- 安全上限 max_steps；到最后一步追加“只用文字总结，不再调工具”。
- 只读工具同一步并行；写操作按顺序。每个 tool_call_id 必有输出（中断写“用户中断”）。工具错误原样回喂。
- interrupt()：取消当前流/工具，保留已产生的历史，可继续对话。
- queue_input()：运行中继续打字，当前步结束后作为新 user 消息送入。
界面只通过 run_turn() 产出的事件流与内核交互。
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import AsyncIterator, Callable

from ee.config import Config, ee_home
from ee.core import compaction as C
from ee.core import gitinfo
from ee.core import hooks
from ee.core.instructions import load_instructions
from ee.core.memory import load_memory
from ee.core.loopguard import REPEAT_NOTE, REPEAT_STOP, LoopGuard, signature
from ee.core.context import ContextUpdates, FrozenPrefix, UsageStats, env_parts, load_prompt, system_prompt
from ee.core.events import (ApprovalDecision, ApprovalRequest, AssistantMessage, Compacted, Error, Notice, PlanUpdate,
                            ReasoningDelta, Status, StepStart, TextDelta, ToolEnd, ToolOutput, ToolStart, TurnDone,
                            TurnStart, Usage)
from ee.core.permissions import MODE_LABEL, Decision, PermissionPolicy
from ee.core.providers import transforms as T
from ee.core.providers.openai_chat import ChatClient, Completion
from ee.core.store import SessionStore
from ee.tools import ToolContext, ToolError, ToolResult, ToolState, build_tools
from ee.tools.shellenv import ShellNotFound, resolve_shell
from ee.tools.truncate import truncate_output

log = logging.getLogger("ee.session")

LAST_STEP_NOTE = ("[系统提示] 已达到本回合允许的最大步数。请不要再调用任何工具，"
                  "只用文字总结目前的进展、结果、尚未完成的部分和建议的下一步。")
INTERRUPTED = "用户中断：这次工具调用没有执行完（或没有执行）。"


class Session:
    def __init__(self, config: Config, model: str | None = None, cwd: Path | None = None, mode: str | None = None,
                 client=None, client_factory: Callable | None = None, persist: bool = True,
                 shell: str | None = None, home: Path | None = None):
        self.config = config
        self.cwd = Path(cwd or config.cwd).resolve()
        self.home = home or ee_home()
        self._client_override = client
        self._client_factory = client_factory
        self._client = None
        self.spec = config.resolve_model(model)
        self.tools = build_tools(self.spec.profile, config)
        self.tool_map = {t.name: t for t in self.tools}
        self.prefix: FrozenPrefix | None = None
        self.history: list[dict] = []
        self.updates = ContextUpdates()
        self.stats = UsageStats()
        self.state = ToolState()
        from ee.core.bgproc import BgManager
        import uuid as _uuid
        self.bg = BgManager(self.home / "tool-output", owner=_uuid.uuid4().hex[:8])   # 后台命令（D4-6）
        perms = config.permissions
        self.policy = PermissionPolicy(mode=mode or perms.get("mode", "default"), workspace=self.cwd,
                                       allow=list(perms.get("allow", [])), ask=list(perms.get("ask", [])),
                                       deny=list(perms.get("deny", [])))
        self.shell_name = shell or config.data.get("shell", "auto")
        self.shell = None
        self.notices: list[str] = []
        try:
            self.shell = resolve_shell(self.shell_name)
        except ShellNotFound as e:
            self.notices.append(str(e))
            try:
                self.shell = resolve_shell("auto")
            except ShellNotFound:
                self.shell = None
        self.git = gitinfo.status(self.cwd)
        self.gh = gitinfo.gh_exe() is not None
        self.pending: list[str] = []
        self.notes: list[str] = []           # 例如用户 !命令 的输出，随下一条消息发给模型
        self.turns = 0
        self.turn_starts: list[int] = []     # 每个回合首条 user 消息在 history 里的位置（回退用）
        # D4-1 磁盘快照：每回合 {"before": tree, "after": tree|None, "undone": bool}；重启后由 threads 从条目重建
        self.turn_trees: list[dict] = []
        self.disk_enabled = bool((config.data.get("snapshot") or {}).get("disk", True))
        self._disk_obj = None
        self.last_undo_tree: str | None = None
        import uuid
        self.snap_epoch = uuid.uuid4().hex[:8]   # 内存快照层号只在本次进程内有效（重启后层号会撞）
        self.store = SessionStore(self.home, enabled=persist)
        self._task: asyncio.Task | None = None
        self._open_calls: list[str] = []
        self._results: dict[str, str] = {}
        self._ok: dict[str, bool] = {}
        self._approval_lock = asyncio.Lock()
        self.last_reasoning = ""
        self.running = False
        self.instruction_sources: list[str] = []   # 本会话加载的 AGENTS.md 路径（界面可显示）
        self.memory_sources: list[str] = []        # 本会话注入的记忆文件
        self.guard = LoopGuard()
        self._fresh_usage = False                   # 上次压缩后是否拿到过新的用量（防止反复压缩）
        self.compactions = 0
        # MCP（D3-A）：首次冻结前缀前连接服务器，工具追加在工具表末尾，会话内不变
        self.mcp_enabled = True
        self.mcp_tools: list = []
        self.mcp_states: list[dict] = []
        self._mcp_done = False

    # ------------------------------------------------------------ 配置切换
    @property
    def profile(self):
        return self.spec.profile

    def client(self):
        if self._client is None:
            if self._client_override is not None:
                self._client = ChatClient(self.spec, client=self._client_override)
            elif self._client_factory is not None:
                self._client = self._client_factory(self.spec)
            else:
                self._client = ChatClient(self.spec, state_path=self.home / "state" / "quirks.json",
                                          network=self.config.data.get("network"))
        return self._client

    async def ensure_client(self):
        """在线程里构造客户端（解析系统代理可能查 DNS、建 TLS 上下文较慢），不卡事件循环。"""
        if self._client is None:
            await asyncio.to_thread(self.client)
        return self._client

    def set_model(self, name: str) -> str:
        spec = self.config.resolve_model(name)
        same_prefix = (spec.profile.prompt_patch == self.spec.profile.prompt_patch and
                       spec.profile.edit_style == self.spec.profile.edit_style)
        self.spec = spec
        self.tools = build_tools(spec.profile, self.config)
        self.tool_map = {t.name: t for t in self.tools}
        self._install_mcp()
        self._client = None
        if not same_prefix:
            self.prefix = None
        self.updates.reset()
        return (f"已切换到 {spec.alias}（{spec.model}，家族 {spec.profile.family}）。"
                "换模型后服务端缓存会从零开始。")

    def set_mode(self, mode: str) -> None:
        self.policy.set_mode(mode)

    def set_shell(self, name: str) -> str:
        sh = resolve_shell(name)   # 找不到会抛 ShellNotFound
        self.shell, self.shell_name = sh, name
        return f"shell 已切换为 {sh.display}（{sh.exe}）。下一条消息会以增量环境信息告诉模型。"

    def queue_input(self, text: str) -> None:
        self.pending.append(text)

    def add_note(self, text: str) -> None:
        self.notes.append(text)

    def interrupt(self) -> bool:
        if self._task and not self._task.done():
            self._task.cancel()
            return True
        return False

    def new(self) -> None:
        """/new：清空对话（system+tools 不变，所以前缀缓存仍然能命中）。"""
        self.history = []
        self.state = ToolState()
        self.updates.reset()
        self.stats = UsageStats()
        self.pending.clear()
        self.notes.clear()
        self.turns = 0
        self.turn_starts = []
        self.turn_trees = []
        self.instruction_sources = []
        self.memory_sources = []
        self._fresh_usage = False
        self.store = SessionStore(self.home, enabled=self.store.enabled)

    def rewind_last_turn(self) -> str | None:
        """回退（backtrack）：删掉最后一个回合，返回那条用户消息原文供重新编辑。文件改动不回滚（用 /undo）。"""
        if self.running or not self.turn_starts:
            return None
        i = self.turn_starts.pop()
        content = self.history[i].get("content") or ""
        text = content.split("\n\n<context-update")[0]
        self.history = self.history[:i]
        self.updates.reset()          # 被删的消息可能带着基线附录，下一条补发全量
        self.store.append("rewind", {"to": i})
        return text

    def user_text_at(self, i: int) -> str | None:
        """history[i] 是用户回合开头时返回其原文（去掉环境附录），否则 None。"""
        if not (0 <= i < len(self.history)):
            return None
        m = self.history[i]
        if m.get("role") != "user" or m.get("_kind"):
            return None
        return (m.get("content") or "").split("\n\n<context-update")[0]

    def truncate_to(self, i: int) -> None:
        """编辑重发（D3-E）：把历史截到第 i 条之前（第 i 条是某回合的用户消息）。文件改动另用 revert_files_from。"""
        if self.running:
            raise RuntimeError("运行中不能截断历史")
        self.history = self.history[:i]
        self.turn_starts = [x for x in self.turn_starts if x < i]
        self.updates.reset()
        self.pending.clear()
        self.notes.clear()
        self.guard = LoopGuard.from_config(self.config.data.get("loop_guard"))
        self.store.append("rewind", {"to": i})

    def revert_files_from(self, layer: int) -> list[str] | None:
        """把第 layer 层及之后所有回合写过的文件恢复到那之前（倒序逐层恢复）。没有对应快照（重启过）返回 None。"""
        layers = self.state.snapshots
        if layer < 0 or layer >= len(layers):
            return None
        restored: list[str] = []
        for snap in reversed(layers[layer:]):
            for path, data in snap.items():
                p = Path(path)
                try:
                    if data is None:
                        if p.exists():
                            p.unlink()
                    else:
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_bytes(data)
                    if path not in restored:
                        restored.append(path)
                    self.state.files.pop(path, None)
                except OSError:
                    continue
        del layers[layer:]
        layers.append({})
        return restored

    # ------------------------------------------------------------ 磁盘快照（D4-1）
    def disk(self):
        """可用的磁盘快照（影子 git 仓库），不可用返回 None。"""
        if not self.disk_enabled:
            return None
        if self._disk_obj is None:
            from ee.core import snapshot
            self._disk_obj = snapshot.for_dir(self.cwd, self.home)
            if not self._disk_obj.available:
                log.info("磁盘快照不可用（%s），改用内存快照", self._disk_obj.disabled)
        return self._disk_obj if self._disk_obj.available else None

    def disk_capture(self) -> str | None:
        d = self.disk()
        return d.capture() if d else None

    def _after_restore(self, restored: list[str], note: str) -> None:
        for path in restored:
            self.state.files.pop(path, None)
        self.state.snapshots = [{}]          # 内存快照已过时，避免二次恢复
        if restored:
            self.add_note(note + "\n" + "\n".join(restored))

    def revert_trees(self, entries: list[tuple[str, str | None]]) -> list[str] | None:
        """编辑重发：entries = 被撤回的各回合 (回合前 tree, 回合后 tree)，第一项是被编辑的那条消息。
        把这些回合改过的文件恢复到第一项的回合前版本。磁盘快照不可用返回 None。"""
        d = self.disk()
        if d is None or not entries or not entries[0][0]:
            return None
        base = entries[0][0]
        now = None
        paths: list[str] = []
        try:
            for before, after in entries:
                if not before:
                    continue
                if not after:
                    now = now or d.capture()
                    after = now
                paths += d.changed(before, after) if after else []
            restored = d.restore(base, paths)
        except Exception as e:  # noqa: BLE001
            log.warning("磁盘快照恢复失败：%s", e)
            return None
        for i, e in enumerate(self.turn_trees):
            if e.get("before") == base:
                del self.turn_trees[i:]
                break
        self._after_restore(restored, "[系统提示] 用户编辑并重发了之前的消息，以下文件已恢复到那条消息之前的内容（如需再改请重新 read）：")
        return restored

    def _disk_undo(self) -> list[str] | None:
        d = self.disk()
        if d is None or not any(e.get("before") for e in self.turn_trees):
            return None
        now = None
        for e in reversed(self.turn_trees):
            if e.get("undone") or not e.get("before"):
                continue
            after = e.get("after")
            if not after:
                now = now or d.capture()
                after = now
            try:
                paths = d.changed(e["before"], after) if after else []
                if not paths:
                    continue
                restored = d.restore(e["before"], paths)
            except Exception as ex:  # noqa: BLE001
                log.warning("磁盘快照撤销失败：%s", ex)
                return None
            e["undone"] = True
            self.last_undo_tree = e["before"]
            self._after_restore(restored, "[系统提示] 用户执行了 /undo，以下文件已恢复到你上一回合修改之前的内容（如需再改请重新 read）：")
            return restored
        return []

    def undo(self) -> list[str]:
        """/undo：把最近一个有改动、还没撤销过的回合的文件改动恢复成改动前。
        有磁盘快照时按回合前后 tree 的差异恢复（含 shell 改的文件，重启后也行）；否则用内存快照。"""
        self.last_undo_tree = None
        res = self._disk_undo()
        if res is not None:
            return res
        layers = self.state.snapshots
        while layers and not layers[-1]:
            layers.pop()
        if not layers:
            self.state.snapshots = [{}]
            return []
        layer = layers.pop()
        if not layers:
            layers.append({})
        restored = []
        for path, data in layer.items():
            p = Path(path)
            try:
                if data is None:
                    if p.exists():
                        p.unlink()
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(data)
                restored.append(path)
                self.state.files.pop(path, None)   # 需要重新读
            except OSError:
                continue
        if restored:
            self.add_note("[系统提示] 用户执行了 /undo，以下文件已恢复到你上一回合修改之前的内容（如需再改请重新 read）：\n"
                          + "\n".join(restored))
        return restored

    # ------------------------------------------------------------ MCP
    def _install_mcp(self) -> None:
        self.tools = [t for t in self.tools if not t.name.startswith("mcp__")] + list(self.mcp_tools)
        self.tool_map = {t.name: t for t in self.tools}

    async def _attach_mcp(self, emit) -> None:
        """首次冻结前缀前连接 MCP 服务器（只做一次）；失败只出提示，不影响回合。"""
        if self._mcp_done or self.prefix is not None or not self.mcp_enabled:
            return
        self._mcp_done = True
        from ee.mcp.config import load_servers, mcp_settings
        data = self.config.data
        m = mcp_settings(data)
        if not m.get("enabled", True):
            return
        try:
            servers, errors = await asyncio.to_thread(load_servers, data, self.cwd)
        except Exception as e:  # noqa: BLE001
            emit(Notice(f"读取 MCP 配置失败：{e}", "warn"))
            return
        for err in errors:
            emit(Notice(f"MCP 配置：{err}", "warn"))
        if not servers:
            return
        from ee.mcp.manager import get_manager
        from ee.mcp.tool import McpTool
        if any(sc.enabled for sc in servers):
            emit(Status("正在连接 MCP 服务器…"))
        mgr = get_manager()
        try:
            states = await mgr.connect_all(data, self.cwd)
        finally:
            emit(Status(""))
        tools, seen = [], set(self.tool_map)
        timeout = float(m.get("tool_timeout") or 120)
        for st in states:
            if st.status == "ready":
                for spec in st.tools:
                    t = McpTool(mgr, st.cfg.name, spec, trusted=st.cfg.trusted, timeout=timeout)
                    if t.name in seen:
                        continue
                    seen.add(t.name)
                    tools.append(t)
            elif st.status == "error":
                first = (st.error or "").splitlines()[0][:300] if st.error else "未知错误"
                emit(Notice(f"MCP 服务器 {st.cfg.name} 连接失败：{first}", "warn"))
            elif st.status == "needs_approval":
                emit(Notice(f"项目 .mcp.json 里的 MCP 服务器 {st.cfg.name} 还没被允许，在 设置 → MCP 里允许后，新对话生效。",
                            "info"))
        self.mcp_states = [st.summary() for st in states]
        self.mcp_tools = tools
        self._install_mcp()

    def refresh_git(self) -> None:
        self.git = gitinfo.status(self.cwd)

    def context_left(self) -> float:
        return 1.0 - self.stats.context_used(self.profile.context_window)

    # ------------------------------------------------------------ 前缀
    def current_prefix(self) -> FrozenPrefix:
        return FrozenPrefix.freeze(system_prompt(self.profile), [t.schema() for t in self.tools])

    def _ensure_prefix(self, emit) -> FrozenPrefix:
        if self.prefix is None:
            self.prefix = self.current_prefix()
            self.store.append("prefix", {"sha256": self.prefix.sha256, "model": self.spec.model})
            return self.prefix
        if not self.prefix.verify():
            emit(Notice("冻结前缀的内容被改动了（sha256 不符），缓存会失效。", "warn"))
        fresh = self.current_prefix().sha256
        if fresh != self.prefix.sha256:
            emit(Notice("system/tools 与会话开始时不同（可能改了提示词文件）；继续使用会话开始时冻结的版本以保住缓存。", "warn"))
        return self.prefix

    # ------------------------------------------------------------ 历史（只追加）
    def _append(self, msg: dict) -> None:
        self.history.append(msg)
        self.store.message(msg)

    def _add_user(self, text: str, images: list[str] | None = None) -> None:
        """调用前先 `await asyncio.to_thread(self.refresh_git)`（git 子进程不能在事件循环上跑）。
        images：附件图片的绝对路径（D3-D），记在消息的 _images 上，发送时由 wire_messages 处理。"""
        parts = env_parts(self.cwd, self.shell, self.git, self.gh, MODE_LABEL.get(self.policy.mode, self.policy.mode))
        appendix = self.updates.build(parts)
        body = text
        if self.notes:
            body = "\n\n".join(self.notes) + "\n\n" + text
            self.notes = []
        msg = {"role": "user", "content": body + "\n\n" + appendix}
        if images:
            msg["_images"] = list(images)
        self._append(msg)

    async def _ensure_instructions_async(self) -> None:
        if self.history:
            return
        cfg = self.config.data.get("agents_md", {}) or {}
        if cfg.get("enabled", True):
            ins = await asyncio.to_thread(load_instructions, self.cwd, self.home, int(cfg.get("max_bytes", 32768)))
            msg = ins.message()
            if msg is not None and not self.history:
                self._append(msg)
                self.instruction_sources = [str(p) for p in ins.sources]
        if any(m.get("_sub") == "memory" for m in self.history) or any(m.get("_kind") != "agents" for m in self.history):
            return
        # 长期记忆：紧跟 AGENTS.md 的固定 user 消息，会话内不再改动（保住前缀缓存）
        mem = await asyncio.to_thread(load_memory, self.home, self.cwd, self.config.data.get("memory") or {})
        mmsg = mem.message()
        if mmsg is not None:
            self._append(mmsg)
            self.memory_sources = list(mem.sources)

    def _ensure_instructions(self) -> None:
        """会话开始（历史为空）时加载 AGENTS.md，作为固定 user 消息放在 system 之后；之后不再改动。"""
        if self.history:
            return
        cfg = self.config.data.get("agents_md", {}) or {}
        if cfg.get("enabled", True):
            ins = load_instructions(self.cwd, self.home, int(cfg.get("max_bytes", 32768)))
            msg = ins.message()
            if msg is not None:
                self._append(msg)
                self.instruction_sources = [str(p) for p in ins.sources]
        # 长期记忆：紧跟 AGENTS.md 的固定 user 消息，会话内不再改动（保住前缀缓存）
        mem = load_memory(self.home, self.cwd, self.config.data.get("memory") or {})
        mmsg = mem.message()
        if mmsg is not None:
            self._append(mmsg)
            self.memory_sources = list(mem.sources)

    def _fill_missing(self, why: str) -> None:
        for cid in list(self._open_calls):
            self._append({"role": "tool", "tool_call_id": cid, "content": self._results.get(cid, why)})
        self._open_calls.clear()
        self._results.clear()

    # ------------------------------------------------------------ 回合
    async def run_turn(self, text: str, images: list[str] | None = None) -> AsyncIterator:
        q: asyncio.Queue = asyncio.Queue()
        self.running = True
        self._task = asyncio.create_task(self._turn(text, q.put_nowait, images))
        try:
            while True:
                ev = await q.get()
                if isinstance(ev, TurnDone) and self.turn_trees and self.turn_trees[-1].get("after") is None:
                    try:
                        ev.tree_after = await asyncio.to_thread(self.disk_capture)
                    except Exception:  # noqa: BLE001
                        ev.tree_after = None
                    self.turn_trees[-1]["after"] = ev.tree_after
                yield ev
                if isinstance(ev, TurnDone):
                    break
        finally:
            self.running = False
            if self._task and not self._task.done():
                self._task.cancel()
                try:
                    await self._task
                except BaseException:
                    pass

    async def _turn(self, text: str, emit, images: list[str] | None = None) -> None:
        final = ""
        try:
            self.turns += 1
            self.state.new_turn()
            self.guard = LoopGuard.from_config(self.config.data.get("loop_guard"))
            log.info("回合开始：会话 %s 第 %d 回合 模型 %s cwd=%s", self.store.id, self.turns, self.spec.alias, self.cwd)
            await self._ensure_instructions_async()
            await self._attach_mcp(emit)
            await self._maybe_auto_compact(emit)
            if await hooks.on_turn_start(self, text, emit):   # D5-4 Hooks：SessionStart / UserPromptSubmit 拦截
                emit(TurnDone("blocked", ""))
                return
            self.turn_starts.append(len(self.history))
            await asyncio.to_thread(self.refresh_git)
            self._add_user(text, images)
            if images and not self.profile.vision:
                emit(Notice(f"当前模型 {self.spec.alias} 不支持看图，这 {len(images)} 张图片没有发给模型（换支持图像的模型，例如 deepseek-flash）。", "warn"))
            tree = await asyncio.to_thread(self.disk_capture)
            if tree:
                self.turn_trees.append({"before": tree, "after": None, "undone": False})
            emit(TurnStart(self.turns, tree))
            step = 0
            max_steps = max(1, self.config.max_steps)
            while True:
                step += 1
                last = step >= max_steps
                if step > 1:
                    await self._maybe_auto_compact(emit)
                if last and step > 1:
                    self._append({"role": "user", "content": LAST_STEP_NOTE, "_kind": "note"})
                emit(StepStart(step))
                comp = await self._sample(emit)
                if comp.content:
                    final = comp.content
                if not comp.tool_calls:
                    if self.pending and not last:
                        await asyncio.to_thread(self.refresh_git)
                        self._add_user("\n\n".join(self.pending)); self.pending.clear()
                        continue
                    if not last and await hooks.on_stop(self, final, emit):   # D5-4 Hooks：Stop 要求继续
                        continue
                    emit(TurnDone("max_steps" if (last and step > 1) else "completed", final))
                    return
                if last:
                    for tc in comp.tool_calls:
                        self._append({"role": "tool", "tool_call_id": tc["id"],
                                      "content": "已达到最大步数，此调用未执行。"})
                    self._open_calls.clear()
                    emit(Notice(f"已达到最大步数 {max_steps}，本回合结束。", "warn"))
                    emit(TurnDone("max_steps", final))
                    return
                self._end_turn = False
                note = await self._run_tools(comp.tool_calls, emit)
                if self._end_turn and not self.pending:   # present_plan：方案已提交，等用户批准
                    self._end_turn = False
                    emit(TurnDone("completed", final))
                    return
                if self.guard.halt:
                    emit(Notice(self.guard.halt, "warn"))
                    emit(TurnDone("loop", final))
                    return
                if note:
                    self._append({"role": "user", "content": note, "_kind": "note"})
                if self.pending:
                    await asyncio.to_thread(self.refresh_git)
                    self._add_user("\n\n".join(self.pending)); self.pending.clear()
        except asyncio.CancelledError:
            log.info("回合被中断：会话 %s", self.store.id)
            self._fill_missing(INTERRUPTED)
            emit(TurnDone("interrupted", final))
        except Exception as e:  # noqa: BLE001
            log.error("回合出错：会话 %s：%s", self.store.id, e, exc_info=True)
            self._fill_missing("执行出错，未完成。")
            emit(Error(_friendly_error(e)))
            emit(TurnDone("error", final))

    async def _sample(self, emit) -> Completion:
        prefix = self._ensure_prefix(emit)
        messages = [prefix.system] + T.wire_messages(self.history, self.profile)
        comp: Completion | None = None
        text, reasoning = [], []
        emit(Status(f"正在准备请求 {self.spec.alias}…"))
        try:
            await self.ensure_client()
            async for kind, val in self.client().stream(messages, prefix.tools):
                if kind == "text":
                    text.append(val); emit(TextDelta(val))
                elif kind == "reasoning":
                    reasoning.append(val); emit(ReasoningDelta(val))
                elif kind == "notice":
                    emit(Notice(val, "warn"))
                elif kind == "status":
                    emit(Status(val))
                elif kind == "done":
                    comp = val
        except asyncio.CancelledError:
            if text or reasoning:
                msg = {"role": "assistant", "content": "".join(text)}
                if reasoning:
                    msg["reasoning"] = "".join(reasoning)
                self._append(msg)
            raise
        if comp is None:
            comp = Completion(content="".join(text), reasoning="".join(reasoning))
        msg: dict = {"role": "assistant", "content": comp.content or ""}
        if comp.tool_calls:
            msg["tool_calls"] = comp.tool_calls
        if comp.reasoning:
            msg["reasoning"] = comp.reasoning
        self._append(msg)
        self._open_calls = [tc["id"] for tc in comp.tool_calls]
        self._results = {}
        self.last_reasoning = comp.reasoning or self.last_reasoning
        if comp.usage:
            self._fresh_usage = True
        cost = self.stats.add(comp.usage, self.profile.price)
        u = comp.usage or {}
        emit(Usage(u.get("prompt", 0), u.get("completion", 0), u.get("hit"), cost, self.stats.totals()))
        emit(AssistantMessage(comp.content, comp.reasoning))
        if comp.recovered:
            emit(Notice(f"模型把 {len(comp.tool_calls)} 个工具调用写进了正文，已自动解析执行。"))
        return comp

    async def _run_tools(self, calls: list[dict], emit) -> str | None:
        """执行一步的工具调用。返回防死循环要追加给模型的提醒（没有则 None）；需要停回合时设置 guard.halt。"""
        prepared = []
        sigs: dict[str, str] = {}
        stopped = False
        for tc in calls:
            name = tc["function"]["name"]
            tool = self.tool_map.get(name)
            if tool is None:
                entry = (tc, None, {}, f"错误：未知工具 {name}。可用工具：{', '.join(self.tool_map)}")
                sig = signature(name, tc["function"].get("arguments"))
            else:
                args, err = T.parse_args(tc["function"].get("arguments"))
                if err:
                    entry = (tc, tool, {}, f"错误：{err}。请用合法的 JSON 重新调用 {name}。")
                    sig = signature(name, tc["function"].get("arguments"))
                else:
                    args = T.repair_args(args, tool.parameters)
                    entry = (tc, tool, args, None)
                    sig = signature(name, args)
            sigs[tc["id"]] = sig
            if stopped:
                entry = (entry[0], entry[1], entry[2], REPEAT_STOP)
            else:
                verdict, n = self.guard.check(sig)
                if verdict == "stop":
                    stopped = True
                    entry = (entry[0], entry[1], entry[2], REPEAT_STOP)
                elif verdict == "block":
                    entry = (entry[0], entry[1], entry[2], REPEAT_NOTE.format(n=n, name=name))
            prepared.append(entry)
        i = 0
        while i < len(prepared):
            tc, tool, args, err = prepared[i]
            if tool is not None and tool.read_only and err is None:
                j = i
                while j < len(prepared) and prepared[j][1] is not None and prepared[j][1].read_only \
                        and prepared[j][3] is None:
                    j += 1
                await asyncio.gather(*(self._exec_one(*p, emit) for p in prepared[i:j]))
                i = j
            else:
                await self._exec_one(tc, tool, args, err, emit)
                i += 1
        for tc in calls:
            cid = tc["id"]
            self._append({"role": "tool", "tool_call_id": cid, "content": self._results.get(cid, INTERRUPTED)})
        outcomes = [(sigs[tc["id"]], self._ok.get(tc["id"], False)) for tc in calls]
        self._open_calls.clear()
        self._results.clear()
        self._ok = {}
        if self.guard.halt:
            return None
        return self.guard.end_step(outcomes)

    async def _exec_one(self, tc: dict, tool, args: dict, err: str | None, emit) -> None:
        cid = tc["id"]
        name = tc["function"]["name"]
        summary = tool.summary(args) if tool else name
        emit(ToolStart(cid, name, args, summary))
        if err is not None:
            res = ToolResult(err, ok=False, summary=summary)
        else:
            res = await self._guarded_run(cid, tool, args, emit)
            res = await hooks.post_tool_use(self, cid, tool, args, res, emit)   # D5-4 Hooks
        out, _ = truncate_output(res.output, self.home / "tool-output", name)
        self._results[cid] = out
        self._ok[cid] = res.ok
        emit(ToolEnd(cid, name, res.ok, res.output, res.summary or summary, res.diff, res.meta))
        if res.meta.get("kind") == "plan":
            emit(PlanUpdate(res.meta.get("items", [])))
        if res.ok and res.meta.get("end_turn"):
            self._end_turn = True

    async def _guarded_run(self, cid: str, tool, args: dict, emit) -> ToolResult:
        pre = await hooks.pre_tool_use(self, cid, tool, args, emit)   # D5-4 Hooks：可拦截 / 改参数 / 免审批
        if pre.result is not None:
            return pre.result
        args = pre.args
        d: Decision = self.policy.check(tool.name, args, info={"read_only": bool(getattr(tool, "read_only", False)),
                                                              "trusted": bool(getattr(tool, "trusted", False))})
        d = hooks.adjust_decision(d, pre)
        if d.action == "deny":
            return ToolResult(f"被权限规则拒绝，未执行：{d.reason}", ok=False, meta={"denied": True})
        if d.action == "ask":
            dec = await self._ask(cid, tool, args, d, emit)
            if dec.choice == "always":
                self.policy.remember(d, tool.name)
            elif dec.choice in ("deny", "deny_with_reason"):
                msg = "用户拒绝了这次操作，没有执行。"
                if dec.reason:
                    msg += f"用户的意见：{dec.reason}"
                else:
                    msg += "请换一种做法，或先问用户。"
                return ToolResult(msg, ok=False, meta={"denied": True, "by_user": True})
        ctx = ToolContext(cwd=self.cwd, state=self.state, output_dir=self.home / "tool-output", shell=self.shell,
                          emit=lambda s: emit(ToolOutput(cid, s)),
                          timeout_default=int(self.config.data.get("bash_timeout", 120)),
                          extra={"session": self, "emit_event": emit, "call_id": cid, "config": self.config,
                                 "home": self.home})
        try:
            return await tool.run(args, ctx)
        except ToolError as e:
            return ToolResult(f"错误：{e}", ok=False)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            return ToolResult(f"工具内部错误：{type(e).__name__}: {e}", ok=False)

    async def _ask(self, cid: str, tool, args: dict, d: Decision, emit) -> ApprovalDecision:
        async with self._approval_lock:
            if tool.name == "shell":
                title = f"{'后台运行' if args.get('background') else '运行'}命令：{args.get('command', '')}"
                detail = args.get("command", "")
            elif tool.name in ("write", "edit"):
                title = f"{'写入' if tool.name == 'write' else '编辑'}文件：{args.get('path', '')}"
                detail = d.reason
            elif tool.name == "apply_patch":
                title, detail = "应用补丁", args.get("patch", "")
            else:
                title, detail = f"使用工具 {tool.name}", json.dumps(args, ensure_ascii=False)[:2000]
            hooks.notify(self, "permission_prompt", title, emit)   # D5-4 Hooks：Notification（不等待）
            fut = asyncio.get_running_loop().create_future()
            emit(ApprovalRequest(cid, tool.name, title, detail, d.prefix, fut, ))
            return await fut

    # ------------------------------------------------------------ 侧路请求（不碰主会话）
    async def side_complete(self, system: str, user: str) -> str:
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        out = []
        comp = None
        await self.ensure_client()
        async for kind, val in self.client().stream(msgs, None):
            if kind == "text":
                out.append(val)
            elif kind == "done":
                comp = val
        if comp is not None:
            self.stats.add(comp.usage, self.profile.price)
            return comp.content.strip()
        return "".join(out).strip()

    async def commit_message(self) -> tuple[str, bool]:
        """/commit：根据 diff 让模型写提交信息。返回 (信息, 是否需要 git add -A)。"""
        staged = await asyncio.to_thread(gitinfo.staged_diff, self.cwd)
        stage_all = not staged.strip()
        diff = await asyncio.to_thread(gitinfo.diff, self.cwd) if stage_all else staged
        if not diff.strip():
            raise ToolError("没有可提交的改动")
        log = await asyncio.to_thread(gitinfo.recent_log, self.cwd)
        prompt = (f"最近的提交记录：\n{log or '（无）'}\n\n改动：\n{diff[:60000]}")
        return await self.side_complete(load_prompt("commit.md"), prompt), stage_all

    # ------------------------------------------------------------ 压缩
    def context_tokens(self) -> int:
        """当前上下文大小（token）：优先用服务端返回的上次用量，没有就粗估。"""
        used = self.stats.last_prompt + self.stats.last_completion
        if used > 0:
            return used
        prefix = self.prefix or self.current_prefix()
        return (C.estimate_tokens(prefix.system["content"]) + C.estimate_tokens(json.dumps(prefix.tools)) +
                C.estimate_messages(self.history))

    def needs_compaction(self) -> bool:
        cfg = self.config.data.get("compact", {}) or {}
        if not cfg.get("auto", True) or not self._fresh_usage:
            return False
        window = self.profile.context_window
        if window <= 0:
            return False
        return self.context_tokens() >= float(cfg.get("threshold", 0.8)) * window

    async def _maybe_auto_compact(self, emit) -> None:
        if self.needs_compaction() and any(m.get("_kind") != "agents" for m in self.history):
            await self._compact(emit, "auto")

    async def compact(self, emit=None) -> dict:
        """手动压缩（/compact，供 server 调用）。不能在回合运行中调用。

        返回 {ok, message, tokens_before, tokens_after}；成功时 emit 一个 Compacted 事件。
        """
        emit = emit or (lambda ev: None)
        if self.running:
            return {"ok": False, "message": "正在运行中，等这一回合结束再压缩。", "tokens_before": 0, "tokens_after": 0}
        self.running = True
        try:
            return await self._compact(emit, "manual")
        finally:
            self.running = False

    async def _summarize(self, emit) -> tuple[str, dict | None]:
        """主路：同一前缀 + 完整历史 + 压缩指令（复用缓存）。不调工具。"""
        prefix = self._ensure_prefix(emit)
        messages = ([prefix.system] + T.wire_messages(self.history, self.profile) +
                    [{"role": "user", "content": C.compact_prompt()}])
        text: list[str] = []
        comp = None
        await self.ensure_client()
        async for kind, val in self.client().stream(messages, prefix.tools):
            if kind == "text":
                text.append(val)
            elif kind == "notice":
                emit(Notice(val, "warn"))
            elif kind == "done":
                comp = val
        content = (comp.content if comp is not None else "".join(text)).strip()
        return content, (comp.usage if comp is not None else None)

    async def _summarize_side(self) -> str:
        """兜底：历史渲染成文字（超长掐头留尾）走侧路请求。"""
        budget = max(4000, int(self.profile.context_window * 0.5))
        transcript = C.render_transcript(self.history, budget)
        return await self.side_complete(C.compact_prompt(), "以下是需要压缩的对话记录：\n\n" + transcript)

    async def _compact(self, emit, reason: str) -> dict:
        real = [m for m in self.history if m.get("_kind") != "agents"]
        before = self.context_tokens()
        if not real:
            return {"ok": False, "message": "没有可压缩的内容。", "tokens_before": before, "tokens_after": before}
        summary, usage = "", None
        try:
            summary, usage = await self._summarize(emit)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            emit(Notice(f"压缩请求失败（{_friendly_error(e)}），改用精简记录重试。", "warn"))
        if usage is not None:
            self.stats.add(usage, self.profile.price)
        if not summary:
            try:
                summary = await self._summarize_side()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                msg = f"压缩失败：{_friendly_error(e)}。上下文没有改动。"
                emit(Notice(msg, "warn"))
                self._fresh_usage = False   # 不要每一步都重试
                return {"ok": False, "message": msg, "tokens_before": before, "tokens_after": before}
        if not summary:
            msg = "压缩失败：模型没有返回摘要。上下文没有改动。"
            emit(Notice(msg, "warn"))
            self._fresh_usage = False
            return {"ok": False, "message": msg, "tokens_before": before, "tokens_after": before}
        cfg = self.config.data.get("compact", {}) or {}
        await asyncio.to_thread(self.refresh_git)
        self.updates.reset()     # 摘要消息里补发全量环境信息
        parts = env_parts(self.cwd, self.shell, self.git, self.gh, MODE_LABEL.get(self.policy.mode, self.policy.mode))
        appendix = self.updates.build(parts)
        new = C.build_history(self.history, summary, appendix, int(cfg.get("keep_user_messages", 3)),
                              int(cfg.get("keep_user_tokens", 20000)))
        self.history = new
        self.store.append("reset", {"why": "compact"})
        for m in new:
            self.store.message(m)
        self.store.append("compact", {"reason": reason, "tokens_before": before})
        self.turn_starts = []          # 压缩前的回合无法再回退
        for rec in self.state.files.values():
            rec.reads.clear()          # 读过的内容已不在上下文里：下次 read 返回全文
        prefix = self.prefix or self.current_prefix()
        after = (C.estimate_tokens(prefix.system["content"]) + C.estimate_tokens(json.dumps(prefix.tools)) +
                 C.estimate_messages(new))
        self.stats.last_prompt, self.stats.last_completion = after, 0
        self._fresh_usage = False
        self.compactions += 1
        text = f"已压缩上下文（约 {before:,} → {after:,} tokens）"
        emit(Compacted(text, "info", reason=reason, tokens_before=before, tokens_after=after, summary=summary))
        return {"ok": True, "message": text, "tokens_before": before, "tokens_after": after}


def _friendly_error(e: Exception) -> str:
    from ee.core.secrets import redact
    return redact(_friendly_error_raw(e))


def _friendly_error_raw(e: Exception) -> str:
    from ee.core.providers.openai_chat import ModelError
    if isinstance(e, ModelError):
        return str(e)
    status = getattr(e, "status_code", None)
    name = type(e).__name__
    if status == 401:
        return "API key 无效或未授权（401）。请到 设置 → 模型与供应商 检查 key。"
    if status == 404:
        return f"模型或接口不存在（404）：{e}"
    if status:
        return f"请求失败（{status}）：{str(e)[:500]}"
    return f"{name}: {str(e)[:500]}"
