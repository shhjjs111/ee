# Pi 调研与落地决定

> 用户决定（2026-10-26 22:28–22:29 CST）：
> - 自动压缩保持在上下文窗口 80% 触发，**不加 128k 上限**（下文报告中的该建议作废）。
> - Pi 只作参考，不强行适配。每项落地前必须实测：用 DeepSeek 跑同一批真实任务（修 bug、多文件修改、跑测试），精简/新做法与完整模式对比，成功率不下降才合入；不好用就放弃，只在此记录结论。
> - ee 的安全审批保持不变。
>
> 第一批候选（D5-9 完成后）：精简模式（read/edit/write/shell + 约 200 token 提示词，可切换，需实测）、工具描述压缩、edit 多处编辑与近似匹配报错、SYSTEM.md/APPEND_SYSTEM.md（经 Hooks 信任检查）、插话/完成后追加两条消息队列。

---

# ee 借鉴 Pi 编程 Agent：调研与落地方案

> 调研日期 2026-10-26。只读调研，没有改动 ee 仓库。
> ee 版本：`github.com/shhjjs111/ee` main 分支的克隆（`/workspace/work/review/clone-pi`）。Pi 版本：`github.com/earendil-works/pi` main 分支，提交 `f9daca3`（2026-10-26），克隆在 `/workspace/work/review/pi-src`。
> 本报告可以写来源；**落地到 ee 的代码和文档时，按用户要求不写第三方出处。**

## 结论（5 行）

1. 用户发第一句话之前，ee 的固定开销是 **7,343 token**（全新安装、DeepSeek tokenizer），其中 **18 个工具的 schema 占 6,344（86%）**，系统提示词 903，context-update 96。按典型配置（加上 AGENTS.md、记忆、3 个技能）是 **8,520**。Pi 默认 4 工具约 1,315（去掉 ee 用不到的 Pi 自身文档段约 1,030）。
2. 我起草了一份中文精简版（短系统提示词加 read/edit/write/shell 4 个短描述工具），实测 **约 865 token**，比现在少 88%。工具描述写长是主要问题，系统提示词本身已经不算长。
3. ee 已经冻结了前缀，DeepSeek 前缀缓存命中率本来就高，按 ee 自己配置的 flash 价格，固定开销每步只要约 ¥0.0003，**所以省钱不是主要收益**。真正的收益在三处：首包/未命中时的预填充延迟、子代理和压缩后重建前缀的成本、模型注意力更集中（Pi 的 terminal-bench 结果说明短提示词和少工具不掉质量）。
4. 最值得先做的 5 条：①精简模式（lean profile）；②所有工具描述缩到一两句，用法细则只在启用时进系统提示词；③edit 支持一次多处（`edits[]`），找不到匹配时给最接近的原文片段；④SYSTEM.md / APPEND_SYSTEM.md 替换或追加系统提示词；⑤排队消息拆成「插话转向」和「完成后追加」两条队列。
5. ee 的安全审批（权限模式、白名单、Hook 强制确认、worktree 隔离）**一条都不学 Pi**。Pi 默认 YOLO、没有权限系统。精简只动提示词和工具表，审批逻辑在代码里，不受影响。

---

## 1. 方法与文件

| 文件 | 内容 |
|---|---|
| `/workspace/work/review/measure_ee.py` | 用 ee 自己的 `Session`、`_ensure_instructions()`、`_add_user("你好")`、`wire_messages()` 构造真实请求体并计 token |
| `/workspace/work/review/ee-overhead.json` | 三个场景的完整统计（逐工具） |
| `/workspace/work/review/ee-request-{fresh,typical,optional-off}.json` | 构造出的真实请求体 |
| `/workspace/work/review/measure_pi.py` / `pi-system-prompt-rendered.txt` | 按 Pi 源码逐字重建的默认系统提示词和 4 个工具，同一个 tokenizer 计数 |
| `/workspace/work/review/lean_draft.py` / `lean_draft.json` | 精简版中文系统提示词和 4 工具草稿，以及它的计数 |

**计数方法**：主口径是 DeepSeek-V3 官方 `tokenizer.json`（HuggingFace `deepseek-ai/DeepSeek-V3`，用 `tokenizers` 库加载，不加特殊 token）。对照口径是 tiktoken `o200k_base`。tools 按 `json.dumps(tools, ensure_ascii=False)` 计数；服务端实际会把 tools 渲染进聊天模板，会有几十个 token 的模板开销，属于近似值。DeepSeek V4 的 tokenizer 没有公开文件，这里用 V3 的代替，**数字只作相对比较用**。

---

## 2. ee 当前的固定开销（实测）

### 2.1 汇总（单位 token，DeepSeek tokenizer；括号里是 o200k）

| 部分 | 全新安装 fresh | 典型 typical | 关掉可选工具 optional-off |
|---|---|---|---|
| 系统提示词（`prompts/system/base.md` + `deepseek.md`） | 903 (1037) | 903 | 903 |
| 工具 schema | **6,344 (6,758)**，18 个 | 6,344，18 个 | 3,273，9 个 |
| AGENTS.md 注入（1.3KB 示例） | — | 609 | — |
| 长期记忆（5 条） | — | 147 | — |
| 技能清单（3 个技能） | — | 422 | — |
| 首条消息的 `<context-update>` | 96 | 95 | 96 |
| **合计固定开销** | **7,343 (7,889)** | **8,520 (9,238)** | **4,272 (4,544)** |
| 整个请求体（含"你好"和 JSON 外壳） | 7,469 | 8,758 | 4,398 |

