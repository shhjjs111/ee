# 长对话缓存命中率钉在 99%：目标、实测与改动方案

**目标（用户明确）**：长对话缓存命中率稳定在 **99%** 并维持，**同时模型不变傻**。

**核心结论（本轮用真实会话数据推翻了我自己的初版判断）**：

> ee **在「正常回合」已经是 99.29%**，不是「最多 98%」。
> 全部命中率被拉低到 96.5%，是因为 **29% 的回合（几乎全是压缩后的回合）掉到 93–95%**，
> 而它们占了 59% 的 prompt、贡献了 **88% 的 miss**。
>
> 所以任务不是「把通用命中率从 98 抠到 99」，而是 **让压缩不再产生断点**。
> 压缩次数 11 次/5 会话 → 断点 11 个 → 每个断点烧掉 5–20 万 token。把这条打掉，99% 是算术必然。

---

## 1. 99% 的算术：先看 ee 现在到底差在哪

定义 `命中率 = 1 - Σmiss / Σprompt`，单请求 miss 由三部分组成：

| 组成 | 例子 | 能否省 |
|---|---|---|
| **A 本步新增内容** | 上一步 assistant 输出、工具结果、新 user 消息、`<context-update>` | **不能**，模型必须看到 |
| **B 断点重算** | 压缩重建历史、修剪改历史中段、换模型重冻前缀、清旧推理晚一步 | 能，**且是唯一的战场** |
| **C 侧路请求** | 标题生成、压缩摘要、commit（每次都是全新的 system+user 两条消息，命中恒 0） | 省不了钱，但不该算进主对话口径 |

`99% ⟺ 单请求 miss ≤ prompt 的 1%`。prompt 100k 时 ≤ 1000，200k 时 ≤ 2000。

---

## 2. 实测账本：ee 的真实表现（本轮从 `~/.ee/sessions` 逐回合拆）

从 6 个真实会话的 `turn_end` 快照做相邻差分（每回合的 `Δprompt / Δhit` 就是该回合的真实账）：

### 2.1 全局

| 指标 | 数值 |
|---|---|
| 总回合数 | 38 |
| 命中率 ≥ 99% 的回合 | **11 个（29%）**，占 25.2M / 83.2M prompt（30%） |
| 全部回合加权命中率 | **96.54%** |
| 正常回合 miss/req | **469–579** |
| 断点回合 miss/req | **3,070–6,665**（差一个数量级） |
| **断点回合占总 prompt** | **59.0%** |
| **断点回合贡献的 miss** | **88.0%**（2.54M / 2.88M） |

### 2.2 单会话（`20261102-205859`，最差的那个，381 请求 / 7 次压缩）

```
turn1  req=50  prompt/req= 94,605  miss/req=   550  命中率 99.42%  ← 正常
turn2  req=50  prompt/req= 79,102  miss/req=   579  命中率 99.27%  ← 正常
turn3  req=51  prompt/req= 55,148  miss/req= 3,070  命中率 94.43%  ← 断点（压缩后）
turn4  req=26  prompt/req= 58,005  miss/req=   469  命中率 99.19%  ← 正常
turn6  req=51  prompt/req= 69,679  miss/req= 4,171  命中率 94.01%  ← 断点（压缩后）
turn7  req=51  prompt/req= 67,794  miss/req= 3,809  命中率 94.38%  ← 断点（压缩后）
turn9  req=42  prompt/req= 59,739  miss/req= 6,665  命中率 88.84%  ← 断点（压缩后）
turn10 req=51  prompt/req= 59,334  miss/req= 3,727  命中率 93.72%  ← 断点（压缩后）
```

### 2.3 断点归因：时间戳对齐，确认是压缩

把该会话 JSONL 里的 `compact` / `reset` / `reasoning_strip` / `turn_end` 按 `t` 排序，
**7 次 `compact` 全部落在断点回合的起点**，且每次压缩后 `reasoning_strip` 游标立刻从 `to: 226` 跳回 `to: 7`
——这是 `_compact():1811 self._reasoning_strip_to = -1` 后重新推进的直接证据，也证明历史被整体重建了。

