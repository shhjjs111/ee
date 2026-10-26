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
from pathlib import Path
from typing import AsyncIterator, Callable

from ee.config import Config, ee_home
from ee.core import gitinfo
from ee.core.context import ContextUpdates, FrozenPrefix, UsageStats, env_parts, load_prompt, system_prompt
from ee.core.events import (ApprovalDecision, ApprovalRequest, AssistantMessage, Error, Notice, PlanUpdate,
                            ReasoningDelta, StepStart, TextDelta, ToolEnd, ToolOutput, ToolStart, TurnDone,
                            TurnStart, Usage)
from ee.core.permissions import MODE_LABEL, Decision, PermissionPolicy
from ee.core.providers import transforms as T
from ee.core.providers.openai_chat import ChatClient, Completion
from ee.core.store import SessionStore
from ee.tools import ToolContext, ToolError, ToolResult, ToolState, build_tools
from ee.tools.shellenv import ShellNotFound, resolve_shell
from ee.tools.truncate import truncate_output

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
        self.tools = build_tools(self.spec.profile)
        self.tool_map = {t.name: t for t in self.tools}
        self.prefix: FrozenPrefix | None = None
        self.history: list[dict] = []
        self.updates = ContextUpdates()
        self.stats = UsageStats()
        self.state = ToolState()
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
        self.store = SessionStore(self.home, enabled=persist)
        self._task: asyncio.Task | None = None
        self._open_calls: list[str] = []
        self._results: dict[str, str] = {}
        self._approval_lock = asyncio.Lock()
        self.last_reasoning = ""
        self.running = False

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
                self._client = ChatClient(self.spec, state_path=self.home / "state" / "quirks.json")
        return self._client

    def set_model(self, name: str) -> str:
        spec = self.config.resolve_model(name)
        same_prefix = (spec.profile.prompt_patch == self.spec.profile.prompt_patch and
                       spec.profile.edit_style == self.spec.profile.edit_style)
        self.spec = spec
        self.tools = build_tools(spec.profile)
        self.tool_map = {t.name: t for t in self.tools}
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

    def undo(self) -> list[str]:
        """/undo：把最近一个有改动的回合里 ee 写过的文件恢复成改动前。"""
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

    def _add_user(self, text: str) -> None:
        self.refresh_git()
        parts = env_parts(self.cwd, self.shell, self.git, self.gh, MODE_LABEL.get(self.policy.mode, self.policy.mode))
        appendix = self.updates.build(parts)
        body = text
        if self.notes:
            body = "\n\n".join(self.notes) + "\n\n" + text
            self.notes = []
        self._append({"role": "user", "content": body + "\n\n" + appendix})

    def _fill_missing(self, why: str) -> None:
        for cid in list(self._open_calls):
            self._append({"role": "tool", "tool_call_id": cid, "content": self._results.get(cid, why)})
        self._open_calls.clear()
        self._results.clear()

    # ------------------------------------------------------------ 回合
    async def run_turn(self, text: str) -> AsyncIterator:
        q: asyncio.Queue = asyncio.Queue()
        self.running = True
        self._task = asyncio.create_task(self._turn(text, q.put_nowait))
        try:
            while True:
                ev = await q.get()
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

    async def _turn(self, text: str, emit) -> None:
        final = ""
        try:
            self.turns += 1
            self.state.new_turn()
            self.turn_starts.append(len(self.history))
            self._add_user(text)
            emit(TurnStart(self.turns))
            step = 0
            max_steps = max(1, self.config.max_steps)
            while True:
                step += 1
                last = step >= max_steps
                if last and step > 1:
                    self._append({"role": "user", "content": LAST_STEP_NOTE})
                emit(StepStart(step))
                comp = await self._sample(emit)
                if comp.content:
                    final = comp.content
                if not comp.tool_calls:
                    if self.pending and not last:
                        self._add_user("\n\n".join(self.pending)); self.pending.clear()
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
                await self._run_tools(comp.tool_calls, emit)
                if self.pending:
                    self._add_user("\n\n".join(self.pending)); self.pending.clear()
        except asyncio.CancelledError:
            self._fill_missing(INTERRUPTED)
            emit(TurnDone("interrupted", final))
        except Exception as e:  # noqa: BLE001
            self._fill_missing("执行出错，未完成。")
            emit(Error(_friendly_error(e)))
            emit(TurnDone("error", final))

    async def _sample(self, emit) -> Completion:
        prefix = self._ensure_prefix(emit)
        messages = [prefix.system] + T.wire_messages(self.history, self.profile)
        comp: Completion | None = None
        text, reasoning = [], []
        try:
            async for kind, val in self.client().stream(messages, prefix.tools):
                if kind == "text":
                    text.append(val); emit(TextDelta(val))
                elif kind == "reasoning":
                    reasoning.append(val); emit(ReasoningDelta(val))
                elif kind == "notice":
                    emit(Notice(val, "warn"))
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
        cost = self.stats.add(comp.usage, self.profile.price)
        u = comp.usage or {}
        emit(Usage(u.get("prompt", 0), u.get("completion", 0), u.get("hit"), cost, self.stats.totals()))
        emit(AssistantMessage(comp.content, comp.reasoning))
        if comp.recovered:
            emit(Notice(f"模型把 {len(comp.tool_calls)} 个工具调用写进了正文，已自动解析执行。"))
        return comp

    async def _run_tools(self, calls: list[dict], emit) -> None:
        prepared = []
        for tc in calls:
            name = tc["function"]["name"]
            tool = self.tool_map.get(name)
            if tool is None:
                prepared.append((tc, None, {}, f"错误：未知工具 {name}。可用工具：{', '.join(self.tool_map)}"))
                continue
            args, err = T.parse_args(tc["function"].get("arguments"))
            if err:
                prepared.append((tc, tool, {}, f"错误：{err}。请用合法的 JSON 重新调用 {name}。"))
                continue
            prepared.append((tc, tool, T.repair_args(args, tool.parameters), None))
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
        self._open_calls.clear()
        self._results.clear()

    async def _exec_one(self, tc: dict, tool, args: dict, err: str | None, emit) -> None:
        cid = tc["id"]
        name = tc["function"]["name"]
        summary = tool.summary(args) if tool else name
        emit(ToolStart(cid, name, args, summary))
        if err is not None:
            res = ToolResult(err, ok=False, summary=summary)
        else:
            res = await self._guarded_run(cid, tool, args, emit)
        out, _ = truncate_output(res.output, self.home / "tool-output", name)
        self._results[cid] = out
        emit(ToolEnd(cid, name, res.ok, res.output, res.summary or summary, res.diff, res.meta))
        if res.meta.get("kind") == "plan":
            emit(PlanUpdate(res.meta.get("items", [])))

    async def _guarded_run(self, cid: str, tool, args: dict, emit) -> ToolResult:
        d: Decision = self.policy.check(tool.name, args)
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
                          timeout_default=int(self.config.data.get("bash_timeout", 120)))
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
                title = f"运行命令：{args.get('command', '')}"
                detail = args.get("command", "")
            elif tool.name in ("write", "edit"):
                title = f"{'写入' if tool.name == 'write' else '编辑'}文件：{args.get('path', '')}"
                detail = d.reason
            elif tool.name == "apply_patch":
                title, detail = "应用补丁", args.get("patch", "")
            else:
                title, detail = f"使用工具 {tool.name}", json.dumps(args, ensure_ascii=False)[:2000]
            fut = asyncio.get_running_loop().create_future()
            emit(ApprovalRequest(cid, tool.name, title, detail, d.prefix, fut, ))
            return await fut

    # ------------------------------------------------------------ 侧路请求（不碰主会话）
    async def side_complete(self, system: str, user: str) -> str:
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        out = []
        comp = None
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

    def compact(self) -> str:
        """M2：压缩（用户边界、保留锚点、摘要用 flash）。M1 占位。"""
        return "压缩功能在 M2 实现。当前上下文剩余 {:.0%}。".format(self.context_left())


def _friendly_error(e: Exception) -> str:
    status = getattr(e, "status_code", None)
    name = type(e).__name__
    if status == 401:
        return "API key 无效或未授权（401）。请检查对应的环境变量。"
    if status == 404:
        return f"模型或接口不存在（404）：{e}"
    if status:
        return f"请求失败（{status}）：{str(e)[:500]}"
    return f"{name}: {str(e)[:500]}"