- 典型场景用的是合成数据：20 条规则的 AGENTS.md、5 条记忆、3 个技能。真实项目的 AGENTS.md 常常更长，上限是 32KB，约 1 万 token。
- 技能清单 3 个技能就要 422 token，其中**固定的使用规则约 300 token**，每个技能只有约 40 token。
- 仓库地图 `repo_map.auto_inject` 默认关闭，没有计入。打开后再加最多约 1,024。
- MCP 工具没有计入。每接一个 MCP 服务器都会追加它全部的工具 schema。

### 2.2 工具逐个开销（fresh，按整条 schema 排序）

| 工具 | 描述字符 | 描述 token | 整条 schema token | 来源文件 |
|---|---|---|---|---|
| task | 830 | 435 | **771** | `ee/ee/prompts/tools/task.md`、`ee/ee/tools/agent.py` |
| shell | 1057 | 505 | **737** | `prompts/tools/shell.md`、`tools/shell.py` |
| read | 612 | 342 | **560** | `prompts/tools/read.md`、`tools/read.py` |
| memory | 409 | 228 | 453 | `prompts/tools/memory.md` |
| repo_map | 510 | 266 | 421 | `prompts/tools/repo_map.md` |
| grep | 357 | 187 | 385 | `prompts/tools/grep.md` |
| edit | 353 | 200 | 361 | `prompts/tools/edit.md` |
| todo | 326 | 159 | 310 | `prompts/tools/todo.md` |
| web_search | 297 | 162 | 291 | |
| web_fetch | 293 | 149 | 288 | |
| ask_user | 211 | 133 | 277 | |
| skill | 364 | 197 | 272 | |
| shell_output | 226 | 112 | 250 | |
| shell_write | 179 | 97 | 239 | |
| glob | 245 | 122 | 229 | |
| present_plan | 184 | 110 | 199 | |
| write | 178 | 99 | 194 | |
| shell_kill | 60 | 30 | 89 | |

（`prompts/tools/*.md` 的文件字节数比上表"描述字符"多，因为 Tool 类加载时有处理；以上按实际发出的 description 计。）

### 2.3 对照：Pi 和精简草稿

| 方案 | 系统提示词 | 工具 | 合计 |
|---|---|---|---|
| Pi 默认（read/bash/edit/write） | 588（其中 Pi 自身文档段 284，ee 用不到） | 727（read 186 / bash 137 / edit 285 / write 114） | 1,315（去掉文档段约 1,030） |
| ee 精简草稿（`lean_draft.json`） | 202 | 567（read 142 / edit 177 / write 100 / shell 143） | **769，加 context-update 约 865** |
| ee 现在（fresh） | 903 | 6,344 | 7,343 |

只看 4 个核心工具，ee 的 read+write+edit+shell 是 1,852 token，Pi 是 727，草稿是 567。

### 2.4 钱和速度怎么看（按 ee 自己的价格配置）

`ee/ee/core/providers/profiles/deepseek.toml`（flash）里写的是：未命中 ¥2/百万，命中 ¥0.04/百万。

- 固定开销 7,343 token：首次请求（未命中）约 ¥0.015；命中以后每步约 ¥0.0003。一回合 50 步也只有约 ¥0.015。**钱上差别很小。**
- 子代理：`core/subagent.py:289` 会给子代理过滤工具表，前缀和父会话不同，每个子代理的第一次请求都是一次全量未命中。并行 explore 越多，这部分越明显。
- 延迟：未命中时预填充 7k token 比 0.9k 慢，具体多少要实测，见第 5 节"验证"。
- 质量：Pi 博客的论点是前沿模型都经过大量 RL，已经懂编程 agent 该怎么做，不需要 1 万 token 的提示词。他用约 1000 token 在 terminal-bench 2.0 上拿到了有竞争力的名次（来源：https://mariozechner.at/posts/2025-11-30-pi-coding-agent/ ，"Minimal system prompt" 和 "Benchmarks" 两节）。**这对 DeepSeek 是否同样成立，需要 ee 自己做对比实验**，不能直接照搬结论。

---

## 3. Pi 源码要点（逐条，附路径）

以下路径都相对于 `pi-src/packages/`。

### 3.1 默认系统提示词（`coding-agent/src/core/system-prompt.ts`）

由多个分段组成，除 preamble 外每段用同名 XML 标签包起来：

- **preamble**（逐字）："You are an expert coding assistant operating inside pi, a coding agent harness. You help users by reading files, executing commands, editing code, and writing new files."
- **`<tools>`**：每个启用的工具一行 `- name: snippet`，最后一句 "In addition to the tools above, you may have access to other custom tools depending on the project."
- **`<rules>`**：由 `buildRules()` 拼出来。只开 bash、没开 grep/find/ls 时加 "Use bash for file operations like ls, rg, find"；然后拼上**每个启用工具自己带的 guidelines**，以及扩展传进来的 promptGuidelines；最后固定两条 "Be concise in your responses"、"Show file paths clearly when working with files"。规则会去重。
- **`<docs>`**：Pi 自己文档的路径，只在用户问 Pi 本身时才去读。
- **`<addendum>`**：APPEND_SYSTEM.md 的内容。
- **`<project_context>`**：AGENTS.md 等，每个文件包在 `<project_instructions path="...">` 里。
- **`<skills>`**：技能清单。
- **`<cwd>`**：工作目录。
- `customPrompt`（来自 SYSTEM.md）只**替换 preamble、tools、rules、docs**；addendum、项目上下文、skills、cwd 仍然会追加。`forceSystemPrompt` 是扩展用的整段替换。
- `diffSystemPromptSections()`：分段之间做 diff，只把变化的段作为补丁发出去。这和 ee 的 `<context-update>` 思路相同。