```
21:28:34 reset   {'why': 'compact'}
21:28:34 compact {'reason': 'auto', 'tokens_before': 109334, 'incremental': False}
21:28:37 reasoning_strip {'to': 7}      ← 游标从 226 跳回 7，历史重建
```

### 2.4 关键发现：压缩的代价不是「一次性重算」，而是**之后每步都变贵 5 倍**

这是本轮最重要的修正。压缩后 prompt 从 ~110k 掉到 ~30k，如果只是「一次性全 miss」，
50 次请求的总 miss 应该是 `30k + 50×500 ≈ 55k`，即该回合命中率 ≈ 98.3%。

**实测是 94.43%（miss/req 3,070），差了 5 倍。** 说明压缩之后**稳定状态下的每请求 miss 从 ~500 涨到了 ~2,500**。

原因（推断，有强支撑）：

- `compaction.py:145 build_history()` 只保留 `[agents] + [最近 3 条用户原话] + [摘要]`，
  **所有工具输出、read 结果、shell 输出全部丢弃**；
- 接手模型为了继续干活，必须**重新 read 那些文件**——每次 read 一个新文件就是几千 token 的**全新内容**（必然 miss）；
- 于是「重读成本」不是一次性的，而是分摊到压缩之后的几十步里。

**这直接回答了用户的两个担忧，而且它们是同一个问题的两面**：

> 命中率下降的主因是压缩后重读；而「变傻」的主因也是压缩丢了工具输出导致重读。
> **修好一个，另一个同时好转。**

---

## 3. 仪表：断点归因到底记了什么（P0-1，已落地）

原来 ee **完全没有逐请求的断点归因**：`context.py` 的 `records` 只活在内存里、从不落盘；
会话 JSONL 只有 `turn_end` 的累计快照（`server/threads.py:720`）；`breakdown.py` 只吐
`hit / hit_rate / hit_coverage` 三个数。所以第 2 节的 88% 只能靠「回合粒度 + 时间戳对齐」倒推。

现在每条**主对话**请求的 record 上多四个字段（`context.py:206 _wire_extra()`）：

| 字段 | 含义 |
|---|---|
| `wire` | 本次请求的输入规模估计（system + tools + 全部 messages），由 `note_wire()` 在 `_sample()` 发送前记下 |
| `new` | `wire - 上一次主对话的 wire`，即「这次真正新增了多少」 |
| `gap` | `prompt - hit`，服务端报的真实未命中 token |
| `break` | 判定为断点时的原因字符串（当前只有 `"compact"`） |

判定规则（`context.py:222`）：`gap > new * 1.5 + 1000` → 断点。
1000 的余量给 tokenizer 抖动和服务端块对齐：正常追加时 `gap` 只比 `new` 多几十到几百。

**为什么用估计的 wire 而不是字节数**：字节数和 token 不是线性关系（中文 / JSON / 工具 schema
差异很大），拿字节算出的「新增」会系统性偏，判定阈值就没有意义。`estimate_messages()` 是
全仓库已在用的同一套估计，和 `context_tokens()` 口径一致。

**侧路不参与**：`note_wire()` 只有主对话调（`session.py:1374`），`_wire_extra()` 里
`if role != "main" or not wire: return {}`。侧路请求和主对话没有公共前缀，拿它的 wire
去比主对话会把断点全部算错。

**断点原因怎么传下来**：`Session._break_reason` 由 `_compact()` 末尾置 `"compact"`
（`session.py:1932`），`_charge()` 里 `role == "main"` 时用 `_take_break()` **一次性取走**
（`session.py:456` / `:468`）——只归因给紧随其后的那一条主请求，不会重复记账。

