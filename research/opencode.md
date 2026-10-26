# OpenCode 源码研究笔记（为 Python 小 agent 借鉴）

**仓库地址**：`github.com/sst/opencode` 现在 301 跳转到 **`github.com/anomalyco/opencode`**，默认分支 `dev`。
**版本**：
- **V1（下文简称"旧版"）**：tag `v1.19.4`（2026-10-22），代码在 `packages/opencode/src/`。题目里的路径就是这一版，下文路径都以它为准。
- **V2（下文简称"新版"）**：`dev`（2.1.4，commit 5be4b16）。用 Effect 重写成 `packages/core/src`（核心）+ `server` / `client` / `sdk` / `tui`，`packages/opencode` 这个目录已经删掉。
- **本地副本**：`/workspace/work/research/opencode`（dev 分支），`/workspace/work/research/opencode-v1`（v1.19.4，sparse checkout）。

## 1. 主循环
- `session/prompt.ts` 里的 `runLoop`（约 1081 行）是一个 `while(true)`：
  1. 读消息，并滤掉已经压缩过的历史（`filterCompacted`）。
  2. 取最后一条 user 消息和最后一条 assistant 消息。
  3. **退出条件**：assistant 的 `finish` 不是 `tool-calls`/`unknown`，并且消息里没有未执行的工具调用（有的 provider 带着 tool call 也返回 stop，所以要额外检查）。
  4. `step++`。第 1 步另起后台任务生成标题和摘要。
  5. 待处理任务队列里有 `subtask` 就跑子代理，有 `compaction` 就跑压缩。
  6. 上一轮 token 超过上限（overflow）时，插入一个压缩任务，然后 `continue`。
  7. `SessionReminders.apply` 往最后一条 user 消息里注入 synthetic 的 `<system-reminder>`。
  8. 组装 system prompt 和工具，调用 `processor.process()`。返回 `stop` 就 break，返回 `compact` 就建压缩任务，返回 `continue` 就进入下一轮。
- **步数上限**：`agent.steps` 是最大步数。到最后一步时，在消息末尾追加一条 assistant 消息 `MAX_STEPS_PROMPT`（新版放在 `core/src/session/runner/max-steps.ts`），要求模型只用文字总结、不再调工具。
- **`session/processor.ts`**：消费 AI SDK 的 `fullStream`，把每个事件落盘成消息的 part（text / reasoning / tool / step-start / step-finish / patch 等）。工具 part 有四种状态：`pending → running → completed | error`。
- **防死循环（`DOOM_LOOP_THRESHOLD = 3`）**：最近 3 个 part 如果是同一个工具、参数用 `JSON.stringify` 比较完全一样，就触发 `permission.ask("doom_loop")` 让用户确认。
- **重试**：`session/retry.ts` 采用指数退避，初始 2s，优先读响应头里的 `retry-after-ms` / `retry-after`。
- **工具调用修复**：`session/llm.ts` 的 `experimental_repairToolCall` 分两种情况：
  - 工具名大小写不对：改成小写再调用。
  - 其他情况：改调 `invalid` 工具（`tool/invalid.ts`），返回"参数无效：错误信息"，模型拿到后会自己重写参数。
  - 新版另有 `core/src/plugin/tool-input-repair.ts`：按 JSON Schema 修正参数类型，例如字符串 `"20"` 转成数字 20、被字符串化的对象转回对象、`null` 视为缺省字段。

## 2. 工具集
- **注册**：`tool/registry.ts`。内置工具有 invalid、question、shell（bash）、read、glob、grep、edit、write、task、webfetch、todowrite、websearch、skill、apply_patch，可选 lsp、plan。工具描述单独放在同名 `.txt` 文件里。
- **按模型换编辑工具**：`registry.ts:297`，模型是 `gpt-*`（不含 oss 和 gpt-4）时只给 `apply_patch`，其他模型给 `edit` + `write`。
- **`tool/tool.ts`**：工具执行完统一走 `Truncate.output`。参数校验失败时返回"请重写参数以满足 schema"。
- **read**（`tool/read.ts`）：
  - 默认读 2000 行，单行超过 2000 字符就截断。
  - 输出格式为 `<path>…</path><content>` 加上 `行号: 内容`，结尾提示 `Use offset=N to continue`。
  - 文件不存在时给出"Did you mean"候选；二进制文件直接拒绝。
  - 读到子目录时，把该目录下的 AGENTS.md 作为 `<system-reminder>` 附在输出里。