实际渲染出来的 4 工具版本见 `/workspace/work/review/pi-system-prompt-rendered.txt`（DeepSeek tokenizer 下 588 token，其中 `<docs>` 段 284）。

### 3.2 四个工具（`coding-agent/src/core/tools/`）

每个工具有三处文字：
- `description`：放进 tool schema，一到两句；
- `promptSnippet`：系统提示词 `<tools>` 里的一行；
- `promptGuidelines`：**只在工具启用时**才并进 `<rules>`。

**read（`read.ts`）**
- 参数：`path`、`offset`（从 1 开始）、`limit`。
- 描述："Read the contents of a file. Supports text files and images (jpg, png, gif, webp, bmp). Images are sent as attachments. For text files, output is truncated to 2000 lines or 50KB (whichever is hit first). Use offset/limit for large files. When you need the full file, continue with offset until complete."
- 截断用 `truncateHead`，2000 行或 50KB，先到哪个算哪个，**不返回半行**。截断后在末尾追加 `[Showing lines X-Y of N (50KB limit). Use offset=Z to continue.]`。如果单行就超过 50KB，提示用 `sed -n 'Np' | head -c` 去读。
- **输出不带行号前缀**，是原文。

**bash（`bash.ts`、`truncate.ts`）**
- 参数：`command`、`timeout`（可选，默认不超时）。
- 输出用 `truncateTail` 保留**最后** 2000 行或 50KB。被截断时，全文写到临时文件，末尾提示 `[Showing lines X-Y of N. Full output: <path>]`。还有一种情况是最后一行本身过长，会标出 "Showing last N of line"。
- 截断信息（truncation 结构、fullOutputPath）放在 `details` 里给界面用。
- Windows 上另有 `powershell.ts`，`shellName: "PowerShell"`。

**edit（`edit.ts`、`edit-diff.ts`）**
- 参数：`path`、`edits: [{oldText, newText}]`，**一次调用可以改多处**。
- 描述："Edit a single file using exact text replacement. Every edits[].oldText must match a unique, non-overlapping region of the original file. If two changes affect the same block or nearby lines, merge them into one edit instead of emitting overlapping edits. Do not include large unchanged regions just to connect distant changes."
- guidelines：多处改动用一次调用；每处都对照**原文件**匹配，不是改一处再找下一处；不能重叠；oldText 越小越好，只要唯一。
- **参数容错 `prepareArguments`**：`edits` 是 JSON 字符串时会先解析（注释里写明 Opus 4.6、GLM-5.1 会这样发）；单个对象会包成数组；老格式的顶层 `oldText/newText` 也会并进 `edits`。
- **匹配**：先剥掉 BOM，把 CRLF 统一成 LF；每处先精确匹配，找不到再做模糊匹配。模糊匹配 `normalizeForFuzzyMatch` 会做 NFKC、去掉行尾空白、弯引号转直引号、各种破折号转 `-`、特殊空格转普通空格。模糊命中时，只重写被触及的那几行，其余行从原文件逐字拷回（`applyReplacementsPreservingUnchangedLines`）。最后恢复原来的换行风格和 BOM。
- **报错**（逐字）：
  - 找不到："Could not find the exact text in {path}. The old text must match exactly including all whitespace and newlines."（多处时写成 `edits[i]`）
  - 不唯一："Found N occurrences of the text in {path}. The text must be unique. Please provide more context to make it unique."
  - oldText 为空、两处重叠、替换后内容没变化，都各有专门的报错。
  - **没有"最接近的候选"提示**。
- 返回给模型的只有 `Successfully replaced N block(s) in {path}.`；diff、patch、firstChangedLine 都放在 `details` 里，只给界面用。
- `file-mutation-queue.ts`：同一个文件的写操作排队执行，避免并行调用的工具互相覆盖。

**write（`write.ts`）**："Write content to a file. Creates the file if it doesn't exist, overwrites if it does. Automatically creates parent directories." guideline："Use write only for new files or complete rewrites."

另外有 grep/find/ls 只读工具，**默认关闭**；只有用户想限制 agent 时才用它们代替 bash。

### 3.3 AGENTS.md / SYSTEM.md / APPEND_SYSTEM.md（`coding-agent/src/core/resource-loader.ts`）

- **上下文文件**（`loadProjectContextFiles`）：先加载全局 `~/.pi/agent/` 下的一个文件；再从 cwd 一直往上走到文件系统根，每层取一个，按**根在前、cwd 在后**的顺序排列。每个目录里按 `AGENTS.override.md` → `AGENTS.md` → `AGENTS.MD` → `CLAUDE.md` → `CLAUDE.MD` 取第一个存在的。会处理 git worktree 里的影子副本，避免同一份上下文加载两次。
- **SYSTEM.md**（`discoverSystemPromptFile`）：项目级 `<cwd>/.pi/SYSTEM.md`，但**只在项目被信任时**才用；否则用全局 `~/.pi/agent/SYSTEM.md`。二选一，项目级优先。
- **APPEND_SYSTEM.md**：同样的规则，二选一。
- 项目资源都要先过项目信任检查（`project-trust.ts`、`trust-manager.ts`）。

### 3.4 压缩（`coding-agent/src/core/compaction/compaction.ts`、`docs/compaction.md`）