> 原设计里的 `set_model()` / `prune.apply()` / `updates.reset()` 三个设置点**没有落地**。
> 换模型会重建 `FrozenPrefix`、修剪会改历史中段，它们确实会断缓存，但这两条路径的断点
> 已经能被 `gap > new*1.5+1000` 自动识别（只是原因为 `"unknown"`）。先只有 `"compact"`
> 一个确定原因，是因为它是实测贡献 88% miss 的那个。

**record 已经落盘**（P0-3，见 §3.1）：每笔请求写一行 `kind="usage"` 进会话 JSONL，
重启后能回放整条命中率曲线。

---

### 3.1 逐请求落盘（P0-3，已落地）

**为什么必须落盘**：断点归因只在内存里，进程一退就全没了。命中所率优化能靠的是
「这个会话里究竟哪几次请求漏了多少、每次漏之前刚发生了什么」——这只能从磁盘上的原始行算。

**落点**：`Session._charge()`（`session.py:456`，全仓唯一记账入口）在 `stats.add()` 之后
把这次新追加的 record 逐条写进 `store`。`add()` 保证至少追加一条（缺 usage 也落
`usage_missing` 记录），所以 `records[before:]` 不会漏也不会重。

**写什么**：`SessionStore.usage(record)` → `append("usage", {"usage": record})`。
一行长这样（`t` 由 `append()` 统一加）：

```json
{"t": 1762…, "kind": "usage", "usage": {"prompt": 61234, "completion": 412, "hit": 60890,
 "reasoning": 180, "cost": 0.135, "currency": "¥", "model": "deepseek-chat",
 "turn": 7, "role": "main", "wire": 61520, "new": 812, "gap": 344}}
```

**读什么**：`SessionStore.usage_records(path)`（`store.py:125`）按写入顺序返回 record 列表。
不做成 `load()` 的字段：`load()` 是恢复会话的热路径（每次打开会话都走），账本只有诊断才读，
按需扫一遍比让每次恢复都多解析一整批记录划算。
`load_messages()` **不用改**：它只认 message/reset/rewind/prune，其它 kind 静默跳过——
所以落盘账本天然不会污染历史重建（`test_usage_does_not_affect_history_rebuild` 守着这条）。
老会话没有 usage 行时返回空列表，不报错（`test_old_session_without_usage_lines_reads_fine`）。

**子代理怎么办**：子代理 `persist=False`（`subagent.py:376`），它自己的 `_charge()` 落不了盘。
所以 `_merge_usage`（`subagent.py:596`）在把 record 并进父会话的同时代写一行，`role` 保持
改写成 `subagent` ——这样账本里的总额和界面上一致，而 `main_hit_rate` 仍然只算主对话。

**不动 `turn_end` 的累计快照**：它是跨重启用「这个会话已花多少」，不是诊断数据。
在 `_compact()` 那段全历史重建上挂钩子会更糟（一次压缩会成倍放大行数）。

### 3.2 账本要有人读：`ee usage`（P0-4，已落地）

只有落盘还不够——没有读它的入口，验收就只能靠每次手写临时脚本，那种东西不会留在仓库，
也就没人在跑。所以配了两层：

| 层 | 位置 | 干什么 |
|---|---|---|
| 纯计算 | `core/cache_report.py:36 summarize(rows)` | 输入一批 record，输出命中率 / 覆盖率 / 断点 / 压缩次数 / 正常回合未命中分位数 / 按 prompt 规模分桶 |
| 生产入口 | `cli.py:195 _cmd_usage`（`ee usage`） | 读会话 JSONL 调上面那层；`--all` 汇总、`--json` 给脚本 |

`ee usage <会话id>` 人读输出长这样（演示数据）：

```
20261103-011500-demo01
  请求 10 次（缺用量 0 次）
  主对话缓存命中 87.95% · 覆盖 100.00% 输入 · 9 次请求
  断点 1 次 / 压缩 1 次
  正常回合未命中：中位 1400 · p90 1600 · 最大 1700 token
    40–80k     87.95% · 9 次
  其它请求：compact 1 次
```

