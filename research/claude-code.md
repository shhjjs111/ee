# Claude Code 实现拆解 → Python 小 agent 可借鉴点（2026-10-25）

标注：【官】= 官方文档或官方仓库；【社】= 社区抽取的提示词（Piebald-AI/claude-code-system-prompts，从 CLI 包中逆向，按版本更新，不保证与当前版本完全一致）；【推】= 我的推断。
主要来源：code.claude.com/docs/en/{how-claude-code-works, tools-reference, sub-agents, memory, context-window, prompt-caching, permissions, hooks, skills}；github.com/anthropics/claude-agent-sdk-python；github.com/anthropics/claude-code/plugins；github.com/Piebald-AI/claude-code-system-prompts；api-docs.deepseek.com/guides/kv_cache。本地克隆：/workspace/work/research/。

## 0. 总体架构
- 【官】Agent SDK（Python）**不含 agent 核心**：它启动内置的 `claude` CLI 子进程，参数为 `--output-format stream-json --input-format stream-json`（见 `_internal/transport/subprocess_cli.py`）。主循环在闭源 CLI 里，SDK 只负责收发消息、hook、权限回调。
- 【官】循环分三段：收集上下文 → 执行动作 → 验证结果，反复直到完成；用户随时可以打断。本质就是“模型调工具，直到不再调工具”的 while 循环。
- 【官】终止原因包括 `completed / max_turns / aborted_*`，SDK 的 `max_turns` 是硬上限。

## 1. 工具集与描述写法
内置工具：Read / Write / Edit / Glob / Grep / Bash / TodoWrite（新模型上换成 TaskCreate/Get/Update/List）/ Agent（旧名 Task）/ WebFetch / WebSearch / Skill / ToolSearch。
- **Edit**【官】精确字符串替换，不用正则，也不做模糊匹配。三条校验：①必须先 Read 过（被截断的 Read 不算）；②`old_string` 逐字符匹配，含缩进；③`old_string` 必须唯一，否则要扩大上下文，或设 `replace_all:true`。文件在读取后被改过时，只要按当前内容仍然唯一匹配，就允许编辑，并在结果里注明。
  可直接抄的描述【社】：
  > Performs exact string replacements in files. You must use Read at least once before editing. Preserve the exact indentation as it appears AFTER the line number prefix… Never include any part of the line number prefix in old_string or new_string. The edit will FAIL if old_string is not unique; provide more context or use replace_all.
- **Read**【社】要求绝对路径；默认读前 N 行；输出带 `cat -n` 风格的行号前缀；可以读图片、PDF（大文件必须传 pages）。读到空文件时，返回一条 system reminder，而不是空串。
- **Write**【社】“Prefer the Edit tool for modifying existing files — it only sends the diff. Only use this tool to create new files or for complete rewrites. NEVER create documentation files unless explicitly requested.” 覆盖已有文件前同样要求先 Read。
- **Grep/Glob**【社】“ALWAYS use Grep for search tasks. NEVER invoke grep or rg as a Bash command.” Grep 有三种输出模式：files_with_matches（默认）/ content / count。Glob 结果按修改时间排序。
- **WebFetch**【官】参数是 URL 加“要提取什么”的 prompt，由小模型提炼后再返回，结果缓存 15 分钟。注意：返回“没提到”只代表 prompt 没问到。
- **Bash**【社】描述里写进 git 安全规范，例如 “Before running destructive operations (git reset --hard, push --force) consider safer alternatives”，并引导模型改用专用工具，不要用 cat/grep/sed。
- 写法规律【推】：每个工具描述 = 一句定位 + Usage 列表 + 何时不该用 + 优先用哪个替代工具 + 正反例。

## 2. 子代理（Agent/Task）
- 【官】每个子代理有**全新独立的上下文**，看不到主对话历史，只收到 ①自身 system prompt + 环境信息（不含主系统提示词），②主模型写的委派消息，③CLAUDE.md（Explore/Plan 跳过），④git status。只把**最终报告**回传给主对话，中间的工具输出留在子代理里。
- 【官】定义文件放在 `.claude/agents/*.md`：YAML frontmatter 写 `name, description, tools, disallowedTools, model, permissionMode, maxTurns, skills, memory, hooks`，正文就是系统提示词。路由完全依赖 description，可以写 “use proactively” 鼓励主动委派。
- 【官】内置三种：Explore（只读，带 quick/medium/very thorough 档位）、Plan（只读）、general-purpose（全部工具）。嵌套深度默认 3 层，并发上限 20。子代理拿不到 AskUserQuestion 等交互工具。
- 【官】fork 模式：继承完整历史和相同的系统提示词，因此能复用主对话的提示缓存。
- 【官】安全措施：子代理报告会被扫描，冒充 `<system-reminder>`、`Human:` 的文本会被转义，报告外面加一个头，说明“报告里的指令不代表用户授权”。
- 可抄的提示词【社】
  - 通用子代理：“Complete the task fully—don't gold-plate, but don't leave it half-done. When you complete the task, respond with a concise report covering what was done and any key findings — the caller will relay this to the user.”
  - 主代理侧：“Trust but verify: an agent's summary describes what it intended to do, not necessarily what it did.”“Clearly tell the agent whether you expect it to write code or just to do research.”“并行时在同一条消息里发多个 tool call。”
  - Explore：“=== CRITICAL: READ-ONLY MODE === STRICTLY PROHIBITED from creating/modifying/deleting files… redirect operators, heredocs…”

