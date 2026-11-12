"""缓存工程：冻结前缀（system + tools，sha256 校验）、<context-update> 增量附录、命中率与花费统计。

规则（对所有前缀缓存模型通用）：
1. system 与 tools 在会话首个请求时冻结，之后不增删不重排；每次请求前重算 sha256，变了发警告。
2. system 里不放日期 / git / cwd / shell 等动态信息；这些做成 <context-update seq=N> 挂在最新 user 消息尾部：
   首次全量，之后只发变化项，全没变就发自闭合标记。
3. 历史只追加：消息一旦发出字节不变（wire_messages 总是复制）。
"""
from __future__ import annotations

import hashlib
import json
import platform
import time
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from ee.core import gitinfo
from ee.core.coerce import as_float, as_int
from ee.core.usage import unbaselined_miss


def load_prompt(rel_path: str) -> str:
    f = resources.files("ee") / "prompts" / rel_path
    return f.read_text(encoding="utf-8").strip()


def builtin_base(profile=None, agent: str = "full") -> str:
    """内置基础段：agent=full 用 system/base.md，lean（精简模式）用 system/lean.md。用户的 SYSTEM.md（core/sysprompt.py）替换的就是这一段。"""
    return load_prompt("system/lean.md" if agent == "lean" else "system/base.md")


def system_prompt(profile, style_segment: str = "", custom=None, tools=None, agent: str = "full") -> str:
    """style_segment：回答风格段（core/styles.py），附在最末尾；默认风格为空，system 与没有风格时逐字节相同。
    custom：sysprompt.SystemSources（SYSTEM.md 替换基础段、APPEND_SYSTEM.md 追加、模型补丁开关）；
    None 或没有任何自定义时与原来逐字节相同。
    tools：本会话启用的工具，它们各自的使用规则（prompts/tools/*.md 的「## 规则」）去重后拼成「工具使用规则」段，
    紧跟基础段。SYSTEM.md 只替换基础段；工具规则照常按启用的工具拼装追加（工具的使用约束跟着工具走，
    自定义提示词不需要也不应该重写它们）。
    agent：full / lean，决定内置基础段用哪份。"""
    base = builtin_base(profile, agent)
    use_patch = True
    if custom is not None:
        if custom.system is not None:
            base = custom.system.text
        use_patch = custom.model_patch
    if tools:
        from ee.tools import tool_rules
        rules = tool_rules(tools)
        if rules:
            base += "\n\n# 工具使用规则\n" + "\n".join(f"- {r}" for r in rules)
    text = base + "\n"
    if use_patch:
        patch_name = getattr(profile, "prompt_patch", "default") or "default"
        try:
            patch = load_prompt(f"system/{patch_name}.md")
        except (FileNotFoundError, OSError):
            patch = load_prompt("system/default.md")
        text = base + "\n\n" + patch + "\n"
    if custom is not None and custom.append is not None:
        text += "\n" + custom.append.text + "\n"
    if style_segment:
        text += "\n" + style_segment.strip() + "\n"
    return text