三个刻意的口径选择：

1. **没有数据时给 `None` 而不是 0**。老会话（账本上线前录的）会被明确告知「没有逐请求账本」
   并以退出码 1 结束，而不是报一个 0% 让人以为命中率崩了。
2. **命中率与覆盖率分开给**。命中率的分母只含「服务端真的报了 hit」的请求；覆盖率说明这部分
   占总输入多少。只看前者会把「只有 3 次请求报了 hit 且全中」读成 100%。
3. **断点数与压缩次数分开给**，并直接给出对照结论：断点多于压缩，就是「有别的东西在改写
   前缀」（换模型 / 修剪 / 老版本白压），这是排查时最有用的一个信号。

分位数用**最近秩**而不是线性插值：插值会编出账本里不存在的 token 数，看着精确其实是假的。

---

## 4. 口径：主对话命中率单算（P0-2，已落地）

**问题**：`_charge()` 是全仓库唯一记账入口（4 个调用点：`_sample`、`_keep_partial`、
`side_complete`、`_compact`）。标题生成、commit、压缩摘要的 usage 全部汇进同一个
`UsageStats`，而这些请求每次都是两三条全新消息、**命中恒 0**。

一个 381 请求的会话里混进 7 次压缩摘要（每次 prompt 100k、命中 0），凭空拉低约 0.7 个百分点，
还不算标题和 commit。

**改法**：

- `UsageStats.add(..., role="main", break_reason=None)`、`add_record()` 里每条 record 带 `role`。
- `_charge(self, usage, spec, role="main")` 一路传下去；`side_complete()` → `role or "side"`；
  `_compact()` → `"compact"`；`title` / `commit` 各自传自己的 role。
- `UsageRollup.roles()`（`usage.py:87`）按 role 分组；`totals_v2(by_role=...)` 输出
  `by_role` / `main_requests` / `main_hit_rate` / `main_hit_coverage` / `main_hit_known_prompt`
  （`usage.py:111 totals_v2()`）。
  **旧字段 `hit_rate` / `hit_coverage` 全部保留**，前端优先读 `main_*`，老后端没有时自动回落。
- 子代理并入主会话时把 `role=main` 改写成 `role=subagent`（`subagent.py:603 _merge_usage`），
  否则子代理的请求会污染主对话口径（`subagent.py:607`）。缺 `role` 的老记录算 `main`
  （改动前唯一存在的来源）。

**这不是「把数字变好看」**：opencode 更彻底（`session/usage.ts` 的 `input` 只算未命中的输入，
cache read/write 单列），ee 用 `by_role` 达到同样效果且不动既有字段，也不改历史数据。

**前端**：`lib/context.ts:53 cacheLine()` 在有 `main_hit_rate` 时用「主对话缓存命中 X%」，
否则回落旧文案「服务端缓存命中」；`Items.tsx:801` 的回合 footer 同样优先 `main_*`。

---

## 5. 真正抬命中率的两处改动（P1-1，已落地）

### 5.1 压缩时保留最近的原始往来

**改之前**：`build_history()` 把历史整体重建为 `[AGENTS] + [最近 3 条用户原话] + [摘要]`，
**所有工具输出消失**。于是 ①压缩那一刻前缀全断；②之后模型反复 read 同一个文件，
每步都在产生新的 miss（§2.4）。

**改法**：新增 `retained_suffix()`（`compaction.py:169`）+
`build_history(keep_tools=, keep_tokens=, floor=)`（`compaction.py:227`）。新历史顺序：

```
AGENTS → 较早的用户原话 → 摘要（带环境附录） → 最近这段原始往来（连续后缀）
```

- **必须是连续后缀**：试过「逐条挑所有工具消息」，会把较早的 assistant 排到较新的 user 后面，
  时序错乱，模型读到「先看到新问题、再看到旧回答」。连续后缀天然保序，也和 Codex
  `compact.rs:362-371`「Trim from the beginning to preserve cache」一致。