- 触发条件：`contextTokens > contextWindow - reserveTokens`（默认 reserve 16384）。会在工具结果追加之后、下一次模型请求之前检查。另外两种情况也会触发：provider 报上下文溢出，或者提前出现 `stopReason: "length"`。这两种都会**压缩一次并重试一次**。
- 切点：从后往前累计到 `keepRecentTokens`（默认 20k）。不在工具结果处切，工具结果必须和它的调用在一起。如果单个用户回合就超过预算，就在回合中间切开，分两段摘要再合并（split turn）。
- 摘要提示词是结构化的：Goal / Constraints & Preferences / Progress（Done / In Progress / Blocked）/ Key Decisions / Next Steps / Critical Context。**第二次及以后的压缩**用 `UPDATE_SUMMARIZATION_PROMPT`，把旧摘要放进 `<previous-summary>` 做增量更新，不重新总结全部历史。
- 会累计记录读过和改过的文件。摘要请求关闭缓存写入，因为这种一次性请求不太会被复用。
- 原始条目留在会话文件里，只是不再发给模型。

### 3.5 会话树（`docs/sessions.md`、`docs/how-pi-works.md`、`session-manager.ts`）

- 会话是 JSONL 文件，每条记录有 id 和 parentId，形成一棵树。当前记录决定活动分支，下一次请求的历史就取这条分支。
- `/tree`：在同一个文件里跳到任意节点。选中用户消息时，会把原文放回输入框，改了再发就长出新分支。
- `/fork`：从某条用户消息起新开一个会话文件。`/clone`：复制当前分支。
- 离开一个分支时，可以对它做 **branch summary**（`branch-summarization.ts`），把摘要挂到新分支上。这样放弃的那条路上做过的事不会完全丢掉。

### 3.6 工作中排队：steering 和 follow-up（`agent/src/agent-loop.ts`、`docs/usage.md`、`docs/settings.md`）

- **Enter = steering**：等当前这次助手回复和它的工具调用跑完，在下一次模型请求前插进去（agent-loop.ts:184、214、304 三处轮询 `getSteeringMessages`）。
- **Alt+Enter = follow-up**：等 agent 本来就要结束时才送进去，然后继续跑（agent-loop.ts:311 `getFollowUpMessages`）。
- `steeringMode` 和 `followUpMode` 可以设成 `"one-at-a-time"`（默认）或 `"all"`。
- Alt+Up 把排队中的消息取回输入框。按 Escape 中止时，排队消息会退回输入框，不会丢。
- 长时间的准备工作（例如压缩）做完后，会再检查一次 steering。

### 3.7 扩展、skills、prompt templates

- **扩展**（`docs/extensions.md`）：TypeScript 模块，在进程内运行，权限和 Pi 一样。可以注册工具、命令、事件钩子、provider、渲染器。`prepareLoadout()` 可以让某个编排型工具把其他工具的声明从请求里隐藏掉（`hiddenTools`），但这些工具仍然可以通过它调用，`codemode` 就是这样实现的。这相当于**懒加载工具**的一种做法。
- **skills**（`coding-agent/src/core/skills.ts` 的 `formatSkillsForPrompt`）：系统提示词里只有 3 句说明，加上 `<available_skills>` 里每个技能的 name、description、location。**没有专门的 skill 工具**，直接用 read 读 SKILL.md。`disableModelInvocation` 可以把某个技能从清单里藏起来。
- **prompt templates**（`docs/prompt-templates.md`、`prompt-templates.ts`）：Markdown 文件，展开成用户消息，支持参数。ee 已经有同类功能（`ee/ee/core/prompts.py`），没有需要补的。
- 博客的思路：MCP 太占上下文（例如 Playwright MCP 21 个工具约 13.7k token），更推荐 **CLI 工具配 README**，需要时才读，也就是渐进披露。

### 3.8 工具结果：content 给模型，details 给界面（`agent/src/types.ts:427` 附近、博客 "Structured split tool results" 一节）

`AgentToolResult = { content: (text|image)[], details: T }`。content 给 LLM，details 是任意结构化数据，用来记日志和渲染界面。出错时 content 变成错误信息，details 仍然保留给界面。edit 和 bash 都是这样实现的。

### 3.9 重试与错误（`ai/src/utils/retry.ts`、`coding-agent/src/core/agent-session.ts:3766`、`docs/settings.md:128`）

- agent 级重试：`retry.maxRetries=3`，指数退避 `baseDelayMs=2000`，最多 `maxAgentDelayMs=60000`。
- `isRetryableAssistantError` 用两组正则判断：一组是可重试的（overloaded、server_busy、5xx、rate limit 等），一组是**不可重试的额度或订阅上限错误**。额度用完不重试，直接告诉用户。
- **上下文溢出不走重试**，交给压缩去恢复。
- provider 级重试默认是 0，文档建议保持 0，否则会拖慢 agent 自己对额度错误的处理。

### 3.10 token 和花费（`ai/src/types.ts:465` 附近、`usage-totals.ts`、`cache-stats.ts`）

Usage 里有 input、output、cacheRead、cacheWrite、reasoning、totalTokens，以及 `cost.{input,output,cacheRead,cacheWrite,total,totalFromProvider}`。价格来自模型目录；provider 报了实际计费时优先用它。界面底部显示上下文用量和累计花费，`/session` 能看到一个会话的总计。`cache-warmer.ts` 是给有 TTL 的缓存（Anthropic）做续期的，**DeepSeek 用不上**。

### 3.11 其他让它又快又省的设计

