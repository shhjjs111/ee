# 天枢 Tianshu-harness × Waveloom：DeepSeek 优化笔记（面向 Python 小 agent）

源码：`/workspace/work/research/Tianshu-harness`（commit 21200c3，2026-10-24；Tianshu-Tui/rivet 都重定向到这个仓库），`/workspace/work/research/waveloom`（daccab5）。oh-my-tianshu 仓库 404，没拿到。CVM 理念文档是 CC BY-NC-ND 许可，代码是 Apache-2.0，所以只借思路、不照抄原文。

## 1. 前缀缓存工程（核心）

**DeepSeek 实测语义**（`docs/deepseek-wire-probe-playbook.md`）：缓存按 64 token 一块（hit 恒为 64 的整数倍）；尾部不满一块的部分总是 miss；短 prompt 不进缓存；"同 system 不同 user"马上就能命中。usage 和 finish_reason 在同一帧返回，`[DONE]` 前没有单独的 usage 块（和官方文档不符）。tools schema 也算前缀：换了 tools，命中直接归零（waveloom 实测）。

**冻结**
- system 只放会话内恒定的静态部分（`src/prompt/static.ts`）。会话开头算一次 sha256 指纹，分 system/tools/volatile 三段（`prompt/fingerprint.ts`），之后每次请求都比对，发现漂移就给出原因。
- 工具表在第一次请求发出后锁定（`engine.ts` 的 `toolCatalogPublished`，`updateTools` 直接 return）。晚到的 MCP 工具一律不加。重复注册时做字节级幂等比较。
- plan 模式不删工具，改在执行时拦截（`agent/plan-mode.ts checkPlanMode`），这样不碰 tools 前缀。
- waveloom：环境探测结果落盘缓存（`pkg/environment/probe.go`，按 PATH 哈希 + TTL），所以 system 跨会话也保持字节一致，跨会话同样能命中。

**动态信息走增量附录**（`engine.ts buildAppendixBody`，L1473）
- 动态上下文（任务契约、进度、git 状态、advisory 等）拼成 `<context-update seq=N>`，挂在**最新一条 user 消息末尾**，不写进 system。
- 子块按 name→content 存一份上次发出的版本：第一次发全量；之后只发有变化的子块（`mode="delta"`）；全都没变就发自闭合的 `<context-update seq="N"/>`。
- 在同一个 user 回合内（工具轮）直接复用 `cachedAppendix`，不重新构建。
- user 消息一旦成为历史，带附录的字节就冻结（`frozenUserMerged`），以后原样重放。
- 压缩之后 `resetAppendixBaseline()`，下一次补发全量。
- 量化放在渲染层：浮点数分三档 low/mid/high，概率按 10% 一个桶，只有跨档时字节才变。tool-history 这类块和消息历史重复，直接删掉（`docs/changelog/2026-07-06-appendix-delta-byte-stability.md`）。

```python
def build_appendix(parts: dict[str,str]) -> str:
    self.seq += 1
    if not self.baseline_sent:
        self.last, self.baseline_sent = dict(parts), True
        return f'<context-update seq="{self.seq}">\n' + "\n\n".join(parts.values()) + "\n</context-update>"
    changed = [v for k,v in parts.items() if self.last.get(k) != v]
    self.last = dict(parts)
    if not changed: return f'<context-update seq="{self.seq}"/>'
    return f'<context-update seq="{self.seq}" mode="delta">\n' + "\n\n".join(changed) + "\n</context-update>"
```

**只追加、不改写**
- 请求变换一律 copy-on-write。原地 `content +=` 遇到重入（重试、投机请求）会把内容写两遍，前缀中途翻转。
- 侧路请求（压缩、gate 判定）必须 hermetic，不能写主会话状态。
- 纠偏提示只追加到尾部（AdvisoryBus / system-reminder），绝不回写前缀。
- 孤儿 tool_call 修复一次写回会话，避免每轮重复修复。

**压缩只在用户边界做**（`agent/compact-boundary-coordinator.ts`、`compact/constants.ts`）
- 工具轮里只记一个 pending 标记，真正压缩放到下一条 user 消息到达时（turn===0）。
- DeepSeek 的阈值偏晚：watch 0.72 / compact 0.86 / reactive 0.92 / ceiling 0.95。命中率 ≥0.85 时阈值再往后推；<0.3 时提前。
- 是否延迟：`protection = hitRate × (1 − 窗口占用率)`，≥0.45 就延迟（`cache/advisor.ts`）。缓存已冷（超过 TTL）就趁机压缩。
- 压缩时保留开头 2 条消息作为锚点（`CACHE_ANCHOR_MESSAGES=2`）。
- 摘要交给 flash 生成，走独立 client；新摘要要无损合并旧摘要；会话空闲 12 秒后在后台预压缩。
- waveloom 的四级水位（`pkg/compaction/compaction.go`）：45% 截断工具输出 → 65% 清掉 reasoning、工具结果换成占位符 → 85% 用 LLM 做增量摘要（只替换游标到保护区之间的那段）→ 98% 硬停。决策表按消息索引记录，单调（snip 只能升级到 prune，不回退），三个游标只往前走。所以每条消息最多被改写一两次，改完后字节永不再变。最近 8K token 是保护区。摘要边界要对齐 assistant↔tool 配对。

