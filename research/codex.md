# OpenAI Codex CLI（codex-rs）设计笔记 —— 供 Python + DeepSeek 小 agent 借鉴

源码：github.com/openai/codex main 分支 tarball（2026-10-25 拉取），本地解压在 `/workspace/work/research/codex/codex-rs`。以下路径都相对 `codex-rs/`。

## 1. 主循环（turn → 多次 sampling）
- `core/src/session/turn.rs`：`run_turn()`（L174）是一次用户回合；内部 `loop {}` 反复调 `run_sampling_request()`（L1730）→ `try_run_sampling_request()`（L2657）流式读模型输出。
- `core/src/stream_events_utils.rs::handle_output_item_done()`：每个输出 item 完成时判断——是 tool call 就**先把调用写进历史**，再把工具 future 推入 `FuturesOrdered`（边流式边开跑），并置 `needs_follow_up=true`；是普通消息就记为 `last_agent_message`。
- **结束条件**：本次采样没有任何工具调用且没有排队的用户输入（`needs_follow_up = model_needs_follow_up || has_pending_input`，turn.rs L622）→ 跑 Stop hooks → 结束回合。没有固定最大步数。
- 工具出错用 `FunctionCallError::RespondToModel(msg)` 回填成 tool output 让模型自己修正；只有 `Fatal` 才中断回合。被中断的调用也要补一条 "aborted" 输出（`ToolCallRuntime::aborted_response`），保证每个 call_id 都有配对输出。
- 用户可在回合中途插话（steer）：`input_queue` 在下一次采样前注入。
- 流错误有重试/退避（`core/src/responses_retry.rs`）。

## 2. 工具集合
定义在 `core/src/tools/handlers/*_spec.rs`，注册/路由在 `tools/registry.rs`、`tools/router.rs`。
- `exec_command`（`shell_spec.rs`）："Runs a command in a PTY, returning output or a session ID for ongoing interaction." 参数：`cmd, workdir, tty, yield_time_ms(默认10000), max_output_tokens(默认10000), login`，加审批参数 `sandbox_permissions / justification / prefix_rule`。长命令返回 `session_id`，用 `write_stdin(session_id, chars, yield_time_ms)` 继续交互/轮询（unified_exec）。
- **没有专门 read_file/grep 工具**：读文件、搜索都走 shell，提示词要求 `rg`/`rg --files`。
- `apply_patch`（`apply_patch_spec.rs` + `core/assets/tools/apply_patch.lark`）：freeform/custom tool，用 Lark 文法约束输出，不包 JSON。格式：
```
*** Begin Patch
*** Add File: hello.txt
+Hello world
*** Update File: src/app.py
*** Move to: src/main.py
@@ def greet():
-print("Hi")
+print("Hello, world!")
*** Delete File: obsolete.txt
*** End Patch
```
  解析器 `apply-patch/src/parser.rs`；定位上下文 `seek_sequence.rs` 逐级放宽：精确 → 忽略行尾空白 → 忽略首尾空白（容错关键）。
- `update_plan`（`plan_spec.rs`）：`{explanation?, plan:[{step, status: pending|in_progress|completed}]}`，"At most one step can be in_progress at a time."；工具本身只是让 UI 渲染，几乎无逻辑。
- 其他：`web_search`（OpenAI 托管工具，`tools/hosted_spec.rs`）、`view_image`、`request_user_input`、`tool_search`（工具多时延迟加载 `defer_loading`）、MCP 工具、多代理工具。
- 工具描述风格：**一句话描述 + 每个参数一句话（含默认值/范围）**，行为规范写在系统提示词里而不是描述里。

## 3. 系统提示词结构
- 基础指令：`protocol/src/prompts/base_instructions/default.md`（≈21KB）；按模型有变体 `core/gpt_5_1_prompt.md`、`core/gpt_5_codex_prompt.md`（Codex 专训模型只要 7KB 精简版）。章节：身份/能力 → AGENTS.md 规范 → Preamble（调用工具前 1-2 句告知）→ Planning（好/坏计划示例）→ Task execution（"keep going until the query is completely resolved"）→ Validating → 最终答复格式 → Tool Guidelines。
- 每轮额外注入的"上下文片段"（`core/src/context/*.rs`）作为 developer/user 消息：权限说明（`prompts/templates/permissions/sandbox_mode/*.md`、`approval_policy/*.md`）、`<environment_context>`（cwd、shell、日期时区、网络）、AGENTS.md。
- AGENTS.md 加载（`core/src/agents_md.rs`）：从 cwd 向上找到项目根（标记 `.git`），把根→cwd 路径上每个 `AGENTS.override.md`/`AGENTS.md` 依次拼接，上限 `project_doc_max_bytes=32KiB`；用户级 `~/.codex/AGENTS.md` 在前，分隔符 `--- project-doc ---`；以 user 角色、`# AGENTS.md instructions for <dir>\n<INSTRUCTIONS>...</INSTRUCTIONS>` 包裹（`context/user_instructions.rs`）。越深层优先，直接指令优先于 AGENTS.md。

可抄片段：
> Please keep going until the query is completely resolved, before ending your turn and yielding back to the user. Only terminate your turn when you are sure that the problem is solved. Do NOT guess or make up an answer.
> Do not waste tokens by re-reading files after calling apply_patch on them. The tool call will fail if it didn't work.
> NEVER revert existing changes you did not make unless explicitly requested.