- **grep / glob**：底层用 ripgrep，结果上限 100 条，按 mtime 排序，超出时提示结果被截断。
- **shell**（`tool/shell.ts`）：
  - 用 tree-sitter 解析命令，提取每条子命令作为权限 pattern。
  - "始终允许"的粒度由 `permission/arity.ts` 决定：一张表规定每个命令前缀占几个 token，比如 `git`→2、`npm run`→3，于是 `git commit -m x` 记成 `git commit *`。
  - 命令里出现工作区外的路径时，额外申请 `external_directory` 权限。
  - 有超时；输出只保留尾部，完整输出存文件并在结果里给出路径。
- **todowrite**：模型每次提交整张 todo 列表（content / status / priority），标题显示未完成数。
- **task**（`tool/task.ts`）：见第 4 节。
- **edit 的模糊匹配**（旧版 `tool/edit.ts:217-737`）：
  - 有 9 个 replacer，都是 generator，依次产出"文件里实际存在的候选子串"：
    1. Simple
    2. LineTrimmed：逐行 trim 后比较
    3. BlockAnchor：首尾行当锚点（至少 3 行），中间行用 Levenshtein 算相似度，阈值 0.65，行数偏差不超过 25%
    4. WhitespaceNormalized
    5. IndentationFlexible：去掉公共缩进后比较
    6. EscapeNormalized：还原 `\n`、`\"` 等转义
    7. TrimmedBoundary
    8. ContextAware：首尾锚点加中间行至少 50% 相同
    9. MultiOccurrence
  - `replace()` 依次尝试每个 replacer，拿到候选后用 `indexOf === lastIndexOf` 保证唯一，否则继续试下一个；`replaceAll` 时全部替换。
  - **安全阀 `isDisproportionateMatch`**：匹配到的片段行数 ≥ max(old+3, old×2)，或者字符数远大于 oldString 时，拒绝替换。
  - **前后处理**：先统一换行符（按文件原有的 CRLF/LF 转换）；保留 BOM；写盘后自动格式化；再调 LSP，诊断错误附在输出里（"LSP errors detected, please fix"）。
  - **新版收紧了策略**（`core/src/tool/plugin/edit.ts`），只剩三档：精确匹配 → 统一花引号、破折号、特殊空格后匹配 → 忽略每行行尾空白后匹配。匹配前先把文件转成 LF 视图，替换后再按 LCS 对齐恢复每行原来的换行符。多处匹配时报错。

## 3. 按模型切换系统提示词
- **旧版**：`session/system.ts` 的 `provider(model)` 按 `model.api.id` 子串选择提示词文件，文件都在 `session/prompt/*.txt`：
  - 含 `gpt-4` / `o1` / `o3` → `beast.txt`
  - `gpt` + `codex` → `codex.txt`；其他 `gpt` → `gpt.txt`
  - `gemini-` → `gemini.txt`
  - `claude` → `anthropic.txt`
  - kimi → `kimi.txt`
  - 其余 → `default.txt`（**DeepSeek 走这个**）
- **拼接顺序**（`session/llm/request.ts:56`）：
  1. 如果 agent 自带 `prompt`，就用它，**不再加** provider 提示词；否则用 provider 提示词
  2. 环境块 `<env>`（工作目录、是否 git 仓库、平台、日期、模型 id）
  3. AGENTS.md / CLAUDE.md 等指令文件（`session/instruction.ts`：全局一份，项目里向上 findUp，第一个命中就停）
  4. `<mcp_instructions>`
  5. skills 列表
  6. user 消息自带的 system
  - 以上合并成**一条** system 消息，方便命中 prompt cache（`transform.ts` 的 `applyCaching` 会打 ephemeral 缓存标记）。
- **新版**：统一用一份 `core/src/session/runner/prompt/system.txt`，再根据实际启用了哪些工具插入对应的工具使用指引（`system-prompt.ts`）。各厂商的提示词改成插件 `core/src/plugin/optimize.ts`，可以替换或追加。