- **预算二者先到**：`RETENTION_TOKENS = 64_000`（照 Codex 的 `RETAINED_MESSAGE_TOKEN_BUDGET`，
  实际定义在 `compact_remote_v2.rs`）+ `RETENTION_COUNT = 20`。
- **调用侧 `_retain_budget()`**（`session.py:1854`）再叠「窗口的一半」上限，
  防止 32k/64k 小窗口模型上 64k 保留段把新历史顶出窗口。
- **孤儿清理**：截断点落在工具往来中间时，开头的孤立 `tool` 要 pop、`assistant.tool_calls`
  里结果被切掉的调用要删（否则请求 400）。
- `DEFAULTS["compact"]["keep_tools"] = 16`（`config.py:50`），0 = 退回旧行为。

试过但删掉的方案：`RETAIN_RATIO = 0.35`（保留段 ≤ 压缩前 35%）——会让 1 万 token 的小会话
压缩后一条往来都不剩；`RETAIN_MIN_TOKENS = 16_000` 下限——会让增量压缩把已概括的原文捞回来。

### 5.2 修掉 `has_new_since()` 的「白压」bug（本轮最有价值的实证发现）

**症状**：几乎每次 `_compact()` 都是白压一遍，每次作废整段前缀缓存。

**根因**：增量压缩靠 `has_new_since(history, idx)`（`compaction.py:148`）判断「上次摘要之后
有没有新内容」。改之前它的实现就是「摘要之后有东西就算新内容」。而 5.1 之后压缩后的历史是
`[... 摘要, 保留段]`——**保留段本来就排在摘要后面**。于是刚压完的会话立刻被判成「有新内容」，
下一次 `_compact()` 走增量路，把保留段重新概括、重新截一段，历史字节全变 → 缓存全断。

**修法**：压缩时把当时的长度记在摘要消息的 `_retained_to` 上（`session.py:1927-1929`），
`has_new_since()` 改走 `after_summary()`（`compaction.py:134`）：边界 = `_retained_to`，
没有这个标记（老会话 / 摘要后面真的被追加过）时退回 `idx + 1`。

```python
def after_summary(history: list[dict], idx: int) -> int:
    m = history[idx] if 0 <= idx < len(history) else {}
    end = m.get("_retained_to")
    if not isinstance(end, int) or end < idx + 1:
        end = idx + 1
    return min(end, len(history))
```

**同一个 `floor` 还修了第二个 bug**：增量压缩的 `retained_suffix()` 原来没有下界，
会把上次摘要之前的原文整段捞回来（摘要之前的内容已经被概括过，再带回来等于没压）。
`_compact()` 现在传 `floor=after_summary(history, prev[0])`，`_summarize()` /
`_summarize_side()` 也一起用 `after_summary()` 定位「要概括的新内容」。

**为什么这条最值钱**：它不是「优化」，是一个**每次压缩都白烧一整段前缀**的确定损失。
修的代价约 30 行，收益覆盖 §2.1 里 59% 的 prompt。

### 5.3 其余三条小改动

| 改动 | 位置 | 说明 |
|---|---|---|
| 压缩不再 `updates.reset()` | `session.py:1912` | system/tools/env 各块都没变，压缩只换历史。补发全量环境块是纯白送，还会把摘要消息的字节推后 |
| 只在回合开始时压缩 | `session.py:1791` | `_maybe_auto_compact()` 在 `_turn_running` 期间直接返回，只有 `context_tokens() > 0.95 × window` 才立刻压（保命优先）。中途压 = 把本回合已建立的前缀缓存全部作废 |
| 压缩水位按命中率自适应 | `session.py:1751 _watermark_factor()` | 最近 10 次主对话命中率 ≥0.99 → ×1.2；<0.98 → ×0.9；否则 1.0。夹在 `[0.8, 1.35]`，且永远 `min(.., 0.9 × window)`。样本 < 5 次不动 |