## 4. 沙箱与审批
- `AskForApproval`（`protocol/src/protocol.rs` L1098）：`untrusted`（除白名单外都问）、`on-request`（默认，模型自己通过 `sandbox_permissions:"require_escalated"`+`justification` 申请）、`never`（失败直接回给模型）、`granular`。
- sandbox_mode：`read-only` / `workspace-write`（可写 cwd+writable_roots，网络默认关）/ `danger-full-access`。实现：macOS Seatbelt、Linux bubblewrap+landlock+seccomp（`linux-sandbox/`、`sandboxing/`），Windows 专用（`windows-sandbox-rs/`）。
- 判定：`core/src/exec_policy.rs` + `execpolicy/`（前缀规则，可被用户"永久批准" `prefix_rule`）；`shell-command/src/command_safety/`（危险命令识别、bash 按 `| && ; $()` 切段逐段判定）；补丁安全 `core/src/safety.rs::assess_patch_safety`（写路径都在可写根内则自动通过）。
- 关键模式：**先在沙箱里跑，失败像沙箱/网络错误再申请提权**（on_request.md 原文："rerun the command with require_escalated"）。

## 5. 多代理
`tools/handlers/multi_agents_spec.rs`：`spawn_agent / send_input / send_message / followup_task / wait_agent / list_agents / close_agent / interrupt_agent`，子代理继承模型，`agents.max_threads` 限并发。描述里写死纪律："Do not spawn sub-agents unless the user … explicitly ask"；关键路径自己做、旁路任务委派；写集不相交；"Call wait_agent very sparingly"。

## 6. 上下文管理
- 截断：`utils/output-truncation/src/lib.rs`，**保留头尾、截中间**，插入 `…N tokens truncated…`，前缀 `Warning: truncated output (original token count: X)\nTotal output lines: Y`。默认每条工具输出 10000 bytes/tokens（`models-manager/src/model_info.rs`，按模型配置）。
- 自动压缩：阈值 `auto_compact_token_limit = min(配置, context_window*0.9)`（`protocol/src/openai_models.rs` L534）。回合前与回合中（`needs_follow_up` 且超限）触发 `run_auto_compact`。本地实现 `core/src/compact.rs`：用 `prompts/templates/compact/prompt.md` 让模型写交接摘要；新历史 = 初始上下文 + **最近的用户消息（倒序累计≤20000 tokens）** + 摘要（前缀 `summary_prefix.md`："Another language model started to solve this problem and produced a summary…"）。
- 压缩提示词：
> You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task. Include: Current progress and key decisions made; Important context, constraints, or user preferences; What remains to be done (clear next steps); Any critical data, examples, or references needed to continue.

## 7. MCP
`codex-mcp/`（连接管理）+ `rmcp-client/`（stdio/streamable HTTP 客户端）；`~/.codex/config.toml` 的 `[mcp_servers.<name>]` 配置；工具名命名空间化为 `mcp__<server>__<tool>`；结果过大用 `truncate_mcp_tool_result` 截断；MCP 工具调用也走审批（`core/src/mcp_tool_call.rs`）。CLI 还可 `codex mcp-server` 把自己暴露为 MCP 服务器。

## 8. 为什么 GPT 在里面好用
- 只走 **Responses API**（`model-provider-info/src/lib.rs` 的 WireApi 只剩 Responses）：`include:["reasoning.encrypted_content"]` 让推理状态跨步回传，`parallel_tool_calls:true`（turn.rs `build_prompt`），`prompt_cache_key` 保缓存命中。
- **apply_patch 是模型训练过的格式**，且用 grammar 约束解码；提示词与工具名共同训练（"NEVER try applypatch"）。
- 并行工具：`tools/parallel.rs` 用 RwLock——支持并行的工具拿读锁同时跑，其他拿写锁串行。
- 提示词按模型族定制，越专训的模型提示词越短。
- 对 DeepSeek 的启示：Chat Completions 无 freeform/grammar，需把补丁放进 JSON 字符串参数 `{"patch": "..."}`，并在系统提示词写全格式+示例；不要依赖模型"天生会"。

## 10 条最值得抄的要点
1. 循环终止判定：本轮无 tool_calls → 结束；有就执行后继续，不设硬步数（可设安全上限）。
2. 工具错误回填为 tool 消息让模型自修，只有致命错误才抛出；每个 tool_call_id 必有输出（含中断）。
3. 工具少而强：shell + apply_patch + update_plan（+web_search），读/搜都用 `rg`。
4. apply_patch V4A 格式 + 三级容错行匹配（精确/去尾空白/去首尾空白），失败信息要具体。
5. 输出截断"保头尾、截中间"并标注原长度，单条默认≈10k tokens。
6. 上下文到窗口 90% 自动压缩：交接摘要 + 保留最近≤20k tokens 用户原话 + 系统前缀。
7. AGENTS.md 从项目根到 cwd 逐层拼接，限 32KiB，以 user 消息带标签注入。
8. 每轮注入 `<environment_context>`（cwd/shell/日期/权限），权限说明用模板化小段文字。
9. 审批三档（untrusted/on-request/never）+ 命令前缀白名单 + 按 `| && ;` 切段判定 + "先沙箱跑、失败再申请提权"。
10. 提示词核心句：持续到问题彻底解决、调用前一句 preamble、好/坏计划示例、改完别重读文件、不要回滚非自己的改动。