- **默认不内置**：todo、plan 模式、MCP、后台 bash、子代理都没有（博客 "No built-in to-dos" 到 "No sub-agents" 几节）。需要任务清单就让 agent 写 TODO.md 或 PLAN.md；需要长跑进程就用 tmux；需要子代理就通过 bash 再起一个 `pi --print`。
- 工具的规则文字跟着工具走，工具没开，规则就不进提示词。
- 只读工具默认关闭，靠 bash 一个工具覆盖 ls、rg、find。
- 读文件不带行号。
- 同一文件的写操作排队（file-mutation-queue）。

---

## 4. 借鉴清单（逐条对比）

难度：S = 半天到 1 天，M = 2 到 4 天，L = 1 周以上。

### A1. 精简模式（lean profile）【最高优先】
- **Pi**：默认只开 read/bash/edit/write，系统提示词由分段拼出来（`coding-agent/src/core/system-prompt.ts`、`tools/index.ts`）。
- **ee 现在**：`ee/ee/tools/__init__.py:build_tools` 里 read/write/edit/shell/grep/glob/todo/present_plan 是常开；后台 shell 3 个、ask_user、repo_map、web×2、task、memory、skill 默认也全开，一共 18 个。`todo` 和 `present_plan` 没有开关。
- **建议**：config 里加 `profile = "full" | "lean"`（可以在设置页切换，**从新会话开始生效**，因为工具表会冻结进前缀）。lean 模式下：
  - 工具只有 read / edit / write / shell 4 个；grep 和 glob 也可以做成可选，用 shell 里的 rg 代替。
  - 系统提示词换成 `prompts/system/lean.md`，约 200 token，草稿见 `lean_draft.json`。
  - 记忆、技能清单、仓库地图都改成"只在有内容时、用最短的格式"注入，见 A8。
  - 权限审批、Hook、只读白名单**完全不变**，因为它们在 `PermissionPolicy` 和 hooks 里，和提示词无关。
- **收益**：固定开销 7,343 → 约 865（-88%）。子代理每次起步的未命中预填充也相应减少。工具少了，选错工具（例如该用 edit 却用了 write）的机会也少。
- **风险**：DeepSeek 可能比 Claude 更依赖详细说明。少了后台 shell，dev server 这类场景会变差（可以让 shell 保留 `background` 参数，把 shell_output/kill 的用法写进描述里）。少了 todo，长任务的进度可能更容易漂。**必须用真实任务对比验证**。
- **难度**：S（构建工具表和选系统提示词的逻辑都已集中在一处）。

### A2. 所有工具描述缩到一两句，细则跟工具走
- **Pi**：每个工具分成 `description`（一两句）、`promptSnippet`（一行）、`promptGuidelines`（只在启用时进 `<rules>`），见 `tools/edit.ts`、`read.ts`、`bash.ts`。
- **ee 现在**：`ee/ee/prompts/tools/*.md` 共 21KB，多数是多段落的"用法"说明。最长的几个：task 771、shell 737（里面整段 Git 规范和 base.md 重复）、read 560、memory 453、repo_map 421 token。
- **建议**：
  1. 每个 `prompts/tools/X.md` 拆成两部分：`description` 不超过 2 句；"规则"部分交给系统提示词按启用的工具拼装。拼装方式学 `buildRules`：去重、工具没开就不拼。
  2. 删掉重复内容：shell.md 里的 Git 规范和 base.md 的「Git 与他人的改动」重复；read.md 里"不要用 shell 跑 cat"和 base.md 重复。
  3. 参数描述也要压缩：很多是"默认 xxx，最大 yyy"的重复说明。
  4. 修一处不准确的描述：edit.md 说"返回结果会附带 diff"，实际上 `tools/edit.py` 返回给模型的只有"已编辑…+a -r 行"，diff 只给界面。
- **收益**：full 模式下预计也能从 6,344 降到约 3,000（按草稿里 4 个工具平均每个约 140 token 估算，18 个约 2,600 到 3,200）。lean 模式依赖这一条。
- **风险**：有些细则是踩过坑才加上的，例如 PowerShell 用 `;`、不要用 `&` 放后台、read 返回"与上次相同"的引用。删之前要逐条确认，必要的改成一句话的规则留在提示词里。
- **难度**：S 到 M（主要是文字工作，加上一份拼装逻辑）。

### A3. edit：一次多处、更好的报错、参数容错
- **Pi**：`edits[]` 多处、全部对照原文件匹配、检查重叠；精确匹配失败再做模糊匹配；每类错误都有专门报错；`prepareArguments` 容错（`tools/edit.ts`、`edit-diff.ts`）。
- **ee 现在**：`ee/ee/tools/edit.py` + `tools/fileops.py:replace`。一次只能改一处（`old_string/new_string/replace_all`）。模糊策略其实比 Pi 多：精确 → rstrip → trim → 缩进无关 → 首尾锚点相似度，还会拒绝过大的模糊块。找不到时的报错只列出试过的策略，**没给最接近的候选**。另外 ee 要求编辑前必须先 read 过（Pi 没有这条），这条应该保留。
- **建议**：
  1. schema 改成 `edits: [{old_string, new_string}]`，并兼容旧的顶层 `old_string/new_string/replace_all`（容错写法同 Pi：字符串先 JSON 解析，单个对象包成数组）。所有 edits 都对照原文件匹配，检查重叠，最后一次性写入。界面上的 diff 和快照不变。
  2. 找不到时，用 difflib 在文件里找相似度最高的窗口，返回"最接近的第 L1-L2 行"原文（带行号，最多约 15 行），并指出第一处不一致的行。**这一点 Pi 也没做**，对 DeepSeek 这种偶尔记错缩进或空格的模型最有用。
  3. 不唯一时，返回每处匹配的行号，模型可以据此补上下文。
  4. 加上 Pi 的 Unicode 归一（弯引号、破折号、NBSP、NFKC），作为一种新的模糊策略。中文文档和作业场景里，全角和半角符号混用很常见。
  5. 同一文件并行写入要串行化。先确认 `session._dispatch` 是否已经把写类工具串行执行；如果没有，加一个按路径排队的锁。