水位自适应的口径说明：系数由**运行时实测反馈**决定，不写进 `~/.ee/config.toml`，
对所有用户、所有 provider 都生效；`compact.max_tokens` 仍是用户显式可配的上限。

### 5.4 `prompt_cache_key` / TTL（P1-3，条件启用）

- `openai_chat.py` 的 `ChatClient(..., session_id=)` + `build_params()` 末尾 `_apply_cache_key(p)`。
- `Profile` 加 `supports_prompt_cache_key: bool = False` / `prompt_cache_ttl: str = ""`；
  只有 `profiles/gpt.toml` 打开。**deepseek 不打开**——它走隐式前缀缓存、按字节匹配，
  这个 key 对它无效；用户的 `~/.ee/config.toml` 也确认主用 deepseek 且不带任何 header。
- 400 降级链复用：`transforms.py:unsupported_param()` 白名单加了
  `prompt_cache_key` / `prompt_cache_retention`，`openai_chat.py` 的 400 降级会自动
  写 `quirks.json` 永久关闭。**不用升级 openai SDK**（3.39.0 的 `Completions.create`
  签名里已有这两个参数，实测确认）。
- 顺带修了一个真 bug：`_apply_cache_key()` 判 `no_prompt_cache_ttl`，而 400 降级链写的
  flag 名是 `no_prompt_ttl`，两边对不上 → 降级了也照样发。

**对 99% 的贡献：对 DeepSeek 为零**。对 OpenAI 系与中转网关有实质作用（同会话粘到同一后端，
避免网关轮询打散缓存）。**不要把它当主力方案**——§1 的算术决定了它不是。

---

## 6. 明确不做：工具 schema 排序 / 稳定 id

Codex 在 `client.rs:1052-1096` 用 `Uuid::new_v5(thread_id, tools_json)` 给 tools 和
base instructions 生成稳定 id，因为它每次请求都重建前缀 item。**ee 不是**：
`build_tools()`（`ee/tools/__init__.py:32`）是硬编码顺序，`FrozenPrefix.freeze()` 之后
`_ensure_prefix()` 只校验不重写，重启还从 `store.append("prefix", ...)` 逐字节恢复。
**ee 在这点上比 Codex 更严格，没有改进空间。**

Codex 的「压缩预计算」（opencode `compaction.ts:325-335`「The summary is held, not applied」）
也没做：它省的是压缩那一刻的一次首包，但要多一份后台请求、多一套新鲜度校验。
`has_new_since()` 修好之后压缩频率本身已经降下来，收益不足以抵消复杂度。

---

## 7. 「压缩/修剪会不会变傻」——三个可测指标

### 先说结论

- **修剪（prune）不变傻，且默认已关**（`prune.DEFAULTS["max_tokens"] = 0`，实测让成本高 10.5%）。
  elide（大字段占位）与工具输出截断是「追加时」做的减法，不产生断点，实测配对任务
  12/12 正确、步数中位 6 vs 7（更少）。
- **压缩是唯一真实损失面**，而且是**双重损失**：既丢信息（摘要覆盖不足），又丢工具输出（导致重读）。
  5.1 的保留段同时把两个损失都减小——这是它优先级最高的原因。
  5.2 的白压 bug 则是纯损失、零收益。

### 三个指标

1. **摘要标识符覆盖率**：压缩前历史里的函数名/路径/命令/报错，在摘要里出现的比例。
   基线 19%（roadmap L239-282），目标 ≥ 35%。可离线跑（正则抽取，不需模型判定）。
2. **压缩后 3 轮内「重复读同一文件」次数**：**核心指标**，直接量化 §2.4 的重读损失。
   目标 ≤ 压缩前水平。
3. **配对任务步数中位数**：同一任务，压缩前 vs 压缩后继续，步数差 ≤ +1。

### 两处 prompt 改动（已落地）

- `ee/ee/prompts/compact.md`：逐字保留项补上 **`URL`**（原样抄完整链接，别只写标题）；
  末尾新增「## 冲突时怎么办」——摘要只是过去某一刻的快照，和对话说法不一致时
  **一律以更新的对话为准**，不要为了让摘要前后一致把过时结论继续抄下去。