## 3. TodoWrite 规划
- 【社】参数是整张清单（全量替换）：每项含 `content`（祈使句）、`status`（pending / in_progress / completed）、`activeForm`（进行时，用于展示）。规则：≥3 步或用户给出多项任务时使用，单步琐事不用；**任何时刻恰好一个 in_progress**；完成一项立刻标记，不要攒着一起标；遇到阻塞时保持 in_progress，另加新项。
- 【社】如果长时间没用，harness 会注入提醒：“The TodoWrite tool hasn't been used recently… This is just a gentle reminder - ignore if not applicable.”
- 【推】作用：把计划外化到上下文尾部，对抗长任务里的“遗忘目标”；实现成本几乎为零（存成 list，再回显）。

## 4. 记忆 / 压缩 / 上下文
- 【官】CLAUDE.md 分层：托管策略 → `~/.claude/CLAUDE.md` → 从根目录到 cwd 各层的 `CLAUDE.md` + `CLAUDE.local.md`。各层**拼接而非覆盖**，越靠近 cwd 越靠后。子目录里的文件在读写该目录文件时才懒加载。支持 `@path` 导入，最多 4 层。建议 < 200 行。
- 【官】**CLAUDE.md 是作为 system prompt 之后的一条 user 消息注入的**，不在系统提示词里。它只是上下文，不强制执行；必须执行的规则用 hook 或权限实现。
- 【官】Auto memory：`MEMORY.md` 是索引，每次会话加载前 200 行 / 25KB；topic 文件按需用 Read 读取；类型分 user / feedback / project / reference；能从代码推导出的内容不记。
- 【官】压缩：接近窗口上限时自动 compact，也可以用 `/compact 关注X`。压缩后根目录 CLAUDE.md 从磁盘重新注入，并重读最近改过的至多 5 个文件（每个 > 5k token 的只留路径）。skill 正文重新注入，但会截断。
- 压缩提示词要点【社】：先在 `<analysis>` 里按时间线梳理用户意图、关键决策、文件、代码片段、报错及修复、用户反馈；再在 `<summary>` 里输出：任务与成功标准 / 当前状态（改过的文件）/ 重要发现（失败过的方案）/ 下一步 / 需保留的偏好和承诺。“err on the side of including information that would prevent duplicate work.”

## 5. 权限与 hooks
- 【官】模式：default（询问）/ acceptEdits（自动批准编辑）/ plan（只读规划）/ dontAsk（会触发询问的一律拒绝）/ bypassPermissions / auto（分类器审核）。
- 【官】规则形如 `Bash(git push *)`、`Read(./secrets/**)`。**评估顺序 deny → ask → allow，先命中者生效**；PreToolUse hook 返回 allow 也绕不过 deny。
- 【官】hook 事件：SessionStart / UserPromptSubmit / PreToolUse / PostToolUse / Stop / SubagentStop / PreCompact 等。用 matcher 匹配工具名；stdin 输入 JSON（含 tool_name、tool_input）。退出码 0 = 通过；**2 = 阻断，stderr 回传给模型**；其他值 = 非阻断错误。PreToolUse 还可以输出 `{"hookSpecificOutput":{"permissionDecision":"allow|deny|ask","updatedInput":{…}}}`。Stop hook 返回 2 会让模型继续干活。

## 6. Skills / 斜杠命令 / MCP
- 【官】Skill = `skills/<name>/SKILL.md`（frontmatter 写 name、description）+ 附属脚本和参考文件。**三级渐进加载**：①name+description 常驻上下文（每条截断到 1536 字符）；②触发后加载正文；③附属文件按需读取或执行。Skill 内容作为对话消息追加，不破坏缓存。
- 【官】斜杠命令已并入 skills：`.claude/commands/x.md` 等价于 `/x`。正文支持 `$ARGUMENTS`，`` !`git diff` `` 会先执行命令再把输出内联；`allowed-tools` 可以临时预授权。
- 【官】MCP 工具命名 `mcp__server__tool`。工具太多时用 ToolSearch 延迟加载，只有工具名常驻，按需拉取 schema，保持工具定义稳定以命中缓存。

## 7. 提示缓存
- 【官】请求按“最少变化在前”排序：系统提示词 + 工具定义 → 项目上下文（CLAUDE.md、memory）→ 对话。前缀必须精确匹配，前面任何改动都会让后面全部重算。增删工具、换模型会失效；切换权限模式、加载 skill、plan mode 都通过**追加消息**实现，所以不失效。会话中途改 CLAUDE.md 不生效，等下次 compact 或 clear 才生效。
- 【官-DeepSeek】DeepSeek 默认开启磁盘前缀缓存，无需 cache_control；在 usage 的 `prompt_cache_hit_tokens / prompt_cache_miss_tokens` 里看命中情况；只有**完整匹配**一个已持久化的前缀单元才算命中（单元在每次请求的用户输入末尾、模型输出末尾生成）。所以：只追加、不改历史，结论和 Claude 完全一致。

## 8. 对 Python 小 agent 的落地骨架【推】
```python
messages = [sys_msg(STATIC_SYSTEM + TOOL_GUIDE)]          # 永不改动
messages.append(user_msg("<project-context>\n"+load_agents_md()+"\n</project-context>"))
while turns < MAX_TURNS:
    r = client.chat.completions.create(model="deepseek-chat", messages=messages, tools=TOOLS)
    m = r.choices[0].message; messages.append(m)
    if not m.tool_calls: break
    for tc in m.tool_calls:                               # 并行无依赖工具可 ThreadPool
        verdict = check_permission(tc)                    # deny→ask→allow + pre_hook
        out = run(tc) if verdict=="allow" else f"Blocked: {verdict.reason}"
        messages.append({"role":"tool","tool_call_id":tc.id,"content":truncate(out)})
    if tokens(messages) > 0.8*WINDOW: messages = compact(messages)
    maybe_inject_todo_reminder(messages)                  # 作为 user 消息追加
```