def prefix_hash(system: dict, tools: list[dict]) -> str:
    blob = json.dumps({"system": system, "tools": tools}, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class FrozenPrefix:
    system: dict
    tools: list[dict]
    sha256: str

    @classmethod
    def freeze(cls, system_text: str, tools: list[dict]) -> "FrozenPrefix":
        sysmsg = {"role": "system", "content": system_text}
        tools_copy = json.loads(json.dumps(tools, ensure_ascii=False))
        return cls(sysmsg, tools_copy, prefix_hash(sysmsg, tools_copy))

    def verify(self) -> bool:
        return prefix_hash(self.system, self.tools) == self.sha256


# ------------------------------------------------------------------ 动态环境块

PLAN_NOTE = ("计划模式：现在只调研、出方案，不修改任何文件（写文件和非只读命令都会被拒绝）。\n"
             "1. 先读代码、搜索、必要时上网，把要改哪里、为什么弄清楚；需求有关键歧义时直接用文字问用户，然后结束回合。\n"
             "2. 想清楚后调用 present_plan 提交方案（markdown）：目标、要改的文件和具体改法、步骤顺序、风险和验证方法；"
             "要具体到别人照着就能做，不要写空话。\n"
             "3. 提交后本回合结束，等用户批准；用户可能提修改意见，按意见改完再次 present_plan。\n")


PLAN_NOTE_TEXT = ("计划模式：现在只调研、出方案，不修改任何文件（写文件和非只读命令都会被拒绝）。"
                  "先读代码弄清要改哪里、为什么；想清楚后直接用文字给出方案（目标、要改的文件和改法、步骤、风险和验证方法），"
                  "然后结束回合，等用户批准并切换到可写模式后再动手。\n")


def env_parts(cwd: Path, shell, git: gitinfo.GitStatus | None, gh_available: bool, mode_label: str,
              today: str | None = None, plan_tool: bool = True, desktop: bool = False) -> dict[str, str]:
    """返回 name → 内容。每个子块独立比较变化；量化到“天”，避免无谓抖动。
    plan_tool=False：没有 present_plan 工具（关掉或精简模式），计划模式改为用文字给方案。
    desktop=True：有真窗口，`screenshot` 可用——不写这一行，模型判断“要不要截图”时没法区分
    桌面会话和 CLI（实测：它会绕过截图工具去开无头浏览器，哪怕窗口就在那里）。"""
    osname = {"Windows": "Windows", "Darwin": "macOS", "Linux": "Linux"}.get(platform.system(), platform.system())
    parts = {
        "env": (f"<env>\n工作目录：{cwd}\n操作系统：{osname} {platform.release()}\n"
                f"日期：{today or time.strftime('%Y-%m-%d')}\n</env>"),
        "shell": (f"<shell>\n当前 shell：{shell.display}（{_syntax_hint(shell)}）\n</shell>" if shell else
                  "<shell>\n当前 shell：未知\n</shell>"),
        "permissions": f"<permissions>\n权限模式：{mode_label}\n{(PLAN_NOTE if plan_tool else PLAN_NOTE_TEXT) if mode_label == '计划' else ''}</permissions>",
    }
    if git is not None and git.is_repo:
        dirty = f"{git.changed} 个已跟踪文件有改动，{git.untracked} 个未跟踪文件" if git.dirty else "工作区干净"
        ab = f"（{git.ahead_behind}）" if git.ahead_behind else ""
        parts["git"] = f"<git>\n这是 git 仓库，根目录：{git.root}\n当前分支：{git.branch}{ab}\n{dirty}\n</git>"
    else:
        parts["git"] = "<git>\n当前目录不是 git 仓库\n</git>"
    if gh_available:
        parts["tools"] = ("<dev-tools>\n已安装 gh（GitHub CLI）：可用 gh pr view/list、gh issue view/list、"
                          "gh run list 等查询 PR、issue、CI；gh 的写操作（创建/评论/合并）需要用户要求并会请求批准。\n</dev-tools>")
    if desktop:
        parts["ui"] = ("<ui>\n当前在桌面端窗口里运行：改完界面可以用 screenshot 截当前窗口，"
                       "直接确认真实渲染（间距、颜色、布局）。\n</ui>")
    return parts


def _syntax_hint(shell) -> str:
    syn = getattr(shell, "syntax", "posix")
    if syn == "powershell":
        return "PowerShell 语法：用 ; 分隔命令，$env:NAME 读环境变量，路径可用 \\ 或 /"
    if syn == "cmd":
        return "cmd 语法：用 & 或 && 连接命令，%NAME% 读环境变量"
    return "POSIX shell 语法"


class ContextUpdates:
    """<context-update seq=N> 增量附录（天枢做法）。"""

    def __init__(self) -> None:
        self.seq = 0
        self.last: dict[str, str] = {}
        self.baseline_sent = False

    def reset(self) -> None:
        """压缩或换模型后调用：下一次补发全量。"""
        self.baseline_sent = False
        self.last = {}

    def build(self, parts: dict[str, str]) -> str:
        self.seq += 1
        if not self.baseline_sent:
            self.last, self.baseline_sent = dict(parts), True
            return f'<context-update seq="{self.seq}">\n' + "\n".join(parts.values()) + "\n</context-update>"
        changed = [v for k, v in parts.items() if self.last.get(k) != v]
        removed = [k for k in self.last if k not in parts]
        self.last = dict(parts)
        if not changed and not removed:
            return f'<context-update seq="{self.seq}"/>'
        body = "\n".join(changed + [f"<{k}/>" for k in removed])
        return f'<context-update seq="{self.seq}" mode="delta">\n' + body + "\n</context-update>"


# ------------------------------------------------------------------ 统计

@dataclass
class UsageStats:
    requests: int = 0
    prompt: int = 0
    completion: int = 0
    hit: int = 0
    hit_known_prompt: int = 0      # 返回了命中字段的那些请求的 prompt 合计
    reasoning: int = 0             # 推理 token（包含在 completion 里；服务端给了才有）
    reasoning_known: bool = False
    cost: float = 0.0
    cost_known: bool = True
    last_prompt: int = 0
    last_completion: int = 0
    # 上一次主对话的服务端 prompt（只由拿到了 usage 的请求更新）。用来算 server_new：
    # 服务端计数的相邻差分，比 wire 估算更接近「这次真正新增了多少」。
    last_server_prompt: int = 0
    usage_missing: int = 0
    currency: str = "¥"
    # 断点归因（P0-1）：上一次主对话请求估算的输入 token 数，以及本次请求待消费的那份。
    # 「本次比上次多出来的量」约等于「本该未命中的量」；实测未命中远超它，说明有人在中间改了历史。
    last_wire_tokens: int = 0
    _wire_pending: int = 0
    # 每次请求一条：{prompt, completion, hit, reasoning, cost, currency, model, turn, role}
    # （子代理并入主会话时按条复制，花费不重算）
    records: list = field(default_factory=list)
    by_model: dict = field(default_factory=dict)
    pruned_tokens: int = 0         # 修剪旧工具输出累计省下的 token（估计，D5-10）
    prunes: int = 0
    # 推理从一开始就不回传（D5-11）：后续被清掉的推理量。与 pruned_tokens 分开计，
    # 界面上一个是「修剪工具输出」一个是「旧推理不回传」，合计才是总节省量。
    reasoning_tokens: int = 0
    compacted_tokens: int = 0      # 压缩累计省下的 token（估计：压缩前 − 压缩后）

    def note_wire(self, tokens: int) -> None:
        """记下本次主对话请求估算的输入 token 数，供 add() 判断未命中是不是「正常新增」。

        侧路请求（标题 / commit / 压缩）不调它：它们的消息和主对话没有公共前缀，
        拿它们的 wire 大小去比主对话，会把主对话的断点全部算错。
        """
        self._wire_pending = max(0, as_int(tokens))

    def _wire_extra(self, role: str, prompt: int | None, hit: int | None, break_reason: str | None) -> dict:
        """把「本次新增了多少 / 实际未命中多少 / 是不是断点」写进 record。

        判定规则：gap（服务端报的未命中）如果远超这次真正追加的量，说明中间有东西被改写了——
        压缩重建历史、换模型换了前缀、修剪改了历史中段都会这样。1000 token 的余量是给
        tokenizer 抖动和服务端块对齐留的：正常追加时实测 gap 只比新增多几十到几百。
        """
        # 侧路请求在取槽之前就返回：它自己不参与主对话基线，但也**不能把主请求刚记下的
        # wire 取走**。标题生成是后台 task（threads.py 的 _ai_title，最长 30s），完全可能
        # 落在主请求 note_wire() 与 _charge() 之间；取走的结果不是报错，而是那条主记录
        # 静默少了 wire/new/gap/break —— 断点从此漏记，账本上什么都看不出来。
        if role != "main":
            return {}
        wire, self._wire_pending = self._wire_pending, 0
        if not wire:
            return {}
        # 没有基线（last_wire_tokens == 0）说明这是本进程里第一次主对话请求，或者刚 /new、
        # 刚从磁盘恢复会话。此时「上次发了多少」根本不知道，把整条 wire 当成新增会让 new
        # 虚高到几万，还会让断点判定 gap > new*1.5+1000 永不成立（漏报）。
        # 真实病例：会话续跑重启后第一请求 wire=82190 -> new=82190，而实际只增加了 1738。
        baseline = self.last_wire_tokens
        new = max(0, wire - baseline) if baseline > 0 else 0
        self.last_wire_tokens = wire
        extra: dict = {"wire": wire, "new": new, "baseline": baseline}
        if baseline > 0 and wire < baseline:
            # new==0 的另一种来源：历史/估算变小（取消、失败重试、上次 note_wire 没落账）。
            # 它不等于冷启动，但仍值得标出来，避免和“真的没有新增 token”混在一起。
            extra["shrink"] = True
        if baseline <= 0:
            # 不能只留下 new==0：它和“wire 估算变小”长得一模一样。显式记 cold，
            # cache_report 与压缩水位据此排除，才不会把一次重启当成“前缀不稳”。
            extra["cold"] = True
        if break_reason:
            # 已知原因始终留下。原来 baseline==0 或 gap 没超过阈值时，
            # model_switch / compact / prune 会被静默丢掉，账上只剩 unknown 或什么都没有。
            extra["cause"] = break_reason
        if hit is not None and prompt:
            gap = max(0, as_int(prompt) - as_int(hit))
            extra["gap"] = gap
            # new 是 wire 估算差；服务端 prompt 的相邻差分更准（没有 tokenizer 偏差）。
            # 有上一次主对话的服务端 prompt 时优先用它，没有才退回 wire。
            server_new = None
            if self.last_server_prompt > 0:
                server_new = max(0, as_int(prompt) - as_int(self.last_server_prompt))
                extra["server_new"] = server_new
            growth = server_new if server_new is not None else new
            # 没有基线时不断点：续跑/重启后的首请求与服务端缓存是否还热无关己，
            # 两者无法区分，此时硬判会把“进程刚起”记成 ee 自己造的断点。
            if baseline > 0 and gap > growth * 1.5 + 1000:
                extra["break"] = break_reason or self._break_kind(as_int(hit), as_int(prompt))
                if not break_reason:
                    # 归因未命中原因时最需要的一项：hit 只有很小一块，说明服务端在最前面
                    # 就匹配不上了（不是「尾巴改了」）。真实会话实测到的孤立断点就是这个形态：
                    # prompt 只比上一条多 224，hit 却从 128,384 掉到 2,560。
                    extra["prefix_hit"] = as_int(hit)
        elif break_reason:
            extra["break"] = break_reason
        return extra

    @staticmethod
    def _break_kind(hit: int, prompt: int) -> str:
        """归因不到本地原因时，按「命中到多深」给断点分类。

        **为什么能这么分**：本地改写（压缩 / 修剪 / 清推理 / 换模型 / 前缀重冻）都发生在某个
        位置之后，那个位置**之前**的前缀照样命中；只有服务端那一侧整段失配，才会出现
        「只命中 system+tools 那一小段」。真实账本：55 次 unknown 断点里 35 次的 hit 不到
        prompt 的 10%，其中 29 次干脆是 0 —— 而同一段历史的相邻请求都在 99.5% 以上。

        用 10% 而不是绝对值：system+tools 的头在不同 provider 上从几千到几万 token 不等，
        按比例才跨模型可比。代价是「尾巴被改了一点点」和「整段丢缓存」在极小的 prompt 上
        可能同形；这类样本本来也分不清，保持 unknown 更诚实。
        """
        if prompt > 0 and hit * 10 < prompt:
            return "upstream_full_miss"
        return "unknown"

    def add(self, usage: dict | None, price, model: str = "", discount: float = 1.0, turn: int | None = None,
            role: str = "main", break_reason: str | None = None) -> float | None:
        """记一次请求：price 是这次请求实际用的模型的单价，model 是它的名字（按模型细分统计）。
        discount 是错峰折扣（0<d<=1），只作用于输入+输出，缓存读取价不打折。
        turn 是第几回合（缓存未命中归因用：首回合必然冷启动）。
        role 区分请求来自哪条路（main / side / title / commit / compact）：侧路请求每次都是两条
        全新消息、命中恒 0，混进主对话会把用户看到的命中率无端拉低几个百分点。
        break_reason：调用方知道「这次请求前面刚发生了会作废缓存的事」时给出（compact / model_switch / prune）。

        缺 usage 时也要落一条记录（cost=None）：否则这次请求会从账里彻底消失，
        用户看到的总额就成了「看起来完整、其实漏了若干次」的假精确。
        """
        if not usage:
            extra = self._wire_extra(role, None, None, break_reason)
            self.requests += 1
            self.usage_missing += 1
            self.records.append({"prompt": 0, "completion": 0, "hit": None, "reasoning": None,
                                 "cost": None, "currency": getattr(price, "currency", "¥") if price is not None else "¥",
                                 "model": model, "usage_missing": True, "turn": turn, "role": role, **extra})
            self.cost_known = False
            return None
        cost = price.cost(usage["prompt"], usage["completion"], usage.get("hit"), discount) if price is not None else None
        extra = self._wire_extra(role, usage.get("prompt"), usage.get("hit"), break_reason)
        self.add_record({"prompt": usage["prompt"], "completion": usage["completion"], "hit": usage.get("hit"),
                         "reasoning": usage.get("reasoning"), "cost": cost,
                         "currency": getattr(price, "currency", "¥") if price is not None else "¥", "model": model,
                         "turn": turn, "role": role, **extra})
        return cost

    def add_record(self, r: dict) -> None:
        self.requests += 1
        p, c, h = as_int(r["prompt"]), as_int(r["completion"]), r.get("hit")
        self.prompt += p
        self.completion += c
        # 「上次上下文大小」只由主对话更新：侧路请求（title / commit / compact，以及
        # 子代理合并进来的记录）的 prompt 是另一段内容。让它覆盖，context_tokens() 会从
        # 十几万掉到几千，needs_compaction / prune.due 因此判定「还没到水位」而跳过本该
        # 发生的压缩与修剪，界面上的「剩余上下文」也跟着失真。
        # 没有 role 的老记录按主对话处理（保持本字段引入时的行为）。
        if (r.get("role") or "main") == "main":
            self.last_prompt, self.last_completion = p, c
            # usage_missing 的 prompt=0 不能把上一次的服务端基线抹掉：否则下一条请求
            # 又退回到 wire 估算，断点判定跟着抖。
            if p > 0:
                self.last_server_prompt = p
        if h is not None:
            self.hit += h
            self.hit_known_prompt += p
        if r.get("reasoning") is not None:
            self.reasoning += as_int(r["reasoning"])
            self.reasoning_known = True
        cost = r.get("cost")
        if cost is None:
            self.cost_known = False
        else:
            self.cost += cost
            self.currency = r.get("currency") or self.currency
        m = r.get("model") or ""
        if m:
            b = self.by_model.setdefault(m, {"requests": 0, "prompt": 0, "completion": 0, "reasoning": 0, "cost": 0.0,
                                              "currency": r.get("currency") or "¥", "cost_known": True})
            b["requests"] += 1
            b["prompt"] += p
            b["completion"] += c
            b["reasoning"] += as_int(r.get("reasoning"))
            if cost is None:
                b["cost_known"] = False
            else:
                b["cost"] += cost
        self.records.append(dict(r))

    def restore(self, records: list) -> int:
        """回放落盘的逐请求账本（重开 / 重启 / 被内存淘汰后重载），返回真正回放的条数。

        **为什么需要**：`records` 只活在进程里，而界面上的「主对话缓存命中」、压缩水位的
        自适应（`recent_main_hit_rate`）、上下文大小的实测值（`last_prompt`）都读它。
        不回放的话，同一条会话会给出两套数字：每回合 footer 读落盘的 `turn_end`（历史全景），
        上下文面板读 `stats`（只有重载之后的那几条）；恢复后前 5 次请求水位自适应还会因为
        样本不足直接停摆（`_watermark_factor` 返回 1.0，等于这一段白白按固定水位压缩）。

        **金额刻意不恢复**（cost 置空）：跨重启的已花金额由 `Session.spent_before`（turn_end
        的 `spent_total`）负责，两边都恢复会重复计费。回放出来的 record 只承担「过程」指标：
        命中率 / 断点归因 / 上下文大小。代价是 `totals()["cost"]` 在重载后变回未知——
        这比显示一个「只含本次进程」的偏小金额诚实。

        只在还没记过账时回放（`requests == 0`），重复调用不会把历史记两遍。
        """
        if not records or self.requests:
            return 0
        n = 0
        for r in records:
            if not isinstance(r, dict) or "prompt" not in r or "completion" not in r:
                continue                     # 坏行：宁可少一条，也不能让恢复整体失败
            rec = dict(r)
            rec["cost"] = None
            self.add_record(rec)
            if rec.get("usage_missing"):
                # add_record 不管这个计数（add() 和子代理合并各自维护它），回放要自己补：
                # 否则重载后 totals()["usage_missing"] 会变成 0，而账本里明明有缺用量的行。
                self.usage_missing += 1
            n += 1
        return n

    def spent(self, currency: str) -> float:
        """某币种的已知花费合计（预算判断用）。"""
        return sum(as_float(r["cost"]) for r in self.records
                   if r.get("cost") is not None and (r.get("currency") or "¥") == currency)

    @property
    def hit_rate(self) -> float | None:
        if self.hit_known_prompt <= 0:
            return None
        return self.hit / self.hit_known_prompt

    def recent_main_hit_rate(self, window: int = 10) -> float | None:
        """最近 window 次**正常**主对话请求的命中率（压缩水位自适应用）。

        只算 role=main：侧路请求命中恒 0，混进来会把水位往错误方向推。
        还要排除断点请求（带 `break` 那些）：断点的未命中是压缩 / 换模型本身造成的既定成本，
        不是「前缀稳不稳」的证据。把它算进来会让水位调节变成正反馈——压缩造成断点 → 命中率
        看起来低 → 水位下调 → 压缩更频繁 → 更多断点。实测真实 DeepSeek 会话的同一段历史：
        含断点 93.01%、排除断点 99.48%，对应水位 126k 与 168k，差一整次压缩。
        没有 wire 基线的冷启动（`cold`）也排除：首次请求 / 重启首发与「前缀是否稳定」无关，
        把它算进来会让一次长会话重启直接把水位压到 0.9。
        老账本（没有 `baseline`）里分不清冷启动还是整段未命中的那些同样排除：把一次
        重算当成「前缀不稳」，会把水位往下调，和断点污染是同一类错误。
        样本不足时返回 None，调用方据此「不动」。
        """
        recs = [r for r in reversed(self.records)
                if (r.get("role") or "main") == "main" and not r.get("break") and not r.get("cold")
                and not unbaselined_miss(r)][:max(1, window)]
        known = [r for r in recs if r.get("hit") is not None]
        if len(known) < 5:
            return None
        total = sum(as_int(r.get("prompt")) for r in known)
        if total <= 0:
            return None
        return sum(as_int(r["hit"]) for r in known) / total

    def context_used(self, window: int) -> float:
        if window <= 0:
            return 0.0
        return min(1.0, (self.last_prompt + self.last_completion) / window)

    def totals(self) -> dict:
        """对外口径：v2 字段（分币种、完整性、命中覆盖率）+ 兼容的单值 cost/currency。

        单值 `cost` 只在「唯一币种且金额完整」时给出；混币或缺用量一律 None。
        消费方如果只读 `cost`，会自然退化成「不知道总额」而不是「错的总览」。
        """
        from ee.core import usage as U

        roll = U.UsageRollup(self.records)
        out = roll.totals_v2(requests=self.requests, prompt=self.prompt, completion=self.completion,
                             hit=self.hit, hit_known_prompt=self.hit_known_prompt,
                             usage_missing=self.usage_missing, by_role=roll.roles())
        if self.reasoning_known:
            out["reasoning"] = self.reasoning
        if self.pruned_tokens:
            out["pruned"] = self.pruned_tokens
        if self.reasoning_tokens:
            out["reasoning_cut"] = self.reasoning_tokens
        if self.compacted_tokens:
            out["compacted"] = self.compacted_tokens
        if self.by_model:
            out["by_model"] = {k: {**v, "cost": v["cost"] if v["cost_known"] else None}
                               for k, v in self.by_model.items()}
            for v in out["by_model"].values():
                v.pop("cost_known", None)
        return out