- **收益**：减少"失败 → 重新 read → 再改"的往返，每次失败大约省 1 到 2 步加一次全文 read（大文件就是几千 token）。多处改动从 N 次调用降到 1 次。
- **风险**：一次改多处中途失败时要整体不写入（原子性）。各个模型对数组参数的支持程度不一，需要容错和测试。
- **难度**：M。

### A4. SYSTEM.md 替换 / APPEND_SYSTEM.md 追加
- **Pi**：`resource-loader.ts:1249-1275`。项目级 `.pi/SYSTEM.md` 只在项目被信任时生效，否则用全局的；APPEND 同理。SYSTEM.md 只替换前半段，AGENTS.md、skills、cwd 仍然追加。
- **ee 现在**：`ee/ee/core/context.py:system_prompt()` 固定是 `system/base.md` 加模型补丁 `system/<family>.md`，用户没有办法替换。
- **建议**：
  - 加载顺序：`<项目根>/.ee/SYSTEM.md`（项目已信任）→ `~/.ee/SYSTEM.md` → 内置 base（lean 或 full）。APPEND_SYSTEM.md 也按这个规则二选一，追加在后面。模型补丁（deepseek.md 等）仍然追加，可以设开关关掉。
  - 项目级的信任检查**复用 hooks 已有的"路径 + 内容指纹"信任机制**。文件内容一变就要重新确认，防止仓库自带的 SYSTEM.md 悄悄注入指令。
  - 设置页显示当前用的是哪个系统提示词文件，并提供"查看最终系统提示词"。
  - 系统提示词在会话开始时冻结，所以改了文件要从新会话才生效。现有的 sha256 警告已经覆盖了这个情况。
- **收益**：用户可以自己做精简实验，也可以写作业专用的提示词，不用改代码。
- **风险**：替换系统提示词**不能削弱安全审批**。这一点天然成立，因为审批在代码里；但要在文档里写清楚"系统提示词不是安全边界"。项目级文件是提示注入的入口，所以必须过信任检查。
- **难度**：S。

### A5. 工作中排队：插话转向和完成后追加
- **Pi**：steering 和 follow-up 两条队列；可选逐条或全部送入；中止时把排队消息退回输入框（`agent/src/agent-loop.ts:184-315`、`docs/usage.md`）。
- **ee 现在**：只有一条 `Session.pending` 队列（`core/session.py:169 queue_input`）。在每一步工具执行完、或模型不再调工具时，把所有排队消息合成一条 user 消息送进去（`session.py:623-658`），也就是只有 steering 的"全部送入"模式。服务端 `server/threads.py:229` 会把条目标成 `queued`。中断时 `pending.clear()`（`session.py:187、227`），**排队消息直接丢掉**。
- **建议**：
  1. 拆成 `steer_queue` 和 `follow_queue`。follow-up 只在模型这一步不再调工具、回合本来要结束时才送入（插在 `on_stop` 之前）。
  2. 加 `steering_mode` 和 `follow_up_mode`，可选 `one` 或 `all`，默认 `one`。
  3. 中断时把排队消息通过事件退回前端输入框，不清空。
  4. 前端：运行中按 Enter = 插话，Ctrl+Enter（或发送按钮的下拉菜单）= 完成后再做。排队条目显示"插话 / 待追加"的标签，可以撤回。
  5. 审批弹窗等待期间收到的插话，保持现在"当前步结束后送入"的行为，**不要绕过审批**。
- **收益**：用户可以一边看一边纠偏，同时又不打断当前的子任务。中断也不会丢消息。
- **风险**：前端交互要设计清楚。present_plan 的 `_end_turn` 和 Hook Stop 逻辑要一起考虑。
- **难度**：M（后端 S，前端 M）。

### A6. 工具结果给模型和给界面分开
- **Pi**：`{content, details}`。edit 只告诉模型"replaced N blocks"，diff 放在 details（`agent/src/types.ts`、`tools/edit.ts`、`tools/bash.ts`）。
- **ee 现在**：已经大体分开了：`ToolResult(output, summary, diff, meta)`（`ee/ee/tools/base.py:15`）。output 给模型，summary/diff/meta 给界面；shell 的 meta 里还另存了 `output[-4000:]` 给界面。
- **建议**（小修补）：
  1. 把 `meta` 正式定成 `details` 契约，按工具类型写好 TypedDict，前端按类型渲染，避免前端再去解析 output 文本。
  2. 检查每个工具给模型的 output 有没有多余文字，例如 edit 的容错策略说明和 stale 提示可以更短。
  3. 截断信息（总行数、全文路径）同时放进 details，界面上可以提供"打开全文"按钮。
  4. **（实验）read 的行号前缀**：实测 `core/session.py` 加上行号后多出 **19.3%** token（12,700 → 15,149）。Pi 不加行号。ee 依赖行号做 `路径:行号` 引用，也帮助定位编辑位置，所以建议先保留，改成不补齐宽度（`12\t` 而不是 `  12\t`）；lean 模式可以试试"不带行号"，做 A/B 对比。
- **收益**：前端更稳；每次 read 可以省 5% 到 19%。
- **风险**：不带行号时，模型引用行号会不准。
- **难度**：S。