**read-ref 去重**（`tools/read-file.ts` L885）：同一文件、同一区段，mtime 和 size 都没变，且内容 >2KB 时，返回 `[read-ref] path 本会话已读且未变（N 行）`，提示模型用 read_section 读具体区段。同一条 ref 已经给过一次、模型又来要，就直接返回全文（防止"回看上文"的目标已被压缩掉而陷入死循环）。自己的编辑会让对应条目立即失效。

**观测**（`api/openai-client.ts` L1488、`cache/billed-hit-rate.ts`）：`hit = usage.prompt_cache_hit_tokens ?? prompt_tokens_details.cached_tokens`，`miss = prompt_cache_miss_tokens`。主路径命中率和计费命中率要分开算：后者 = Σhit/Σinput，侧路请求也计入，否则侧路的 miss 会隐身。
- 逐条消息做签名（djb2 加长度，`prompt/message-signature.ts`），和上次请求比对，定位第一条分叉的消息。
- miss 原因分类（`prompt/cache-diagnostic.ts`）：first_turn / prefix_drift / history_changed / compaction / eviction。

## 2. DeepSeek 其他适配

- **reasoning_content**（`openai-client.ts mapWireMessages` L476）：带 tools 时，工具轮必须回传 reasoning；缺这个字段时补成 `""`。"缺字段"和"空字段"字节不同，会让缓存断掉。无 tools 时可以剥掉纯文本轮的 reasoning。网关回 400 "must be passed back" 时，只对本次请求强制保留重试，成功后记住这个行为。waveloom 的口径：带 tools 时**所有**轮次都回传。最稳的做法：一个 assistant 的 reasoning 写进历史后就不再改，只在压缩 tier2 时一次性清掉。
- **工具调用格式兜底**：content 里整段是 `{...}` 或 `[...]` 时按 JSON 解析出工具调用（`tryParseToolJsonFromContent`）。content 里出现 DSML 标记时也解析（`<｜DSML｜tool_calls><｜DSML｜invoke name=..><｜DSML｜parameter name=.. string="true">`，全角竖线和半角竖线都要认，Markdown 代码块里的不算；见 `api/dsml-tool-calls.ts`）。
- **参数修复**（`agent/repair-passes.ts`）：可选字段为 null 就删掉；数组字段收到字符串化的 JSON、`{"0":..}` 形式的对象、或裸字符串，都转成数组。
- **复读守卫**：推理流最近 128 行里 ≥95% 由 ≤3 个短句构成时，判定卡死。丢掉这段推理，附一句纠正指令后软重试（`api/reasoning-repetition.ts`）。正文里 50/100/200 字符的块连续重复时去重（`agent/dedup.ts`）。
- **flash/pro 路由**（`agent/model-tier-policy.ts`、`effort-routing.ts`、`auto-reasoning.ts`）：
  - 用 flash：scout / verify / 执行型 patcher；
  - planner 至少用 balanced；
  - 只有高风险或 council 护栏席用 pro；
  - 默认失败也不升档：换模型等于在新会话里零缓存重跑，成本可达几十倍；
  - reasoning_effort 按关键词选（off/low/…/max），常规回合降一档。

## 3. CVM

- **任务契约**（`context/task-contract.ts`）：从用户消息里用规则抽出 objective、提到的文件、约束（命中"不要/必须/禁止/never"的分句）、success criteria、status（exploring→planning→executing→verifying→ready_to_deliver）。
  - `classifyTurnMode` 区分 chat/task/followUp；"继续"这类短消息并入当前契约；follow-up 里出现新约束也会合进去。
  - 没有新信息时保持同一个对象，不让附录无谓变动。
  - 压缩后重新注入 task-anchor（每个列表最多 6 条）。