- `ee/ee/prompts/compact_update.md`：同样补 URL，并补「上次摘要为底稿、新对话是事实」。
- 空摘要重试一次（`session.py:1885-1904`）：主路空 → 再试一次主路 → 仍空才走侧路。
  实测 6 轮里出现 1 次空摘要，第一次就放弃太亏。

---

## 8. 验收标准（可执行的断言）

```python
# 主对话口径（P0-2 落地后才有这些字段）
t = session.stats.totals()
assert t["main_hit_rate"] >= 0.99,      "长对话命中率没到 99%"
assert t["main_hit_coverage"] == 1.0,   "有请求没报 hit 字段，数字不可信"
assert t["main_requests"] > 0

# 断点预算（P0-1 落地后才有这些字段）
main = [r for r in session.stats.records if (r.get("role") or "main") == "main"]
breaks = [r for r in main if r.get("break")]
assert len(breaks) <= session.compactions,   "每个压缩最多产生一个断点，不能更多"
normal = sorted(r["gap"] for r in main if "gap" in r and not r.get("break"))
assert normal[len(normal) // 2] <= 1000,     "正常回合的未命中中位数该在几百量级"

# 不变傻
assert session.compactions <= 基线压缩次数            # 压缩次数不能变多
assert reread_within_3_turns_after_compact <= 基线    # 压缩后重读没减少
assert compact_summary_identifier_coverage >= 0.30
assert paired_task_steps_median <= 基线 + 1

# 成本不能倒退（roadmap D 配置 = ¥11.337 / 5 会话）
assert total_cost <= 11.337 * 1.05
```

**注意**：`records` 不落盘，上面这段只能在**会话进程存活期间**跑（或在测试里构造）。
跨重启回放要先把逐请求记录写进会话 JSONL——这是尚未做的一步。

**落地顺序**：P0-1 → P0-2（只加观测，不改行为，风险为零）→ **P1-1**（唯一真正抬命中率的改动）
→ P1-2（水位自适应）→ P1-3（按 provider 收益决定）。

---

## 9. 与三家对比（只列与 99% 目标直接相关的）

| | ee 改之前 | ee 改之后 | Codex | opencode | Claude Code |
|---|---|---|---|---|---|
| 缓存键 | 不发 | 按 profile 开关 | per-session `prompt_cache_key` | `affinity` = `parentID ?? fork?.sessionID ?? id` | 无（靠字节） |
| 压缩后工具输出 | **全丢** | 保留最近 64k / 20 条 | 保留至 64k token | 摘要包成一条 user 消息 | 接受失效 |
| 压缩时机 | 每步之前都可能 | 回合开始；>95% 窗口才中途压 | 回合前 | 85% 预计算、只存不用 | — |
| 「压了等于没压」 | **每次都发生** | 已修（`_retained_to`） | — | — | — |
| 断点归因 | 无 | record 上 `wire/new/gap/break` | `cached_input_tokens` 指标 | 无（但 `input` 口径干净） | 有（热不热 + miss 原因） |
| 命中率口径 | 全部请求混算 | `main_hit_rate` 单算 | 主/侧不分 | cache read/write 单列 | 主对话 / 子代理分开 |

**ee 真正落后的是「压缩后工具输出 + 白压 bug + 看不见问题」三件**，不是缺某个开关。

---

## 10. 验证与测试

