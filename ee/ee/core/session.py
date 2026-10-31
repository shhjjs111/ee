"""主循环（asyncio）：一个用户回合 = 组装请求 → 流式调用 → 收集 tool_calls → 执行 → 追加结果 → 再调用。

- 结束：模型这一步没有工具调用（且没有排队的用户输入）。
- 安全上限 max_steps；到最后一步追加“只用文字总结，不再调工具”。
- 只读工具同一步并行；写操作按顺序。每个 tool_call_id 必有输出（中断写“用户中断”）。工具错误原样回喂。
- interrupt()：取消当前流/工具，保留已产生的历史，可继续对话。
- queue_input()：运行中继续打字。插话（steer）在当前步结束后送入；待追加（follow）在回合本该结束时送入；
  回合结束时没送入的退回界面（core/queue.py）。
界面只通过 run_turn() 产出的事件流与内核交互。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import AsyncIterator, Callable

from ee.config import Config, ee_home
from ee.core import compaction as C
from ee.core import gitinfo
from ee.core import hooks
from ee.core.instructions import load_instructions
from ee.core.memory import load_memory
from ee.core.repomap import startup_message as repo_map_message
from ee.core import parallel
from ee.core import queue as Q
from ee.core import reasoning as RZ
from ee.core import styles as ST
from ee.core import sysprompt as SP
from ee.core.budget import BudgetGuard
from ee.core.loopguard import REPEAT_NOTE, REPEAT_STOP, LoopGuard, signature
from ee.core.context import ContextUpdates, FrozenPrefix, UsageStats, env_parts, load_prompt, system_prompt
from ee.core.events import (ApprovalDecision, ApprovalRequest, AssistantMessage, Compacted, Error, Notice, PlanUpdate,
                            QueueDelivered, QueueReturned, ReasoningDelta, Status, StepStart, TextDelta, ToolEnd,
                            ToolOutput, ToolStart, TurnDone, TurnStart, Usage)
from ee.core.permissions import MODE_LABEL, Decision, PermissionPolicy
from ee.core.providers import transforms as T
from ee.core.providers.openai_chat import ChatClient, Completion
from ee.core.providers.pricing import discount_for as _discount_for
from ee.core.store import SessionStore
from ee.tools import AGENT_PROFILES, ToolContext, ToolError, ToolResult, ToolState, agent_profile, build_tools
from ee.tools.shellenv import ShellNotFound, resolve_shell
from ee.tools.details import finalize as finalize_details
from ee.tools.truncate import truncate_output_info

log = logging.getLogger("ee.session")

LAST_STEP_NOTE = ("[系统提示] 已达到本回合允许的最大步数。请不要再调用任何工具，"
                  "只用文字总结目前的进展、结果、尚未完成的部分和建议的下一步。")
INTERRUPTED = "用户中断：这次工具调用没有执行完（或没有执行）。"


class RestoreConflict(Exception):
    """要恢复的文件在 ee 改完之后又被用户改过：conflicts = 相对路径，tree = 检测时的当前工作区快照。"""

    def __init__(self, conflicts: list[str], tree: str | None = None):
        super().__init__("这些文件在 ee 改完之后又被改过：" + "、".join(conflicts))
        self.conflicts = list(conflicts)
        self.tree = tree


class Session:
    def __init__(self, config: Config, model: str | None = None, cwd: Path | None = None, mode: str | None = None,
                 client=None, client_factory: Callable | None = None, persist: bool = True,
                 shell: str | None = None, home: Path | None = None, agent: str | None = None,
                 user_home: Path | None = None, orchestrator_role: str | None = None):
        self.config = config
        self.cwd = Path(cwd or config.cwd).resolve()
        self.home = home or ee_home()
        # 真实主目录（技能兼容目录 ~/.agents/skills、~/.claude/skills 的查找起点）。
        # 默认取 Path.home()；测试可以注入隔离目录，否则本机装了技能就会让
        # 「会话开头注入的那几条消息」数量随开发机变化，测试随机红。
        self.user_home = Path(user_home) if user_home is not None else Path.home()

        self._client_override = client
        self._client_factory = client_factory
        self._client = None
        self.spec = config.resolve_model(model)
        # 精简模式 full / lean：会话开始时定下（恢复会话时沿用会话记下的），之后切模型也不变
        self.agent_profile = agent if agent in AGENT_PROFILES else agent_profile(config)
        # 指挥会话（D6-1）：子会话拿到的工具表里没有 thread_*（它不能再建子会话）；
        # 窄接口与调用者身份由所属 Thread 在构造后注入，普通会话保持 None。
        self.orchestrator_role = "child" if orchestrator_role == "child" else None
        self.orchestrator = None
        self.thread_id = None
        self.tools = build_tools(self.spec.profile, config, agent=self.agent_profile,
                                 orchestrator_role=self.orchestrator_role)
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
        try:     # 审批时选过「本项目以后都允许」的规则（D4-8，~/.ee/permissions.json）
            from ee.core import rules as _rules
            self.policy.allow += [r for r in _rules.rules_for(self.cwd, self.home) if r not in self.policy.allow]
        except Exception:  # noqa: BLE001
            pass
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
        self.policy.shell = self.shell.syntax if self.shell else None   # 权限按实际 shell 的语法拆命令
        # 注意：这里不能改成惰性求值。曾经试过（想省掉构造时那次 git status），结果
        # `thread/detail` 之类的 RPC 会在**它自己**第一次访问 .git 时才跑 git 子进程，
        # 把 git 的 200ms+ 同步阻塞搬到了服务线程的热路径上——
        # test_robustness.py::test_slow_git_does_not_block_other_rpcs 立刻抓到（卡 1.6s）。
        # 保持构造时算好：那时还在建会话的路径上，之后的读取都是纯内存访问。
        self._git: gitinfo.GitStatus | None = gitinfo.status(self.cwd)
        self.gh = gitinfo.gh_exe() is not None
        self.queue = Q.InputQueue()          # 运行中排队的用户消息（插话 / 待追加）
        self.notes: list[str] = []           # 例如用户 !命令 的输出，随下一条消息发给模型
        self.turns = 0
        self.turn_starts: list[int] = []     # 每个回合首条 user 消息在 history 里的位置（回退用）
        # 本回合是否可能出现文件改动（由 _access 累积）。只读回合收尾不必再抓一次快照。
        self._turn_may_write = False
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
        self._hook_stop: str | None = None      # Hook 输出 continue=false 时的原因：本步工具跑完后结束回合
        self._approval_lock = asyncio.Lock()
        # 历史锁：回合与手动压缩都会整段改写 self.history（压缩是 self.history = 新历史），
        # 两者并发时后完成的一方会覆盖另一方的消息。这里把它们串行化：回合等压缩做完再开始；
        # 回合进行中手动压缩直接拒绝（回合里的自动压缩在回合内部顺序执行，不再取锁）。
        self._history_lock = asyncio.Lock()
        self._subagent_plan: dict[str, str] = {}   # 本步每个 task 调用的隔离方式（D5-2）：worktree / inplace
        self._subagent_sem: asyncio.Semaphore | None = None   # 本步子代理并发上限
        self.last_reasoning = ""
        self.running = False
        self.instruction_sources: list[str] = []   # 本会话加载的 AGENTS.md 路径（界面可显示）
        self.memory_sources: list[str] = []        # 本会话注入的记忆文件
        self._sys_src: SP.SystemSources | None = None   # 本会话的 SYSTEM.md / APPEND_SYSTEM.md（开始时解析一次后冻结）
        self.skill_names: list[str] = []           # 本会话清单里列出的技能（D5-5）
        # LSP 诊断（D6-2 / T9）：懒建。没启用时永远是 None，工具据此如实报 unavailable。
        # 放在 Session 上而不是全局，是因为诊断缓存属于"这个会话看到的这些文件版本"。
        self.lsp_store = None
        self.lsp_notifier = None
        self.lsp_manager = None
        self._lsp_done = False
        self._lsp_handle = None
        self._lsp_root = ""
        self._lsp_language = "python"
        self._skills_cat = None
        self.guard = LoopGuard()
        self._fresh_usage = False                   # 上次压缩后是否拿到过新的用量（防止反复压缩）
        self.compactions = 0
        # D5-9：推理强度（会话级，初始取按模型记住的 / 配置）、回答风格、按用途分模型、花费上限
        self.effort = RZ.canonical(self.spec.profile, RZ.resolve(config.data, self.home, self.spec.alias))
        sdef = str((config.data.get("style") or {}).get("default") or "default")
        self.style = sdef if ST.exists(self.home, sdef) else "default"
        self._prefix_style: str | None = None      # 冻结进 system 的风格
        self._style_switched = False               # 会话中途切换过风格（之后用 <context-update> 子块说明当前风格）
        self._turn_role: str | None = None         # 本回合按哪个用途取模型（review）；计划模式自动用 plan
        self._role_clients: dict = {}
        self._closing: list = []                   # 换模型时丢下的旧 client，等 aclose() 统一释放
        self.budget = BudgetGuard(config.data, self.home)
        self.budget_owner = True                   # 子代理共用主会话的 BudgetGuard，不负责回合重置
        self.budget_root = None                    # 子代理：主会话（会话花费按主会话合计算）
        self.spent_before: dict[str, float] = {}   # 重启前这个会话已花的钱（按币种），会话上限要算上
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
        self.tools = build_tools(spec.profile, self.config, agent=self.agent_profile,
                                 orchestrator_role=self.orchestrator_role)
        self.tool_map = {t.name: t for t in self.tools}
        self._install_mcp()
        self._drop_client()          # 旧 client 的连接池要释放，不能只丢引用
        self._client = None
        if not same_prefix:
            self.prefix = None
        self.updates.reset()
        self.effort = RZ.canonical(spec.profile, RZ.resolve(self.config.data, self.home, spec.alias))
        return (f"已切换到 {spec.alias}（{spec.model}，家族 {spec.profile.family}）。"
                "换模型后服务端缓存会从零开始。")

    def _drop_client(self) -> None:
        """换模型时把旧 client 摘下来，交给 aclose() 统一释放。

        不能在这里 await（set_model 是同步方法、调用路径很多），也不该在无事件循环时
        直接丢掉引用——那样连接池就永远漏了。统一记进 _closing，aclose() 时一并关。
        """
        old = getattr(self, "_client", None)
        self._client = None
        if old is not None and getattr(old, "aclose", None) is not None:
            self._closing.append(old)

    async def aclose(self) -> None:
        """释放本会话持有的所有 HTTP 连接池（主 client + 按角色缓存的 + 换模型丢下的）。

        服务退出、会话删除、测试收尾都该调；重复调用安全。
        """
        clients = [self._client, *self._role_clients.values(), *self._closing]
        self._client = None
        self._role_clients.clear()
        self._closing.clear()
        for c in clients:
            close = getattr(c, "aclose", None)
            if close is None:
                continue
            try:
                await close()
            except Exception as e:    # noqa: BLE001  一个关不掉不该挡住其他
                log.info("关闭模型客户端时出错（忽略）：%s", e)

    def set_mode(self, mode: str) -> None:
        self.policy.set_mode(mode)

    # ------------------------------------------------------------ D5-9：推理强度 / 回答风格 / 按用途分模型
    def set_effort(self, level, remember: bool = True) -> str:
        """切换本会话的推理强度；remember 时同时按模型记住（下次用这个模型默认就是它）。"""
        lv = RZ.normalize(level)
        if lv is None:
            opts = " / ".join(RZ.labels(self.profile)[x] for x in ["", *RZ.levels(self.profile), "off"])
            raise ValueError(f"未知推理强度：{level}（可选 {opts}）")
        lv = RZ.canonical(self.profile, lv)       # DeepSeek：旧写法 中/高 → 默认/最高
        self.effort = lv
        if remember:
            try:
                RZ.remember(self.home, self.spec.alias, lv)
            except OSError:
                pass
        eff = RZ.effective(self.profile, lv)
        note = "" if eff == lv else "（当前模型不支持这个档位，请求里不会发送）"
        return f"推理强度：{RZ.labels(self.profile)[lv]}{note}"

    def effort_support(self) -> dict:
        return RZ.support(self.profile)

    def set_style(self, sid: str) -> str:
        sid = (sid or "default").strip() or "default"
        if not ST.exists(self.home, sid):
            raise ValueError(f"没有这个回答风格：{sid}")
        self.style = sid
        return f"回答风格：{ST.get(self.home, sid).label}"

    def _style_part(self) -> str | None:
        """会话中途切换过风格时，挂在用户消息尾部的风格子块（未冻结前缀时风格直接进 system）。"""
        if self.prefix is None or self._prefix_style is None:
            return None
        if self.style != self._prefix_style:
            self._style_switched = True
        if not self._style_switched:
            return None
        return ST.switch_part(ST.get(self.home, self.style))

    def _env_parts(self) -> dict:
        parts = env_parts(self.cwd, self.shell, self.git, self.gh, MODE_LABEL.get(self.policy.mode, self.policy.mode),
                          plan_tool="present_plan" in self.tool_map)
        sp = self._style_part()
        if sp:
            parts["style"] = sp
        return parts

    def active_role(self) -> str | None:
        """本次主循环请求的用途：/review 回合 → review；计划模式 → plan；否则主模型。"""
        return self._turn_role or ("plan" if self.policy.mode == "plan" else None)

    def role_spec(self, role: str | None):
        """某个用途实际用的模型（没配 / 配错 → 主模型）。"""
        name = self.config.role_model(role) if role else None
        if not name or name == self.spec.alias:
            return self.spec
        try:
            return self.config.resolve_model(name)
        except ValueError:
            return self.spec

    def role_client(self, spec):
        if spec.alias == self.spec.alias:
            return self.client()
        c = self._role_clients.get(spec.alias)
        if c is None:
            if self._client_override is not None:
                c = ChatClient(spec, client=self._client_override)
            elif self._client_factory is not None:
                c = self._client_factory(spec)
            else:
                c = ChatClient(spec, state_path=self.home / "state" / "quirks.json",
                               network=self.config.data.get("network"))
            self._role_clients[spec.alias] = c
        return c

    async def ensure_role_client(self, spec):
        if spec.alias == self.spec.alias:
            return await self.ensure_client()
        if spec.alias not in self._role_clients:
            await asyncio.to_thread(self.role_client, spec)
        return self._role_clients[spec.alias]

    def effort_for(self, spec) -> str:
        if spec.alias == self.spec.alias:
            return self.effort
        return RZ.resolve(self.config.data, self.home, spec.alias)

    def _current_discount(self, family: str | None = None) -> float:
        """当前时刻的错峰折扣；见 pricing.discount_for。family 决定用哪个厂商的官方峰谷规则。"""
        from datetime import datetime
        return _discount_for(self.config.data.get("pricing") or {}, datetime.now(), family=family)

    async def _charge(self, usage: dict | None, spec) -> float | None:
        """记一次请求的用量：按这次实际用的模型的单价计价，计入会话统计和每日/每月账本。

        同时标记「已花金额是否可信」：有请求缺 usage、或模型没配单价时金额不可知，
        当前累加值只是已知部分。预算检查必须知道这一点，否则会把未知当 0、
        让用户设的上限形同虚设（见 budget.BudgetGuard.accounting_complete）。
        """
        cost = self.stats.add(usage, spec.profile.price, spec.alias, discount=self._current_discount(spec.profile.family), turn=self.turns)
        if cost is None:
            # 未知金额：不能当成免费。
            for g in self._budget_guards():
                g.accounting_complete = False
        elif cost:
            await asyncio.to_thread(self.budget.record, cost, spec.profile.price.currency)
        return cost

    def _budget_guards(self) -> list:
        """需要同步「金额是否可信」的预算守卫（子代理与父会话共用同一个守卫）。"""
        root = self.budget_root or self
        g = getattr(root, "budget", None)
        return [g] if g is not None else []

    def session_spent(self, currency: str) -> float:
        root = self.budget_root or self
        return float(root.spent_before.get(currency, 0.0)) + root.stats.spent(currency)

    async def _budget_check(self, emit) -> str | None:
        g = self.budget
        if not g.active:
            return None

        async def ask(scope: str, text: str) -> str:
            import uuid as _u
            fut = asyncio.get_running_loop().create_future()
            detail = ("继续这一回合：本回合内不再询问；本会话不再询问：直到关闭这个对话；停止：结束本回合。"
                      "上限在 设置 → 回答与花费 里修改。")
            emit(ApprovalRequest(f"budget_{_u.uuid4().hex[:8]}", "budget", text, detail, "", fut, force=True))
            dec = await fut
            return {"once": "once", "always": "always", "forever": "always"}.get(dec.choice, "deny")

        return await g.check(self.session_spent, lambda t: emit(Notice(t, "warn")), ask)

    def set_shell(self, name: str) -> str:
        sh = resolve_shell(name)   # 找不到会抛 ShellNotFound
        self.shell, self.shell_name = sh, name
        self.policy.shell = sh.syntax
        return f"shell 已切换为 {sh.display}（{sh.exe}）。下一条消息会以增量环境信息告诉模型。"

    @property
    def pending(self) -> list[str]:
        """排队中的消息原文（按到达顺序，只读）。"""
        return self.queue.texts()

    def queue_input(self, text: str, kind: str = Q.STEER, qid: str | None = None) -> str:
        """运行中排队一条用户消息，返回它的 id（撤回用）。kind：steer 插话 / follow 完成后追加。"""
        return self.queue.add(text, kind, qid).id

    def withdraw_input(self, qid: str):
        """撤回还没送入的排队消息；已送入或不存在返回 None。"""
        return self.queue.withdraw(qid)

    async def _deliver(self, kind: str, emit) -> bool:
        """把 kind 类排队消息送进历史（按 one/all），送了返回 True。"""
        if not self.queue.has(kind):
            return False
        await asyncio.to_thread(self.refresh_git)
        modes = Q.mode_settings(self.config.data)      # 取出到写入历史之间没有挂起点：中断不会丢消息
        items = self.queue.take(kind, modes["steering_mode" if kind == Q.STEER else "follow_up_mode"])
        if not items:     # 等 git 时被撤回了
            return False
        self._add_user(Q.join(items))
        emit(QueueDelivered([q.id for q in items], kind))
        return True

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
        self.queue.clear()
        self.notes.clear()
        self.turns = 0
        self.turn_starts = []
        self.turn_trees = []
        self.instruction_sources = []
        self.memory_sources = []
        self.skill_names = []
        self._skills_cat = None
        self._fresh_usage = False
        self.store = SessionStore(self.home, enabled=self.store.enabled)

    def _invalidate_read_refs(self, why: str) -> None:
        """历史被改写后，作废"已返回过引用"的记账，下次 read 给全文。

        为什么需要集中一处：read 的「与上次读取相同，内容见上文」只有在
        那段内容**当前仍在上下文里**时才成立。压缩/修剪会重建历史，回退与
        编辑重发会截断历史——后者原来没清 reads，于是模型被指向一段已经不存在的
        "上文"，既浪费一轮又可能据此改错代码。

        只清 reads（引用计数），保留 files 条目本身：was_read 的语义是
        "这个会话了解这个文件"，与"内容此刻是否可见"是两件事。
        """
        for rec in self.state.files.values():
            rec.reads.clear()
        log.debug("已作废读引用记账（%s），后续 read 将返回全文", why)

    def rewind_last_turn(self) -> str | None:
        """回退（backtrack）：删掉最后一个回合，返回那条用户消息原文供重新编辑。文件改动不回滚（用 /undo）。"""
        if self.running or not self.turn_starts:
            return None
        i = self.turn_starts.pop()
        content = self.history[i].get("content") or ""
        text = content.split("\n\n<context-update")[0]
        self.history = self.history[:i]
        self.updates.reset()          # 被删的消息可能带着基线附录，下一条补发全量
        self._invalidate_read_refs("rewind")
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
        self._invalidate_read_refs("truncate")
        self.queue.clear()
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

    def revert_trees(self, entries: list[tuple[str, str | None]], force: bool = False,
                     on_restore_point=None) -> list[str] | None:
        """编辑重发：entries = 被撤回的各回合 (回合前 tree, 回合后 tree)，第一项是被编辑的那条消息。
        把这些回合改过的文件恢复到第一项的回合前版本。磁盘快照不可用返回 None。

        用户在这些回合之后（或两回合之间）手改过其中的文件：force=False 时抛 RestoreConflict，什么都不动；
        force=True 时先把当前工作区 tree 交给 on_restore_point（存成恢复点，可反悔）再恢复。"""
        d = self.disk()
        if d is None or not entries or not entries[0][0]:
            return None
        base = entries[0][0]
        try:
            cur = d.capture()
            paths: list[str] = []
            for before, after in entries:
                if not before:
                    continue
                after = after or cur
                paths += d.changed(before, after) if after else []
            paths = list(dict.fromkeys(paths))
            conflicts = self._conflicts(d, entries, cur, paths)
        except Exception as e:  # noqa: BLE001
            log.warning("磁盘快照恢复失败：%s", e)
            return None
        if conflicts and not force:
            raise RestoreConflict(conflicts, cur)
        if force and cur and on_restore_point is not None and paths:
            on_restore_point(cur)
        note = "[系统提示] 用户编辑并重发了之前的消息，以下文件已恢复到那条消息之前的内容（如需再改请重新 read）："
        from ee.core.snapshot import RestorePartial
        try:
            restored = self._disk_restore(d, base, paths, note)
        except RestorePartial:
            self._drop_turn_trees(base)      # 编辑重发照常进行（调用方如实报告没恢复的文件）
            raise
        if restored is None:
            return None
        self._drop_turn_trees(base)
        self._after_restore(restored, note)
        return restored

    def _drop_turn_trees(self, base: str) -> None:
        for i, e in enumerate(self.turn_trees):
            if e.get("before") == base:
                del self.turn_trees[i:]
                break

    def _disk_restore(self, d, tree: str, paths: list[str], note: str = "") -> list[str] | None:
        """预检没过（RestoreBlocked，一个没动）/ 恢复到一半（RestorePartial）原样抛给调用方如实报告；
        其他失败（git 出错）返回 None，调用方回退内存快照。"""
        from ee.core.snapshot import RestoreBlocked, RestorePartial
        try:
            return d.restore(tree, paths)
        except RestoreBlocked:
            raise
        except RestorePartial as e:
            self._after_restore(e.restored, note or "[系统提示] 以下文件已被恢复成之前的内容（如需再改请重新 read）：")
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("磁盘快照恢复失败：%s", e)
            return None

    @staticmethod
    def _conflicts(d, entries: list[tuple[str, str | None]], cur: str | None, paths: list[str]) -> list[str]:
        """要恢复的文件里，哪些在 ee 的回合结束后被别人（用户/编辑器）改过：
        回合后 tree → 下一回合前 tree（两回合之间）、最后一回合后 tree → 当前。回合后没有快照的（中断）无从比较。"""
        want = set(paths)
        if not want:
            return []
        hit: set[str] = set()
        for i, (_before, after) in enumerate(entries):
            nxt = entries[i + 1][0] if i + 1 < len(entries) else cur
            if after and nxt and after != nxt:
                hit |= want & set(d.changed(after, nxt))
        if hit and cur and entries[0][0]:     # 当前内容已经和恢复目标一样的（比如上次恢复到一半已写好的）不会丢东西
            hit &= set(d.changed(entries[0][0], cur))
        return sorted(hit)

    def _disk_undo(self, force: bool = False, on_restore_point=None) -> list[str] | None:
        d = self.disk()
        if d is None or not any(e.get("before") for e in self.turn_trees):
            return None
        cur = None
        for e in reversed(self.turn_trees):
            if e.get("undone") or not e.get("before"):
                continue
            try:
                cur = cur or d.capture()
                after = e.get("after") or cur
                paths = d.changed(e["before"], after) if after else []
                if not paths:
                    continue
                conflicts = self._conflicts(d, [(e["before"], e.get("after"))], cur, paths)
            except Exception as ex:  # noqa: BLE001
                log.warning("磁盘快照撤销失败：%s", ex)
                return None
            if conflicts and not force:
                raise RestoreConflict(conflicts, cur)
            if force and cur and on_restore_point is not None:
                on_restore_point(cur)
            note = "[系统提示] 用户执行了 /undo，以下文件已恢复到你上一回合修改之前的内容（如需再改请重新 read）："
            restored = self._disk_restore(d, e["before"], paths, note)   # 恢复到一半：不标已撤销，关掉占用后可再撤一次
            if restored is None:
                return None
            e["undone"] = True
            self.last_undo_tree = e["before"]
            self._after_restore(restored, note)
            return restored
        return []

    def undo(self, force: bool = False, on_restore_point=None) -> list[str]:
        """/undo：把最近一个有改动、还没撤销过的回合的文件改动恢复成改动前。
        有磁盘快照时按回合前后 tree 的差异恢复（含 shell 改的文件，重启后也行）；否则用内存快照。
        磁盘快照下发现用户在回合后手改过要恢复的文件：force=False 抛 RestoreConflict（不动文件），
        force=True 先调 on_restore_point(当前 tree) 存恢复点再恢复。"""
        self.last_undo_tree = None
        res = self._disk_undo(force, on_restore_point)
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

    # ------------------------------------------------------------ LSP 接线（D6-2 / T9 U5）
    async def _attach_lsp(self, emit) -> None:
        """首次回合把 LSP 管理器 / 诊断缓存 / 变更通知接到会话上（只做一次）。

        为什么必须有这一步：`LspTool` 在工具表里是**无参构造**的，它的 manager 和
        store 都为空；`_store_from_ctx` 从 `session.lsp_store` 取，而 `_notify_lsp`
        又要 `session.lsp_notifier`。这三者原先只在 `__init__` 里被置成 None，
        全仓没有赋值点——工具永远报「没有启用」，写后通知永远在第一行 return。
        **假服务器测试测不到这一层**，因为它们手工 `LspTool(manager=..., store=store)`。

        失败一律只出提示，不影响回合：诊断是辅助信息，缺了它编辑仍要能正常干活。
        """
        if self._lsp_done or self.prefix is not None:
            return
        self._lsp_done = True
        data = self.config.data
        if not (data.get("lsp") or {}).get("enabled"):
            return

        from ee.core.lsp import diagnostics as D
        from ee.core.lsp import manager as M
        from ee.core.lsp import notify as N
        from ee.core.lsp import servers as S

        # 用户配置与项目配置合并：项目只能关闭/收紧，不能指定可执行文件。
        user = ((data.get("lsp") or {}).get("servers") or {})
        project = {}
        try:
            project = _read_project_lsp(self.cwd)
        except Exception as e:  # noqa: BLE001  配置文件坏了不该拦住会话
            emit(Notice(f"读取项目 LSP 配置失败（忽略）：{e}", "warn"))
        cfg = S.merge_config(user, project)
        if cfg.get("enabled") is False:
            return

        language = _lsp_language_for_root(self.cwd)
        spec = S.resolve(language, cfg)
        if not spec.available:
            # 未安装：明确说清楚，并且**不尝试安装**。此时不接 manager，
            # 工具会如实报 unavailable——那正是「没取到诊断」的正确表述。
            emit(Notice(f"LSP：{spec.reason}", "info"))

        mgr = M.LspManager()
        store = D.DiagStore()
        notifier = N.Notifier(store)

        if spec.available:
            try:
                handle = await mgr.acquire(str(self.cwd), language, spec.identity, spec=spec)
                client = handle.entry.client
                # 这一行是整条链的接口：没有它就永远收不到诊断。
                client.on_notification["textDocument/publishDiagnostics"] = (
                    lambda params: store.on_publish(params))
                self._lsp_handle = handle
                self._lsp_root = str(self.cwd)
                self._lsp_language = language
                notifier.attach("", client)      # 占位，真正的 uri 在首次通知时绑
            except Exception as e:  # noqa: BLE001
                emit(Notice(f"LSP 启动失败（忽略，编辑不受影响）：{e}", "warn"))

        self.lsp_manager = mgr
        self.lsp_store = store
        self.lsp_notifier = notifier
        # 工具表里的 LspTool 是无参构造的，把真实能力补给它。
        for t in self.tools:
            if t.name == "lsp":
                t.manager = mgr
                t.store = store
                t.root = str(self.cwd)

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
                    t = McpTool(mgr, st.handle, spec, trusted=st.cfg.trusted, timeout=timeout)
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
        self._git = gitinfo.status(self.cwd)

    @property
    def git(self):
        """当前 git 状态（构造时就算好，refresh_git 刷新）。读它永远是纯内存访问。

        别改成惰性求值：那会把 git 子进程挪到第一个访问者身上，
        `thread/detail` 这类 RPC 会因此同步阻塞事件循环
        （test_robustness.py::test_slow_git_does_not_block_other_rpcs 守着这条）。"""
        return self._git

    @git.setter
    def git(self, v) -> None:
        self._git = v

    def context_left(self) -> float:
        return 1.0 - self.stats.context_used(self.profile.context_window)

    # ------------------------------------------------------------ 前缀
    def current_prefix(self) -> FrozenPrefix:
        style = self._prefix_style if (self.prefix is not None and self._prefix_style is not None) else self.style
        seg = ST.system_segment(ST.get(self.home, style))
        return FrozenPrefix.freeze(system_prompt(self.profile, seg, self.system_sources(), self.tools, self.agent_profile),
                                   [t.schema() for t in self.tools])

    def system_sources(self) -> SP.SystemSources:
        """自定义系统提示词来源：会话里第一次用到时解析，之后不变（改文件 / 信任从新对话生效）。"""
        if self._sys_src is None:
            self._sys_src = SP.resolve(self.cwd, self.home, self.config.data)
        return self._sys_src

    def _prefix_compat(self) -> str:
        """决定 system/tools 形状的会话属性；恢复会话时只复用同一组属性下冻结的前缀。"""
        p = self.spec.profile
        return f"{self.agent_profile}|{p.prompt_patch}|{p.edit_style}"

    def _restore_prefix(self) -> bool:
        """重启后恢复会话：复用会话存盘里最后一次冻结的 system+tools（逐字节），而不是按当前文件 / 风格 / MCP 重算。

        原因：前缀缓存按字节匹配。重启后重算的前缀只要差一个字（会话中途切过风格、改过提示词文件、
        MCP 工具顺序或启停变了），整段历史的缓存都会失效，下一次请求按全价计费。
        存盘记录被脱敏改写过（sha256 对不上）或属性不同（换了精简模式 / 模型补丁）时不复用。"""
        if not self.history or not self.store.enabled or not self.store.path.is_file():
            return False
        from ee.core.store import SessionStore
        rec = SessionStore.last_prefix(self.store.path, self._prefix_compat())
        if not rec:
            return False
        try:
            fp = FrozenPrefix(dict(rec["system"]), list(rec["tools"]), str(rec["sha256"]))
        except (KeyError, TypeError, ValueError):
            return False
        if not fp.verify():
            return False
        self.prefix = fp
        style = rec.get("style")
        self._prefix_style = style if isinstance(style, str) and style else "default"
        self._style_switched = False
        log.info("恢复会话 %s：沿用存盘里冻结的前缀 %s", self.store.id, fp.sha256[:12])
        return True

    def _ensure_prefix(self, emit) -> FrozenPrefix:
        if self.prefix is None and self._restore_prefix():
            return self._ensure_prefix(emit)
        if self.prefix is None:
            self._prefix_style = self.style          # 风格段随 system 冻结；之后切换走 <context-update>
            self._style_switched = False
            self.prefix = self.current_prefix()
            # 整个前缀落盘：重启恢复会话时逐字节复用（_restore_prefix），保住前缀缓存
            self.store.append("prefix", {"sha256": self.prefix.sha256, "model": self.spec.model,
                                         "compat": self._prefix_compat(), "style": self._prefix_style,
                                         "system": self.prefix.system, "tools": self.prefix.tools})
            if getattr(self, "hooks_role", "main") != "subagent":
                for text, level in self.system_sources().notices():
                    emit(Notice(text, level))
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
        appendix = self.updates.build(self._env_parts())
        body = text
        if self.notes:
            body = "\n\n".join(self.notes) + "\n\n" + text
            self.notes = []
        msg = {"role": "user", "content": body + "\n\n" + appendix}
        # read(as_image=true) 登记的图片（方案 A）：本轮工具产出，下一轮注入视觉上下文。
        pending = getattr(self.state, "pending_images", None) or []
        if pending:
            merged = list(images or []) + [p for p in pending if p not in (images or [])]
            self.state.pending_images = []
            images = merged or None
        if images:
            msg["_images"] = list(images)
        self._append(msg)

    async def _ensure_instructions_async(self) -> None:
        if self._sys_src is None and self.prefix is None:   # 文件读取放到线程里，别卡事件循环
            self._sys_src = await asyncio.to_thread(SP.resolve, self.cwd, self.home, self.config.data)
        if self.history:
            return
        cfg = self.config.data.get("agents_md", {}) or {}
        if cfg.get("enabled", True):
            ins = await asyncio.to_thread(load_instructions, self.cwd, self.home, int(cfg.get("max_bytes", 32768)))
            msg = ins.message()
            if msg is not None and not self.history:
                self._append(msg)
                self.instruction_sources = [str(p) for p in ins.sources]
        if any(m.get("_sub") in ("memory", "skills") for m in self.history) \
                or any(m.get("_kind") != "agents" for m in self.history):
            return
        # 长期记忆：紧跟 AGENTS.md 的固定 user 消息，会话内不再改动（保住前缀缓存）
        mem = await asyncio.to_thread(load_memory, self.home, self.cwd, self.config.data.get("memory") or {},
                                      "memory" in self.tool_map)
        mmsg = mem.message()
        if mmsg is not None:
            self._append(mmsg)
            self.memory_sources = list(mem.sources)
        # 技能清单（D5-5）：紧跟记忆，同样固定不变
        smsg = await asyncio.to_thread(self._skills_message)
        if smsg is not None and not any(m.get("_kind") != "agents" for m in self.history):
            self._append(smsg)
        # 仓库地图（repo_map.auto_inject，默认关）：同样是会话开始时的固定 user 消息
        rmsg = await asyncio.to_thread(repo_map_message, self.cwd, self.home, self.config.data)
        if rmsg is not None:
            self._append(rmsg)

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
        mem = load_memory(self.home, self.cwd, self.config.data.get("memory") or {}, "memory" in self.tool_map)
        mmsg = mem.message()
        if mmsg is not None:
            self._append(mmsg)
            self.memory_sources = list(mem.sources)
        smsg = self._skills_message()
        if smsg is not None:
            self._append(smsg)
        rmsg = repo_map_message(self.cwd, self.home, self.config.data)
        if rmsg is not None:
            self._append(rmsg)

    # ------------------------------------------------------------ 技能（D5-5）
    def skill_catalog(self, refresh: bool = False):
        """本会话的技能目录：首次用到时扫描并缓存（skill 工具找不到名字时会 refresh 重扫一次）。会在线程里调用。"""
        if self._skills_cat is None or refresh:
            from ee.core import skills as K
            self._skills_cat = K.discover(self.home, self.cwd, self.config.data, user_home=self.user_home)
        return self._skills_cat

    def _skills_message(self) -> dict | None:
        from ee.core import skills as K
        if not K.settings(self.config.data).get("enabled", True):
            return None
        blk = K.load_block(self.home, self.cwd, self.config.data, user_home=self.user_home,
                           catalog=self.skill_catalog(), tool="skill" in self.tool_map)
        self.skill_names = list(blk.names)
        return blk.message()

    def skill_mention_notes(self, text: str) -> list[tuple[str, str]]:
        """用户消息里显式点名（开头 /名字 或 $名字）的已启用技能 → [(名字, 附在消息前的全文提示)]。"""
        from ee.core import skills as K
        if "$" not in (text or "") and not (text or "").lstrip().startswith("/"):
            return []
        if not K.settings(self.config.data).get("enabled", True):
            return []
        cat = self.skill_catalog()
        found = K.mentions(text, {s.name for s in cat.enabled()})
        if not found:                       # 可能是会话开始后新建的技能
            cat = self.skill_catalog(refresh=True)
            found = K.mentions(text, {s.name for s in cat.enabled()})
        out = []
        for n in found:
            try:
                out.append((n, K.mention_note(cat.skills[n])))
            except OSError as e:
                log.warning("读取技能 %s 失败：%s", n, e)
        return out

    def _fill_missing(self, why: str) -> None:
        for cid in list(self._open_calls):
            self._append({"role": "tool", "tool_call_id": cid, "content": self._results.get(cid, why)})
        self._open_calls.clear()
        self._results.clear()

    # ------------------------------------------------------------ 回合
    async def run_turn(self, text: str, images: list[str] | None = None) -> AsyncIterator:
        q: asyncio.Queue = asyncio.Queue()
        if self._history_lock.locked():
            yield Status("正在压缩上下文，压缩完成后开始这一回合…")
        await self._history_lock.acquire()          # 与压缩串行（见 __init__ 里 _history_lock 的说明）
        self.running = True
        self._task = asyncio.create_task(self._turn(text, q.put_nowait, images))
        try:
            while True:
                ev = await q.get()
                if isinstance(ev, TurnDone) and self.turn_trees and self.turn_trees[-1].get("after") is None:
                    # 只读回合不可能有文件改动，before 就是 after：那一次 capture 的结论是
                    # 提前已知的，却要花 3 个 git 子进程（实测 140–220ms）。省掉。
                    # 留下 after=None 也不影响 /undo：_disk_undo 里写的是
                    # `after = e.get("after") or cur`，缺了就退回实时快照。
                    if self._turn_may_write:
                        try:
                            ev.tree_after = await asyncio.to_thread(self.disk_capture)
                        except Exception:  # noqa: BLE001
                            ev.tree_after = None
                        self.turn_trees[-1]["after"] = ev.tree_after
                    else:
                        self.turn_trees[-1]["after"] = self.turn_trees[-1].get("before")
                if isinstance(ev, TurnDone) and self.queue:   # 没送入的排队消息退回界面（之后没有挂起点，不会再有人排进来）
                    yield QueueReturned([q.to_dict() for q in self.queue.drain()])
                yield ev
                if isinstance(ev, TurnDone):
                    break
        finally:
            try:
                if self._task and not self._task.done():
                    self._task.cancel()
                    try:
                        await self._task
                    except BaseException:
                        pass
            finally:
                self.running = False
                self._history_lock.release()

    async def _turn(self, text: str, emit, images: list[str] | None = None) -> None:
        final = ""
        try:
            self.turns += 1
            self._hook_stop = None
            self.state.new_turn()
            self.guard = LoopGuard.from_config(self.config.data.get("loop_guard"))
            if self.budget_owner:
                self.budget.reload(self.config.data)
                self.budget.new_turn()
            log.info("回合开始：会话 %s 第 %d 回合 模型 %s cwd=%s", self.store.id, self.turns, self.spec.alias, self.cwd)
            await self._ensure_instructions_async()
            await self._attach_mcp(emit)
            await self._attach_lsp(emit)
            await self._maybe_auto_compact(emit)
            if await hooks.on_turn_start(self, text, emit):   # D5-4 Hooks：SessionStart / UserPromptSubmit 拦截
                emit(TurnDone("blocked", ""))
                return
            self.turn_starts.append(len(self.history))
            await asyncio.to_thread(self.refresh_git)
            for sname, snote in await asyncio.to_thread(self.skill_mention_notes, text):   # 显式调用技能（D5-5）
                self.notes.append(snote)
                emit(Notice(f"已加载技能 {sname}", "info"))
            self._add_user(text, images)
            if images and not self.profile.vision:
                emit(Notice(f"当前模型 {self.spec.alias} 不支持看图，这 {len(images)} 张图片没有发给模型（换支持图像的模型，例如 deepseek-flash）。", "warn"))
            self._turn_may_write = False       # 本回合的写标记从这里重新累积
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
                stop = await self._budget_check(emit)        # D5-9：花费上限
                if stop:
                    emit(Notice(stop, "warn"))
                    emit(TurnDone("budget", final))
                    return
                emit(StepStart(step))
                comp = await self._sample_or_compact(emit)
                if comp.content:
                    final = comp.content
                if not comp.tool_calls:
                    # 插话先送；没有插话时送待追加（回合本该结束，在 Stop Hook 之前）
                    if not last and (await self._deliver(Q.STEER, emit) or await self._deliver(Q.FOLLOW, emit)):
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
                if self._hook_stop is not None:   # Hook 输出 continue=false：结束本回合
                    why, self._hook_stop = self._hook_stop, None
                    emit(Notice(f"Hook 要求结束本回合：{why}", "warn"))
                    emit(TurnDone("completed", final))
                    return
                if self._end_turn and not self.queue.has(Q.STEER):   # present_plan：方案已提交，等用户批准（待追加的退回）
                    self._end_turn = False
                    emit(TurnDone("completed", final))
                    return
                if self.guard.halt:
                    emit(Notice(self.guard.halt, "warn"))
                    emit(TurnDone("loop", final))
                    return
                if note:
                    self._append({"role": "user", "content": note, "_kind": "note"})
                await self._deliver(Q.STEER, emit)
        except asyncio.CancelledError:
            log.info("回合被中断：会话 %s", self.store.id)
            self._fill_missing(INTERRUPTED)
            emit(TurnDone("interrupted", final))
        except Exception as e:  # noqa: BLE001
            log.error("回合出错：会话 %s：%s", self.store.id, e, exc_info=True)
            self._fill_missing("执行出错，未完成。")
            emit(Error(_friendly_error(e)))
            emit(TurnDone("error", final))
        finally:
            # 兜底：KeyboardInterrupt / SystemExit 这类 BaseException 不是 Exception 的子类，
            # 上面两个 except 都接不住；不补的话它们同样会留下孤儿 tool_calls（会话永久 400）。
            # _fill_missing 幂等（_open_calls 已空时什么也不做），正常路径下这里是空操作。
            self._fill_missing(INTERRUPTED)


    async def _sample_or_compact(self, emit) -> Completion:
        """服务端报上下文超长时：自动压缩一次再重试（自动压缩关了就直接报错，让用户自己决定）。"""
        from ee.core.providers.openai_chat import ModelError
        try:
            return await self._sample(emit)
        except ModelError as e:
            cfg = self.config.data.get("compact", {}) or {}
            if getattr(e, "kind", "") != "overflow" or not cfg.get("auto", True) \
                    or not any(m.get("_kind") != "agents" for m in self.history):
                raise
            emit(Notice("对话超出了模型的上下文长度上限，先自动压缩上下文再重试一次。", "warn"))
            r = await self._compact(emit, "overflow")
            if not r.get("ok"):
                raise
            return await self._sample(emit)

    async def _sample(self, emit) -> Completion:
        prefix = self._ensure_prefix(emit)
        spec = self.role_spec(self.active_role())     # 计划模式 / 审查回合可配不同模型（D5-9）
        messages = [prefix.system] + T.wire_messages(self.history, spec.profile)
        comp: Completion | None = None
        text, reasoning = [], []
        emit(Status(f"正在准备请求 {spec.alias}…"))
        try:
            client = await self.ensure_role_client(spec)
            async for kind, val in client.stream(messages, prefix.tools, effort=self.effort_for(spec)):
                if kind == "text":
                    text.append(val); emit(TextDelta(val))
                elif kind == "reasoning":
                    reasoning.append(val); emit(ReasoningDelta(val))
                elif kind == "notice":
                    # 兼容两种形态：纯文本（告警）与已构造好的 Notice（带 metrics 的计时数据）。
                    emit(val if isinstance(val, Notice) else Notice(val, "warn"))
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
        except BaseException:
            # 普通异常也要把「界面上已经显示的那段」落库：否则用户看得见、历史里没有，
            # 重发后模型像失忆。同时这次请求必须入账（缺 usage 记一条 usage_missing），
            # 否则总额看起来完整、其实漏了若干次。
            await self._keep_partial(text, reasoning, spec, emit)
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
        cost = await self._charge(comp.usage, spec)
        u = comp.usage or {}
        emit(Usage(u.get("prompt", 0), u.get("completion", 0), u.get("hit"), cost, self.stats.totals()))
        emit(AssistantMessage(comp.content, comp.reasoning))
        if comp.recovered:
            emit(Notice(f"模型把 {len(comp.tool_calls)} 个工具调用写进了正文，已自动解析执行。"))
        return comp

    async def _keep_partial(self, text: list[str], reasoning: list[str], spec, emit) -> None:
        """流中途失败时：把已显示的内容落库，并把这次请求记进账（usage 记为缺失）。

        只做「保住已经发生的部分」，不改错误处理本身——异常照原样往上抛。
        内容为空时不落库（避免把一条空 assistant 塞进历史，那会让下一条请求 400）。
        """
        content = "".join(text)
        reason = "".join(reasoning)
        if content or reason:
            msg = {"role": "assistant", "content": content, "_partial": True}
            if reason:
                msg["reasoning"] = reason
            self._append(msg)
        try:
            await self._charge(None, spec)          # 缺 usage：记一条 usage_missing
        except Exception:  # noqa: BLE001
            log.warning("流中途失败后记账失败（忽略）：会话 %s", self.store.id)

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
        await self._dispatch(prepared, emit)
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

    def _access(self, tool, args: dict, err: str | None) -> str:
        """调用的访问类型（D5-2）：read 可并发 / write 独占且保序 / free 不过闸门。

        顺便记下本回合有没有可能出现文件改动（_turn_may_write）。只读回合不必在收尾再抓
        一次磁盘快照——before 和 after 必然相同，那一次 capture 的结论是提前就知道的，
        却要花掉 3 个 git 子进程（Windows 上每个约 70ms，实测 capture 约 140–220ms）。"""
        if tool is None or err is not None:
            return parallel.READ          # 直接回错误，不碰任何东西
        fn = getattr(tool, "access", None)
        if fn is not None:
            try:
                kind = fn(args, self)
            except Exception:  # noqa: BLE001
                self._turn_may_write = True
                return parallel.WRITE
            if kind != parallel.READ:
                self._turn_may_write = True
            return kind
        kind = parallel.READ if tool.read_only else parallel.WRITE
        if kind != parallel.READ:
            self._turn_may_write = True
        return kind

    async def _dispatch(self, prepared: list, emit) -> None:
        """一步里的调用同时派发，按读写闸门并发/排队；单个调用出错不影响其它调用；取消传给所有调用。"""
        from ee.core import subagent as _sub
        kinds = [self._access(p[1], p[2], p[3]) for p in prepared]
        self._subagent_plan = await _sub.plan_step(self, prepared, kinds, emit)
        for i, p in enumerate(prepared):
            iso = self._subagent_plan.get(p[0]["id"])
            if iso == "worktree":
                kinds[i] = parallel.FREE
        sc = _sub.subagent_config(self.config.data)
        self._subagent_sem = asyncio.Semaphore(max(1, int(sc.get("max_parallel") or 1)))
        gate = parallel.RWGate()

        async def one(p, kind):
            tc = p[0]
            try:
                async with gate.hold(kind):
                    await self._exec_one(*p, emit)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001  某个调用出了意外：只记它自己的结果
                log.error("工具调用异常：%s", tc["function"]["name"], exc_info=True)
                cid = tc["id"]
                if cid not in self._results:
                    msg = f"工具内部错误：{type(e).__name__}: {e}"
                    self._results[cid] = msg
                    self._ok[cid] = False
                    emit(ToolEnd(cid, tc["function"]["name"], False, msg))

        tasks = [asyncio.ensure_future(one(p, k)) for p, k in zip(prepared, kinds)]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _exec_one(self, tc: dict, tool, args: dict, err: str | None, emit) -> None:
        cid = tc["id"]
        name = tc["function"]["name"]
        pre = None
        if err is None and tool is not None:     # 先跑 PreToolUse，ToolStart 显示的是最终执行的参数
            pre = await hooks.pre_tool_use(self, cid, tool, args, emit)   # D5-4 Hooks：可拦截 / 改参数 / 免审批
            args = pre.args
        summary = tool.summary(args) if tool else name
        emit(ToolStart(cid, name, args, summary))
        t0 = time.monotonic()
        if err is not None:
            res = ToolResult(err, ok=False, summary=summary)
        else:
            res = await self._guarded_run(cid, tool, args, emit, pre)
            res = await hooks.post_tool_use(self, cid, tool, args, res, emit)   # D5-4 Hooks
        # A6：content 给模型（截断后进历史），details 只给界面（不进历史，持久化在条目上）
        out, trunc = truncate_output_info(res.output, self.home / "tool-output", name)
        self._results[cid] = out
        self._ok[cid] = res.ok
        details = finalize_details(name, res, int((time.monotonic() - t0) * 1000), trunc)
        emit(ToolEnd(cid, name, res.ok, res.output, res.summary or summary, res.diff, details))
        # LSP（D6-2 / T9）：写成功后同步文件变更，让下一次取诊断拿到新版本。
        # 只记日志、不影响工具结果——诊断失败不能反噬已经成功的写入。
        if res.ok and name in ("write", "edit", "apply_patch"):
            try:
                await self._notify_lsp(args, res)
            except Exception as e:  # noqa: BLE001
                log.info("LSP 变更通知失败（忽略）：%s", e)
        if res.meta.get("kind") == "plan":
            emit(PlanUpdate(res.meta.get("items", [])))
        if res.ok and res.meta.get("end_turn"):
            self._end_turn = True

    async def _notify_lsp(self, args: dict, res) -> None:
        """把写成功后的文件内容同步给语言服务器（didOpen / didChange）。

        没启用 LSP、没绑到服务器、文件读不出来——都直接返回，不抛。
        调用方已经把它们包在 try 里了，这里再稳一层：诊断是辅助信息，
        不能因为同步失败就让用户以为"文件没写成功"。
        """
        if self.lsp_notifier is None:
            return
        from ee.tools.lsp import uri_for

        raw = (res.meta or {}).get("path") or args.get("path") or ""
        if not raw:
            return
        path = self.cwd / raw if not Path(raw).is_absolute() else Path(raw)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        uri = uri_for(path)
        lang = _language_of(path)
        if self.lsp_notifier.client_for(path=str(path), uri=uri) is None:
            return
        await self.lsp_notifier.changed(str(path), uri, text, language=lang)

    async def _guarded_run(self, cid: str, tool, args: dict, emit, pre=None) -> ToolResult:
        if pre is None:
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
            if dec.choice in ("always", "forever"):
                self.policy.remember(d, tool.name)
            if dec.choice == "forever":
                from ee.core import rules as _rules
                rule = _rules.rule_from_decision(tool.name, d.prefix)
                if rule:
                    try:
                        _rules.add(self.cwd, rule, self.home, title=str(args.get("command") or args.get("path") or tool.name))
                    except OSError:
                        pass
                    if rule not in self.policy.allow:
                        self.policy.allow.append(rule)
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
                          # 会话级硬上限：bash_timeout 只是默认值，模型可以在 tool call 里显式传更大的
                          # timeout（schema 允许到 600）。无人值守运行（评测 / 自动化）时一条跑不完的
                          # 命令就能吃掉 600 秒，把整轮耗时完全盖住。配了 shell_max_timeout 就封顶。
                          max_timeout=int(self.config.data.get("shell_max_timeout") or 0),
                          extra={"session": self, "emit_event": emit, "call_id": cid, "config": self.config,
                                 "home": self.home,
                                 # 指挥会话（D6-1）：窄接口与调用者身份都由所属 Thread 注入。
                                 # 普通会话这里是 None，工具据此明确说"没有启用指挥能力"，
                                 # 而不是凭空猜一个身份去改别人的会话。
                                 "orchestrator": self.orchestrator, "thread_id": self.thread_id})
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
            ev = ApprovalRequest(cid, tool.name, title, detail, d.prefix, fut, force=d.force)
            # 自动审批建议态（T10-c）：只往事件上挂一段说明，**不改 d.action**。
            # 这一层永远不能放行；放行要用户自己打开开关，且受 deny/force/只读约束。
            _attach_suggestion(ev, decision=d, tool=tool.name, args=args,
                               mode=self.policy.mode, config=self.config.data)
            emit(ev)
            return await fut

    # ------------------------------------------------------------ 侧路请求（不碰主会话）
    async def side_complete(self, system: str, user: str, role: str | None = None) -> str:
        """侧路请求（不进历史）。role：按用途取模型（title / compact / commit），花费按那个模型的单价记。
        不带主会话的推理强度（换了模型的参数不一定被支持）。"""
        spec = self.role_spec(role)
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        out = []
        comp = None
        client = await self.ensure_role_client(spec)
        async for kind, val in client.stream(msgs, None):
            if kind == "text":
                out.append(val)
            elif kind == "done":
                comp = val
        if comp is not None:
            await self._charge(comp.usage, spec)
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
        return await self.side_complete(load_prompt("commit.md"), prompt, role="commit"), stage_all

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
        from ee.core import prune as _prune
        # Wave 6 / T20：影子观察——只记录「本可以怎么做」，不执行任何改写。
        # 开关默认关（context_policy.settings），关闭时 observe() 返回 None、零副作用。
        try:
            from ee.core import context_policy as _policy
            _policy.observe(self)
        except Exception:  # noqa: BLE001  影子记录失败绝不能影响真实回合
            pass
        await _prune.maybe_prune(self, emit)      # D5-10：先成批修剪旧工具输出（阈值低于压缩）
        if self.needs_compaction() and any(m.get("_kind") != "agents" for m in self.history):
            await self._compact(emit, "auto")

    async def compact(self, emit=None) -> dict:
        """手动压缩（/compact，供 server 调用）。不能在回合运行中调用。

        返回 {ok, message, tokens_before, tokens_after}；成功时 emit 一个 Compacted 事件。
        """
        emit = emit or (lambda ev: None)
        if self.running or self._history_lock.locked():
            return {"ok": False, "message": "正在运行中，等这一回合结束再压缩。", "tokens_before": 0, "tokens_after": 0}
        async with self._history_lock:              # 压缩期间到来的回合会排队等待，而不是并行改写历史
            self.running = True
            try:
                return await self._compact(emit, "manual")
            finally:
                self.running = False

    async def _summarize(self, emit, prev: tuple[int, str] | None = None) -> tuple[str, dict | None, object]:
        """主路：同一前缀 + 当前历史 + 压缩指令（同模型时复用缓存）。不调工具。
        配了 roles.compact 时交给那个模型（历史放不进它的窗口就抛错，走侧路精简记录）。
        prev = 上次摘要 (下标, 正文)：增量压缩（A9）。当前历史里本来就只剩「保留的原话 + 旧摘要 + 之后的新内容」，
        同模型时照发当前历史保住缓存，指令改成在旧摘要上合并新内容；换了模型（缓存不共享）时只发固定消息 + 旧摘要起的部分。"""
        prefix = self._ensure_prefix(emit)
        spec = self.role_spec("compact")
        hist = self.history
        if prev is not None and spec.alias != self.spec.alias:
            hist = [m for m in self.history[:prev[0]] if m.get("_kind") == "agents"] + self.history[prev[0]:]
        instr = C.compact_update_prompt() if prev is not None else C.compact_prompt()
        messages = ([prefix.system] + T.wire_messages(hist, spec.profile) +
                    [{"role": "user", "content": instr}])
        if spec.alias != self.spec.alias and C.estimate_messages(messages) > 0.85 * spec.profile.context_window:
            raise RuntimeError(f"历史放不进压缩模型 {spec.alias} 的上下文窗口")
        text: list[str] = []
        comp = None
        client = await self.ensure_role_client(spec)
        async for kind, val in client.stream(messages, prefix.tools):
            if kind == "text":
                text.append(val)
            elif kind == "notice":
                emit(val if isinstance(val, Notice) else Notice(val, "warn"))
            elif kind == "done":
                comp = val
        content = (comp.content if comp is not None else "".join(text)).strip()
        return content, (comp.usage if comp is not None else None), spec

    async def _summarize_side(self, prev: tuple[int, str] | None = None) -> str:
        """兜底：历史渲染成文字（超长掐头留尾）走侧路请求。有上次摘要时只渲染之后的新内容 + <previous-summary>。"""
        budget = max(4000, int(min(self.profile.context_window, self.role_spec("compact").profile.context_window) * 0.5))
        if prev is not None:
            body = C.incremental_side_request(prev[1], self.history[prev[0] + 1:], budget)
            return await self.side_complete(C.compact_update_prompt(), body, role="compact")
        transcript = C.render_transcript(self.history, budget)
        return await self.side_complete(C.compact_prompt(), "以下是需要压缩的对话记录：\n\n" + transcript,
                                        role="compact")

    async def _compact(self, emit, reason: str) -> dict:
        real = [m for m in self.history if m.get("_kind") != "agents"]
        before = self.context_tokens()
        if not real:
            return {"ok": False, "message": "没有可压缩的内容。", "tokens_before": before, "tokens_after": before}
        prev = C.last_summary(self.history)            # A9：已有上次摘要 → 只合并之后的新内容
        if prev is not None and not C.has_new_since(self.history, prev[0]):
            return {"ok": False, "message": "上次压缩之后没有新的内容，不需要再压缩。",
                    "tokens_before": before, "tokens_after": before}
        incremental = prev is not None
        summary, usage, used_spec = "", None, self.spec
        try:
            summary, usage, used_spec = await self._summarize(emit, prev)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            emit(Notice(f"压缩请求失败（{_friendly_error(e)}），改用精简记录重试。", "warn"))
        if usage is not None:
            await self._charge(usage, used_spec)
        if not summary:
            try:
                summary = await self._summarize_side(prev)
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
        appendix = self.updates.build(self._env_parts())
        new = C.build_history(self.history, summary, appendix, int(cfg.get("keep_user_messages", 3)),
                              int(cfg.get("keep_user_tokens", 20000)))
        self.history = new
        self.store.append("reset", {"why": "compact"})
        for m in new:
            self.store.message(m)
        self.store.append("compact", {"reason": reason, "tokens_before": before, "incremental": incremental})
        self.turn_starts = []          # 压缩前的回合无法再回退
        self._invalidate_read_refs("compact")
        prefix = self.prefix or self.current_prefix()
        after = (C.estimate_tokens(prefix.system["content"]) + C.estimate_tokens(json.dumps(prefix.tools)) +
                 C.estimate_messages(new))
        self.stats.last_prompt, self.stats.last_completion = after, 0
        self.stats.compacted_tokens += max(0, before - after)
        self._fresh_usage = False
        self.compactions += 1
        text = f"已{'增量' if incremental else ''}压缩上下文（约 {before:,} → {after:,} tokens）"
        emit(Compacted(text, "info", reason=reason, tokens_before=before, tokens_after=after, summary=summary,
                       incremental=incremental))
        return {"ok": True, "message": text, "tokens_before": before, "tokens_after": after, "incremental": incremental}


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


# 扩展名 → LSP languageId。只覆盖 MVP 支持的语言（python / typescript）。
_LANG_BY_SUFFIX = {
    ".py": "python", ".pyi": "python",
    ".ts": "typescript", ".tsx": "typescriptreact",
    ".js": "javascript", ".jsx": "javascriptreact",
    ".json": "json",
}


def _attach_suggestion(ev, *, decision=None, tool: str = "", args: dict | None = None,
                       mode: str = "default", config: dict | None = None):
    """把自动审批的**建议**挂到 ApprovalRequest 上（T10-c）。

    三条纪律：
    - 只写 `ev.suggestion`，**不改任何决策**；审批照常等用户。
    - 算建议出错只记日志，返回原事件——建议是辅助信息，不能把审批搞崩。
    - 未开启时不产生合格建议（默认关闭）。

    `decision` 缺省时从事件自身字段重建：这样单测可以直接构造事件调用。
    """
    try:
        from ee.core.autoapprove import suggest

        if decision is None:
            from ee.core.permissions import Decision

            decision = Decision("ask", "", getattr(ev, "prefix", "") or "",
                                getattr(ev, "force", False))
        s = suggest(decision, tool=tool or getattr(ev, "tool", ""), args=args or {},
                    mode=mode, config=config)
        ev.suggestion = s.as_dict()
    except Exception as e:  # noqa: BLE001  建议失败不影响审批
        log.info("自动审批建议计算失败（忽略）：%s", e)
    return ev


def _language_of(path: Path) -> str:
    return _LANG_BY_SUFFIX.get(path.suffix.lower(), "plaintext")


# LSP 只支持 python / typescript（方案明确「不扩泛语言框架」）。
# 用根目录里最可能的入口文件判断要不要起服务器：起一个用不上的进程
# 比不起更糟——它会一直占着内存，还让设置页显示「已连接」。
_ROOT_MARKERS = (
    ("python", ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")),
    ("typescript", ("tsconfig.json", "package.json")),
)


def _lsp_language_for_root(root: Path) -> str:
    """按根目录的特征文件猜一种语言；都没有时按 python。"""
    for lang, names in _ROOT_MARKERS:
        if any((root / n).exists() for n in names):
            return lang
    return "python"


def _read_project_lsp(root: Path) -> dict:
    """读项目级 LSP 配置（`.ee/lsp.toml`）。

    项目配置**只能关闭或收紧**——`merge_config` 负责丢弃 `command`。
    文件不存在、解析失败都返回空字典，不影响会话。
    """
    p = root / ".ee" / "lsp.toml"
    if not p.is_file():
        return {}
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover  3.11+
        return {}
    with p.open("rb") as f:
        data = tomllib.load(f)
    return data if isinstance(data, dict) else {}