## 4. Agents
- **定义**：`agent/agent.ts`。一个 agent 就是 `{name, mode: primary|subagent, permission 规则集, prompt?, model?, temperature, steps}`。
- **build / plan 的区别只在权限**：plan 把 `edit` 设为 `"*": deny`，只允许写 `.opencode/plans/*.md`；`task.general` 也是 deny。
- **plan 模式提示**：靠 `session/reminders.ts` 往 user 消息注入 `plan.txt`（"只读阶段、严禁修改"）。从 plan 切回 build 时注入 `build-switch.txt`，告诉模型"可以改文件了"，并提示去执行已有的 plan 文件。
- **子代理**：
  - `general`：全部工具，但禁用 todowrite。
  - `explore`：默认拒绝一切，只放行 grep / glob / read / bash / webfetch，有专用提示词。
- **隐藏 agent**：`compaction`、`title`、`summary`，权限是 `*: deny`。它们本质是有固定提示词的单次 LLM 调用。
- **task 工具**：
  - 参数：`description`、`prompt`、`subagent_type`、`task_id`（传入则续接之前的子会话）、`background`。
  - 子会话用 `parentID` 指向父会话。嵌套深度受 `subagent_depth` 限制，默认 1。
  - 子会话的权限 = 父会话权限 + 子代理权限，并强制禁用 todowrite 和 task 自身，防止无限递归。
  - 子代理的结果包在 `<task id state><task_result>…</task_result></task>` 里返回给父代理。
  - 工具描述明确要求：并行时一次发多个 task；给子代理的任务要写全；说清"写代码还是只调研"；结果对用户不可见，需要父代理自己转述。

## 5. 权限
- **规则**：`permission/index.ts` 中每条规则是 `{permission, pattern, action: allow|ask|deny}`，pattern 支持通配。
- **求值**：多个规则集按"defaults → agent → 用户配置 → 会话已批准"顺序拼接，`evaluate` 用 **`findLast`**（后面的覆盖前面的），没有命中则默认 `ask`。
- **ask 的流程**：任一 pattern 被 deny 就直接报错；有需要询问的就发布 `Asked` 事件，并在 Deferred 上挂起等待。
- **回复有三种**：
  - `once`：只放行这一次。
  - `always`：把请求里的 `always` 模式（如 `git commit *`）写进本次会话的已批准列表，并顺带放行其他已被覆盖的待处理请求。
  - `reject`：带反馈文字时抛 `CorrectedError`，模型会读到反馈继续工作；不带反馈则拒绝本会话所有待处理请求。
- **默认配置**：`*: allow`；`doom_loop` 和 `external_directory` 为 ask；读 `*.env` 要 ask；`question`、`plan_enter` 默认 deny（由各 agent 自己放开）。
- **工具可见性**：`disabled()` 把规则为 `"*": deny` 的工具直接从工具列表里删掉，模型根本看不到。edit、write、apply_patch 共用 `edit` 这一个权限名。

## 6. 压缩与裁剪
- **溢出判断**（`session/overflow.ts`）：`usable = input上限 − min(20k, maxOutput)`。上一轮 assistant 消息记录的 token 用量 ≥ usable 时触发压缩。provider 直接报 ContextOverflow 时也会触发。
- **压缩流程**（`session/compaction.ts`）：
  1. 把历史切成 turn，从尾部往前保留最近的 turn，预算为 `clamp(usable×25%, 2k, 15k)` token（单个 turn 超预算时可以切开）。
  2. 头部的历史序列化成 `[User]: … / [Assistant tool call]: …` 纯文本，每个工具结果截到 2000 字符。
  3. 用 compaction agent 生成结构化摘要。已经有摘要时，要求"更新旧 checkpoint"而不是重写。
  4. **摘要模板**（新版 `core/src/session/compaction.ts:159`）：Objective / Requirements / Decisions / Work State（Completed / Active / Blocked）/ Next Move / Relevant Files（最多 15 个）/ Important Context。规则：保留原样的路径、命令和报错；不提"上下文被压缩过"。
  5. 压缩完成后，把原来那条 user 消息重放一遍，让循环继续。
- **prune**：每轮结束后在后台执行。从后往前扫描，跳过最近 2 个 user turn，保留最近 40k token 的工具输出，更早的给 part 打上 `compacted` 时间戳。之后再发给模型时，这些输出替换成 `"[Old tool result content cleared]"`（`message-v2.ts:303`）。只有能省下 20k 以上才执行。skill 工具的输出不会被清掉。
- **工具输出截断**（`tool/truncate.ts`）：
  - 上限 2000 行或 50KB，可以从头保留或从尾保留。
  - 完整输出写到 `tool-output/tool_<id>`，保留 7 天后清理。
  - 返回预览加一条提示：建议让 explore 子代理用 grep / read（带 offset）处理这个文件，"不要自己读全文"。