| 测试文件 | 用例数 | 守什么 |
|---|---|---|
| `test_cache_breaks.py`（新） | 3 | 端到端接线：正常回合 `gap`≈`new` 不标断点；压缩后下一条主请求标 `break="compact"`；压缩请求本身 `role="compact"` 不算主对话 |
| `test_compact_retention.py`（新） | 9 | 保留段：连续后缀保序、预算截断不留孤儿、`tool_calls` 孤儿清理、摘要排在保留段之前、`keep_tools=0` 退回旧行为、不重复最近的用户消息、`has_new_since` 忽略保留段、无 `_retained_to` 时退回下一条、`floor` 生效 |
| `test_cache_attribution.py` | 9 | 口径：侧路/标题/子代理不拉低 `main_hit_rate`；`recent_main_hit_rate` 忽略侧路；`gap≈new` 不断点、`gap>>new` 断点；侧路永不产生断点 record |
| `test_prompt_cache_key.py`（新） | 8 | 默认不发、profile 允许才发、无 session_id 不发、TTL 只在配置时发、记住 400 拒绝、`unsupported_param()` 认这两个名字、Session 传自己的 id、fork 子代理继承父 key |
| `test_compaction.py` | 改 5 处 | `test_no_compact_mid_turn`：回合中途不压；`test_compact_when_urgent_mid_turn`：>95% 窗口立刻压；摘要下标按 `kinds.index("summary")` 定位 |
| `test_prune.py` | 加 1 | `test_prune_still_works_on_retained_tail`：保留段里的旧工具输出照样该被修剪 |
| `test_compaction_incremental.py` | 改 1 | 这组守「增量摘要不重读旧内容」，与保留段正交，`keep_tools=0` 关掉保留段 |
| `test_usage_persist.py`（新） | 7 | 逐请求落盘：每请求一条带 wire/new/gap/turn；落盘与内存 `records` 逐字段一致；侧路/压缩保留 role；缺 usage 也写；账本不混进历史；老会话无账本读取正常；子代理由父会话代写 |
| `test_cache_report.py`（新） | 8 | 诊断纯计算：空账本给 None 不给 0；侧路不进主对话分母；覆盖率与命中率分开；断点/压缩分开计；断点回合不混进正常分布；按 prompt 规模分桶（含 80k/150k 边界）；分位数用最近秩；无 role 的老记录算 main |
| `test_usage_cli.py`（新） | 7 | `ee usage` 入口：子命令已注册；人读输出含命中率与断点对照；`--json` 字段够写验收断言；`--all` 汇总多会话；老会话明确报「没有账本」；会话不存在报错；坏文件不影响汇总 |

实跑结果（本轮最终）：
- 本轮直接相关的 8 个文件（`test_usage_persist` / `test_usage_cli` / `test_cache_report` /
  `test_usage_restore` / `test_subagent_usage` / `test_prune` / `test_session_lifecycle` /
  以及此前的 `test_cache_breaks` 等）：**全部通过**
- 前端全量：**60 files / 328 tests passed**，`npm run typecheck` 通过
- `ruff check ee/`：All checks passed
- `./scripts/verify.sh --quick`：全部通过（92.7s）

---

## 11. 一句话总结

> ee 的正常回合已经是 **99.3%**，不是 98%。拖到 96.5% 的是**压缩**：它占 59% 的 prompt、
> 贡献 88% 的 miss，而且代价不是一次性重算，是**压缩后每步多花 5 倍**（工具输出被丢光，
> 模型反复重读文件）。
>
> 本轮修的三件事按价值排序：**①`has_new_since` 白压 bug**（每次压缩都白烧一整段前缀，
> 约 30 行，覆盖 59% 的 prompt）；**②压缩保留最近 64k 原始往来**（同时治命中率和变傻）；
> **③主对话口径单算 + 逐请求断点归因**（不改行为，但从此可以验收而不是靠感觉）。
>
> 另外两块是「让结论能被检验」而不是改行为：**④逐请求账本落盘**（`kind="usage"`，跨重启可回放）；
> **⑤`ee usage` 诊断入口**（命中率 / 覆盖率 / 断点 vs 压缩 / 正常回合未命中分位数 / 规模分桶）。
> 从这一版起，「99%」是可以拿真实会话的账本去核对的，不再只能靠推断。
>
> 剩下没做的：压缩预计算、摘要覆盖率与压缩后重读次数的自动化度量。