- **证据门**：EvidenceState 记录 filesModified 和每次验证的结果（`agent/evidence.ts` + `delivery-gate.ts`）。改过文件且没有一次通过的验证 → `unverified`，不能宣布完成，附带 nextAction（"跑相关测试/类型检查"）。有失败 → `failed`；环境缺失 → `blocked`。v2 版用 GREEN/YELLOW/RED 区分自己改的文件和外部文件。`evidence-gate.ts` 统计探针→决策闭环（同一 target，间隔 ≤6 轮）。
- **Doom loop**（`agent/convergence-detector.ts`）：按阶段加权（explore/plan/execute/verify/deliver）的 7 个信号：editRatio、targetNovelty、tool 熵、errorPenalty、tokenEfficiency、振荡（A→B→A 回摆率）、跨轮文本 Jaccard（中文用 2-gram）。分 L1/L2/L3 逐级升级，L3 中止。轮数阈值随窗口大小变化。argsHash = sha256(tool + 排序后的参数) 前 8 位（`tool-history-recorder.ts`）。前后两轮回复 trigram 重合 ≥60% 时提示不要复读（`hooks/dedup-guard-hook.ts`）。
- **服从漂移**：
  - sycophancy-trap：最近 5 轮里 ≥3 轮无验证地顺从，且置信度单调下降 → courage-hook 切到"义务"语气；
  - user-correction 用正则抓"不是A，是B"写进断言账本；
  - intent-anchor：自治运行 >20 轮、且距上次用户输入 >10 轮时重申启动意图，之后冷却 10 轮。
  - 所有纠偏都走 AdvisoryBus：每轮最多 3 条，按 key 去重，带 expect 谓词（如 tool_appears / verify_attempted）在之后几轮核销，统计提示是否被采纳。

## 4. 多代理

全部是"slash 命令 = 提示词模板 + 专用工具"（`workflows/ecosystem-workflows.ts`）：
- **/plan**：只读 + 写计划文件，用 plan_submit 审批后分波执行（wave-gate）。
- **/scout**：先自己内联摸底，再用一次 `delegate_batch` 按问题维度并行派 2–5 个只读 code_scout（flash）。scout 的结论只当待验证假设，主控要亲自跑命令拿 exit code。
- **/council**：`council_convene` 并行派出席位（默认天权领航、天府护栏 strong、天璇探索）。每席返回 additions/risks/alternatives/challenges，再做**确定性聚合**（`council/council-plan.ts aggregateCouncil`：同 id 冲突登记、high risk 对上 alternative 标冲突、blocking challenge 记为否决）。第二轮只在有冲突时召开；不负责执行。
- **/team**：`plan_task` 把任务水平切成正交分片（每片文件集合互不相交，同一文件用 dependsOn 串行）。每片交给一个 flash patcher 端到端完成，大家写同一个工作区，靠文件认领表防止互相覆盖。主控审 git diff，最后 deliver_task。
- 子代理 system 是主 prompt 按段落过滤出来的子集（`prompt/static-subagent.ts`），主 prompt 字节不变。
- waveloom 的 **fork 子代理**（`pkg/subagent/agent.go buildForkMessages`）：直接复制父请求的 messages，截到最后一个发起 tool_call 的 assistant 之前，tools 原样用父的（没注册的工具补 stub），末尾追加一条任务 user 消息，于是父请求的前缀全部命中。要用同一个模型才行。

## 5. 记忆

- **Stigmergy**（`context/stigmergy.ts` + `hooks/stigmergy-hook.ts`）：每条信息素是 (path, signal, strength)，信号有 fragile / well-tested / entry-point / dead-end 等。衰减 `s·e^(−ln2·Δt/半衰期)`，半衰期 7 天；低于 0.05 剪掉；容量 200，按 LRU 淘汰；同 path+signal 覆盖。
  - 沉积规则：同一文件读 ≥3 次且没写过 → entry-point 0.4；写入后有通过的验证 → well-tested 0.6，有失败 → fragile 0.8；同一个 bash target 出现 ≥2 次语义失败（超时、缺命令不算）→ dead-end 0.9。
  - 模型再碰到这个 path 时，以 advisory 形式提示。
- **项目知识库**：`.rivet/knowledge/memory.jsonl`（`memory/unified-memory.ts`），条目带 kind、validFrom/validTo、supersededBy、transferableTo、topic。
  - 每轮注入只放 project_rule 和 user_constraint 两类，且置信度 ≥0.95、≤1500 字符；其余只能召回取用。
  - 召回 = BM25 × 时间近因 1/(1+天/30)，可选向量 RRF 融合（`knowledge-index.ts`）。
  - 入库要过 essence-gate：会话结束时用一次廉价 LLM 裁决，只收"可迁移原则"，必须填 transferableTo；矛盾的写 supersede，重复的丢弃；LLM 失败则不写（fail-closed）。
  - 跨会话记忆每会话只查一次，放进附录，不抖动前缀。

## 6. Hooks

- **内部**：RuntimeHookPipeline 五个阶段 `preTurn → afterPerception → postTool → postTurn → postSession`（`agent/runtime-hooks.ts`）。每个 hook 限时 10 秒；hook 自己失败不影响主流程；只通过 effects 上报信号，不直接改 prompt。
- **用户**：`.rivet/hooks.json`（`hooks/user-hooks-runner.ts`），事件 PreToolUse / PostToolUse / UserPromptSubmit / PreCompact / SubagentStop / Notification。stdin 传 JSON；环境变量要清洗，不把密钥带进去；超时 5 秒。PreToolUse 可以 block 或改参数；被拦截的原因作为工具结果回给模型，并提示它换条路。