## 7. 多 provider / MCP / LSP
- **provider**（`provider/provider.ts`）：
  - 模型元数据（上下文和输出上限、是否支持 reasoning / tool_call / 温度、价格）来自 models.dev 的 JSON 目录。
  - 每个 provider 声明用哪个 npm SDK，SDK 动态 import（`BUNDLED_PROVIDERS`），OpenAI 兼容接口统一走 `@ai-sdk/openai-compatible`。
- **`provider/transform.ts`（1900 行）是各家特例的集中地**：
  - 消息规整、缓存标记。
  - 推理强度（variant）映射：`reasoningEffort`，deepseek-v4 额外支持 `max`。
  - 温度和 topP 默认值。
  - **DeepSeek 特例**（`transform.ts:303-340`）：所有 assistant 消息必须带 reasoning 字段，没有就补一个空的；同时把 `reasoning_content` 字段原样回传（即使是空字符串）。
- **MCP**（`mcp/index.ts`、`mcp/catalog.ts`）：
  - 支持三种传输：stdio、StreamableHTTP，失败时回退到 SSE。支持 OAuth。
  - 工具名格式为 `sanitize(server)_sanitize(tool)`，非 `[a-zA-Z0-9_-]` 的字符替换成 `_`。
  - 监听 `tools/list_changed` 通知并刷新工具列表。
  - 额外提供 `list_mcp_resources` / `read_mcp_resource` 两个工具；服务端自带的 instructions 注入 system。
- **LSP**（`lsp/server.ts`、`lsp/client.ts`）：
  - 按文件扩展名自动启动语言服务器，内置 30 多种（如 pyright、gopls、clangd）。
  - 编辑后发 `didOpen` / `didChange`，等待 `publishDiagnostics`，把诊断错误附在 edit / write 的工具输出里，形成"改完马上看到报错"的闭环。

## 8. 客户端 / 服务端
- **服务端**：`opencode` 在本地启动 HTTP 服务（`server/server.ts`，基于 Effect HttpApi，有 OpenAPI 定义）。
  - 会话、消息、权限回复都是 REST 接口。
  - 实时更新通过 SSE（`/event`、`/global/event`）推送 Bus 事件：消息、part 更新、权限请求等。
  - 会话和消息持久化在 SQLite。
- **客户端**：TUI、桌面版、Web、IDE、ACP 都只是客户端。SDK 由 OpenAPI 生成，支持 mDNS 发现。
- **新版分层**：Schema → Core / Protocol → Server，Client 只依赖 Protocol。
- **效果**：agent 内核完全无界面，权限请求作为事件推给客户端，客户端调 reply 接口回复。

## 可抄的设计要点（Python 版落地）
1. **主循环**：`while` 循环的退出条件写成"finish 不是 tool_calls，并且没有未执行的工具调用"。加步数上限，到最后一步时追加一条"不许再调工具"的 assistant 消息。
2. **消息结构**：消息由 part 列表组成，工具 part 有状态机，每次变更都落盘，崩溃后能恢复，压缩和裁剪也可以只改单个 part。
3. **edit 用"候选子串生成器链"**，配合"唯一性检查 + 匹配片段过大保护"，失败信息要告诉模型下一步怎么改。
4. **工具输出统一截断**：超出部分存文件，把文件路径告诉模型。
5. **旧工具输出打标记清空**，再加结构化 checkpoint 摘要，阈值用模型元数据计算。
6. **权限规则表**：`(权限名, 通配 pattern, allow/ask/deny)`，用 `findLast` 求值；支持 once / always / 带反馈的 reject；被 deny 的工具从工具列表里隐藏。
7. **agent 就是"提示词 + 权限集 + 步数"**，plan 模式靠权限加 reminder 实现。
8. **DeepSeek 兼容**：回传 `reasoning_content`，把 provider 特例集中放在一个 transform 模块里。
9. **非法工具调用改成返回错误给模型**，参数做类型修正，让模型自己纠错，而不是直接崩溃。
10. **内核做成服务**（HTTP + SSE 事件），以后加 Web 或手机客户端不用动内核。