### A7. 把重功能改成可关闭（todo、plan、记忆注入、仓库地图等）
- **Pi**：不内置 todo、plan、子代理、后台 bash、MCP，需要时用文件、tmux 或 CLI 代替（博客对应几节）。
- **ee 现在**：`todo`、`present_plan` 常开，没有开关；后台 shell（`background_shell`）、ask_user、repo_map、web、subagent、memory、skills 有开关，但默认全开。
- **建议**：
  - `todo`：加 `tools.todo` 开关。lean 下默认关，系统提示词里写一句"多步任务可以在回复里列步骤"，或者让 agent 维护 `.ee/TODO.md`。base.md 里"3 步以上先用 todo"这条要跟着开关条件拼装。
  - `present_plan`：只在会话以 plan 模式开始时注册。模式中途切换会改变工具表，导致缓存失效，可以接受，但要提示一下。或者干脆在 plan 模式下让模型把计划写进 PLAN.md 或直接用文字回复，审批照旧。
  - `memory`：工具和注入分成两个开关（`memory.tool`、`memory.inject`）。
  - `repo_map`：lean 下关闭工具，需要时可以通过 shell 跑 `ee repomap` 子命令（Pi 的"CLI + README"思路）。
  - `task`（子代理）、web：lean 下关闭；需要的话用户在设置里开，或者用技能文件说明怎么调 CLI。
  - **不建议做"会话中途按需加载工具"**：ee 的 tools 冻结在前缀里，DeepSeek 的缓存按前缀匹配，中途改 tools 数组会让整段前缀失效。Pi 的 `hiddenTools` 是靠一个常驻的编排工具（codemode）间接调用别的工具实现的。ee 如果想要类似效果，可以做一个常驻的 `ee_tool(name, args)` 通用入口，配合技能文档按需说明用法。这样工具表不变、缓存也不失效，但会牺牲 schema 校验，**列为后续实验**。
- **收益**：每关一个工具省 90 到 770 token；用户可以按自己的习惯组合。
- **风险**：功能入口变多，设置页要清楚说明"从新会话生效"。
- **难度**：S。

### A8. 注入块精简（AGENTS.md、记忆、技能清单）
- **Pi**：AGENTS.md 包一层 `<project_instructions path>` 就进去了；技能清单只有 3 句说明（`skills.ts:358`），用 read 加载 SKILL.md，没有专门的工具。
- **ee 现在**：`core/instructions.py`、`core/memory.py:216`、`core/skills.py`。三个块各有一段中文说明头，技能块固定约 300 token 的"使用规则"。另外 skill 工具本身还要 272 token。
- **建议**：技能规则压到 3 句；lean 模式不要 skill 工具，直接用 read 读 SKILL.md 的路径（`$名字` 点名时附全文的逻辑保留）。记忆头压到 1 句。**保留 ee 把这些放在 system 之后的固定 user 消息里的做法**：这样所有项目的 system+tools 字节都一样，DeepSeek 跨会话、跨项目都能命中缓存。这一点比 Pi 放进系统提示词更好，不要改。
- **收益**：典型场景约省 350 token，加上 skill 工具 272。
- **风险**：技能触发率可能下降，要用"点名触发"和"按描述自动触发"两类用例测。
- **难度**：S。

### A9. 压缩：按预留量触发、增量摘要
- **Pi**：`window - reserve(16384)` 触发，保留最近 20k，结构化摘要加 `<previous-summary>` 增量更新，在回合中间切开时分两段摘要，溢出或 length 时压缩一次再重试（`compaction/compaction.ts`、`docs/compaction.md`）。
- **ee 现在**：`core/session.py:933 needs_compaction` 在 `threshold(0.8) × context_window` 时触发。DeepSeek 的 window 配置是 1,000,000，**也就是 80 万 token 才会压缩**。保留最近 3 条用户消息或 20k（`compaction.build_history`）。摘要模板 `prompts/compact.md` 已经是结构化的（目标、决策、改过的文件、状态、失败方案、下一步）。
- **建议**：
  1. 触发条件改成 `min(window - reserve, compact.max_context)`，`max_context` 默认值比如 128k，用户可以调。长上下文下模型质量和速度都会下降，1M 的窗口不代表应该用到 80 万。
  2. 第二次压缩起，把上次摘要作为 `<previous-summary>` 做增量更新，不重新总结全部历史，摘要请求更短也更稳。
  3. provider 报上下文溢出时，自动压缩一次再重试（见 A10）。
- **收益**：长会话的单步延迟和花费下降，未命中部分少了，也减少"长上下文变笨"。
- **风险**：压得更频繁，每次压缩后前缀会重建（一次未命中）。阈值需要实测。
- **难度**：S 到 M。

### A10. 重试和错误分类
- **Pi**：agent 级 3 次指数退避（2s 起，最多 60s），可重试和不可重试（额度、订阅上限）用正则分类，上下文溢出交给压缩（`ai/src/utils/retry.ts`、`agent-session.ts:3766`）。
- **ee 现在**：`core/providers/net.py` 在连接失败、超时、5xx、429 时重试 2 次；中文报错做得很好。**没有区分"429 限流"和"额度或余额不足"**（DeepSeek 余额不足是 402，要按官方文档核对）。也没有"上下文溢出 → 压缩 → 重试"。
- **建议**：加 `classify(error) -> retry | quota | overflow | fatal`。quota 不重试，直接提示去充值或换模型；overflow 压缩一次再重试；retry 用指数退避加抖动，如果服务端给了 Retry-After 就照它来。
- **收益**：少了无意义的重试等待，长会话溢出时能自动恢复。
- **风险**：各家报错文本不统一，模式要靠测试积累。
- **难度**：S。

### A11. 会话树和分支摘要（低优先）
- **Pi**：单个 JSONL 文件里存一棵树，用 `/tree` 跳转，离开分支时生成摘要（`session-manager.ts`、`compaction/branch-summarization.ts`）。
- **ee 现在**：有 `thread/fork` 和 `edit_resend`（`server/branch.py`），有快照回退，没有在同一个对话里浏览分支，也没有分支摘要。
- **建议**：先只做"编辑重发或 fork 时，可以选择把被放弃分支的摘要附到新分支"。完整的树形界面以后再说。
- **收益**：试错时不丢已经知道的信息。
- **风险**：界面复杂度。
- **难度**：M 到 L。

### A12. token 和花费展示（小项）
- **Pi**：Usage 里有 cacheRead/cacheWrite/reasoning、成本分项、`totalFromProvider`。
- **ee 现在**：`core/context.py UsageStats` 已经有命中率和花费，比较完整。
- **建议**：设置页或状态栏加一个"上下文构成"视图，把本报告的统计脚本做成内置功能：显示 system、tools（逐个）、AGENTS.md、记忆、技能、历史各占多少 token。用户关开关时能直接看到省了多少。`cache_warmer` 不需要，DeepSeek 的缓存没有 TTL 可以续。
- **难度**：S。

### 明确不学的
- **YOLO 和没有权限系统**：ee 的权限模式、只读白名单、Hook 强制确认、worktree 隔离、审批弹窗全部保留，lean 模式下也一样。
- **Pi 的 TypeScript 扩展机制**：ee 已经有 Hooks、MCP、技能、自定义提示，形态不同，没必要搬。
- **把 AGENTS.md 放进系统提示词**：ee 现在放在固定 user 消息里，跨项目缓存更好，见 A8。

---

## 5. 实施顺序与验证

### 第一批（约 1 周，都是 S 级，互相独立）
1. **A2 工具描述精简**加上**A7 开关**（todo 和 present_plan 加开关，memory 拆分开关）。
2. **A1 lean profile**：基于 A2 的短描述，加 `prompts/system/lean.md`。
3. **A4 SYSTEM.md / APPEND_SYSTEM.md**，复用 hooks 的信任指纹。
4. **A8 注入块精简**。
5. **A10 错误分类**。

### 第二批
6. **A3 edit 多处和最接近候选**（M）。
7. **A5 两条队列加前端**（M）。
8. **A9 压缩阈值和增量摘要**。
9. **A6 details 契约和 read 行号实验**、**A12 上下文构成视图**。

### 之后
10. A11 分支摘要；`ee_tool` 通用入口实验（A7 末尾）。

### 怎么验证
1. **静态 token 对比（每个 PR 都跑）**：把 `measure_ee.py` 改成仓库内的测试，例如 `tests/test_prefix_budget.py`，断言 full 和 lean 两种模式的 system+tools token 上限（例如 lean ≤ 1,200，full ≤ 3,500）。这样以后加工具或改描述时，开销上涨会直接让测试失败。tokenizer 可以用字符数近似，避免 CI 依赖 HF 文件：实测 DeepSeek 下中文工具 JSON 大约每 token 2.2 个字符。
2. **假模型 e2e**（`desktop/scripts/fake_model_server.py`、`ee/scripts/e2e_ws.py` 已有）：
   - 断言 lean 模式请求体里的 tools 只有 4 个；SYSTEM.md 替换后请求的 system 是自定义内容；未信任项目里的 SYSTEM.md 不生效。
   - edit 多处：脚本化的假模型发出 `edits` 是字符串、单个对象、旧参数格式这三种，都要成功；故意给错 old_string，断言报错里有"最接近的第 X-Y 行"。
   - 队列：运行中发插话和追加，断言送入的时机和顺序；中断后断言排队消息通过事件退回。
   - 审批回归：lean 模式下写文件和危险命令照样弹审批（现有的 `test_permissions.py`、Hook 测试全部要过）。
3. **真实任务对比（DeepSeek flash，full 和 lean 各跑一遍）**：
   - 选 10 到 15 个 ee 自己仓库里的真实小任务（修一个测试、加一个设置项、改一个前端组件、写一份文档、做一道作业题），每个跑 3 次。
   - 记录：是否完成（测试过没过）、步数、总 prompt/completion token、命中率、花费、首 token 延迟、总耗时、edit 失败次数。前四项从 `UsageStats` 和会话 JSONL 里就能拿到。
   - 判定标准：lean 的完成率不低于 full 减 5 个百分点，同时 token 或耗时明显下降，才把 lean 设成默认；否则 lean 作为可选模式保留，再看哪些工具或规则需要加回来。

---

## 6. 不确定和没有核实的
- token 数用的是 DeepSeek-V3 的公开 tokenizer，V4 的 tokenizer 没有公开文件。tools 按 JSON 字符串计数，没有包含服务端模板开销。
- 典型场景的 AGENTS.md、记忆、技能是合成数据，真实项目可能更大。
- "精简会不会降低 DeepSeek 的完成率"没有实测。Pi 的基准成绩是用 Claude Opus 4.5 跑的。
- `session._dispatch` 是否已经把同一文件的并行写串行执行，我没有逐行核实（A3 第 5 点）。
- DeepSeek 余额不足的状态码（402）要再对照官方错误码文档。
- 克隆 ee 时，shell 提示 `/workspace/projects/.../ee/ee/ee/server/answer_rpc.py` 有变动。这是同一工作区里别的任务产生的，本次调研没有写 /workspace/projects 下的任何文件。
