# 个人 Agent 详细设计 v1（2026-10-25）

代号暂定 `ee`（命令行 `ee`），Python 3.11+。**多模型通用**（2026-10-25 用户要求：随便接什么模型都能用、都适配好）；DeepSeek 是默认和省钱主力，不是唯一。
来源：research/{codex,claude-code,opencode,tianshu}.md；UI 参照 codex-rs/tui。

## 1. 架构：内核与界面分离（OpenCode/Codex 做法）
```
ee/
  core/
    session.py      主循环（asyncio），产出事件流
    providers/      多模型层：openai_chat 适配器 + transforms.py + profiles/*.toml（家族档案）
    context.py      冻结前缀 + 增量附录 + 压缩/prune
    events.py       事件类型：TextDelta/ReasoningDelta/ToolStart/ToolOutput/ToolEnd/
                    ApprovalRequest/PlanUpdate/Usage/TurnDone/Error
    permissions.py  deny→ask→allow 规则、命令拆分、会话内“以后都行”
    store.py        会话 jsonl 存盘：resume / fork / export
    agents.py       子代理定义加载（agents/*.md，frontmatter: name/description/tools/model/max_steps）
  tools/            每个工具一个文件：schema + 描述(prompts/tools/*.md) + run() + read_only 标记
  prompts/          system.md、compact.md、tools/*.md、agents/explore.md 等
  ui/
    tui.py          Codex 风格终端界面（prompt_toolkit + rich）
    exec.py         无界面单发：`ee exec "任务"`，输出最终结果（脚本/批处理用）
  config.py         ~/.ee/config.toml + 项目 .ee/
```
界面只消费事件，以后加网页界面/桌面端不动内核。

## 1.5 多模型层（core/providers/）—— 2026-10-25 用户定：只做 OpenAI Chat Completions 协议
用户经常换模型；国产模型都用（DeepSeek、Qwen、Kimi、GLM、MiniMax、豆包等），GPT 通过中转站用；其他协议他用网关转换。所以：
- 只实现一个协议适配器 openai_chat（流式、tools、并行工具、usage）。Responses / Anthropic 不做。
- 配置：config.toml 里 [providers.x] base_url + api_key_env；[models.y] provider + 模型名 + 可选覆盖项。key 只从环境变量读。
- 模型家族按模型名子串识别（OpenCode 做法，因为走网关/中转 base_url 不可信）：deepseek / qwen / kimi|moonshot / glm / minimax / doubao / gpt|o\d / claude / gemini / default。
- 家族档案 profiles/*.toml：上下文窗口、最大输出、单价（含缓存价）、是否支持并行工具/视觉/推理、推理字段、提示词补丁、编辑工具风格、怪癖列表。可在 config 里逐项覆盖。
- 推理：统一识别 reasoning_content / reasoning 字段，以及 content 里的 <think>…</think>（MiniMax 等），渲染为思考块；回传规则按家族（DeepSeek 带工具轮回传、缺失补 ""；其他默认不回传，可配）。
- 缓存：冻结前缀/只追加规则对所有模型统一；命中数读 prompt_cache_hit_tokens 或 prompt_tokens_details.cached_tokens，都没有就显示"未知"（很多中转站不返回）。
- 编辑工具：GPT 家族额外提供 apply_patch（JSON 字符串参数 + 提示词里完整格式说明，它训练过这个格式）；其他家族用 edit 精确替换+容错链。底层同一个改文件引擎。
- 提示词：prompts/system/base.md + 家族补丁段（gpt / deepseek / qwen / kimi / glm / default）。
- 怪癖 transforms.py 集中处理：工具名大小写、content 里的 JSON/DSML/XML 工具调用兜底、参数类型修复、空 content、缺 tool_call_id、中转站把 usage 放最后一帧或不给、某些模型不支持 parallel_tool_calls / stream_options 时自动降级并记住。
- /model 随时切换（提醒缓存会从零开始）；子代理可单独指定模型。
- `ee doctor <模型>` 自检：对话、工具调用、并行工具、流式、推理字段、缓存字段，逐项结果写回本地档案。

## 2. 主循环（session.py）
- 一个用户回合：组装请求 → 流式调用 → 收集 tool_calls → 执行 → 追加结果 → 再调用。
- 结束条件：finish_reason≠tool_calls 且没有未执行调用。
- 安全上限 max_steps=50（可配）；到最后一步追加“只用文字总结，不再调工具”。
- 只读工具（read/grep/glob/web_fetch）同一步并行执行；有写操作的按顺序。
- 每个 tool_call_id 必有输出（被中断写“用户中断”）。工具错误原样回喂。
- Esc 中断：取消当前流/工具，保留已产生的历史，可继续说话。
- 运行中可以继续打字：排队，当前步结束后作为新 user 消息送入（Codex 的 queued input）。

## 3. 工具集（M1 七个 + 后续）
| 工具 | 要点 |
|---|---|
| read | 带行号（cat -n 风格），offset/limit，默认 2000 行；未变的重复读返回“与上次相同”引用（省 token）；PDF/docx 抽文字（M4） |
| write | 新建/覆盖；覆盖已有文件前必须读过 |
| edit | old/new 精确替换，必须先读、必须唯一或 replace_all；失败依次放宽：trim 行尾 → 逐行 trim → 缩进无关 → 首尾锚点+相似度≥0.65；匹配块远大于 old 时拒绝；返回 diff |
| bash | 超时（默认 120s，可后台）；输出头尾截断 |
| grep / glob | 调 rg；结果按修改时间排序；上限 + 截断提示 |
| todo | 整表替换，状态 pending/in_progress/completed，同时仅一项进行中；界面显示为计划清单 |
| task（M3） | 子代理：explore（只读、flash）/ general；独立上下文，只回传 `<task_result>` 总结 |
| web_search / web_fetch（M2） | 搜索 API 待定；fetch 转 markdown |
- 工具描述照 Claude Code 写法：做什么、怎么用、何时别用、该用谁替代。
- 截断：单次输出超 2000 行或 50KB → 全文存 ~/.ee/tool-output/，返回头尾预览+路径。

## 4. 缓存工程（天枢规则，对所有前缀缓存模型通用；DeepSeek 实测最关键）
1. system 与 tools 在会话首个请求后冻结：不增删不重排；每次请求前算 sha256，变了打警告。
2. system 里不放日期/git/cwd 等动态信息；这些做成 `<context-update seq=N>` 挂在最新 user 消息尾部：首次全量，之后只发变化项。
3. 历史一旦发出字节不变：先复制再改，绝不原地改；侧路请求（压缩、标题）不碰主会话。
4. reasoning_content：带工具的轮次回传，缺失补 ""，写入后不改。
5. AGENTS.md / 技能正文作为 user 消息追加，不改 system。
6. 压缩只在用户新消息到来时做（窗口 ≥0.86 或命中率已冷时），保留开头锚点和最近消息，摘要用 flash，切分边界对齐 assistant↔tool 配对。
7. 统计：prompt_cache_hit_tokens / miss_tokens，状态栏显示命中率和本会话花费（按 config 里的单价算）。
8. fork 式子代理：复制父 messages 前缀 + 末尾追加任务，复用父缓存。

## 5. 权限与安全（Codex + OpenCode）
- 三档模式（/permissions 切换，状态栏显示）：
  - 只读：只能读和搜。
  - 默认：工作区内读写免问；bash 白名单前缀（ls/cat/rg/git status/git diff/pytest…）免问，其他问；工作区外写、联网命令问。
  - 全自动：都不问（仍拦 deny 规则，如 rm -rf /、git push --force）。
- 命令按 | && ; $() 拆段逐段判。
- 审批弹窗选项（抄 Codex）：是，仅这次 / 是，以后 `git commit` 开头的都不问 / 否，跳过 / 否，并告诉它怎么改。
- 写文件前对原文件做快照 → `/undo` 回滚上一回合的改动（OpenCode rewind）。

## 6. 稳定性
- 死循环：同工具同参数连续 3 次 → 暂停问用户；A→B→A 摆动、回复 trigram 重合≥60% → 追加一条提醒。
- 坏调用兜底：content 整段是 JSON 或含 DSML 标记 → 解析成工具调用；参数类型修复（"20"→20、字符串化数组→数组、null 可选字段删除）；工具名大小写纠正；仍失败回喂错误让它重写。
- 网络：429/5xx 指数退避 3 次。
- 证据门（M3）：本回合改过文件但没跑过一次成功的验证（测试/运行/编译），收尾时追加提醒让它去验证或说明为什么没法验证。

## 7. 系统提示词要点（抄 Codex/Claude Code，按 DeepSeek 调）
- 坚持到问题真正解决再结束；不确定就去读代码，不猜。
- 调用工具前一句话说要做什么；多步任务先 todo。
- 优先 edit 不用 write；改完不必重读；不回滚不是自己做的改动；不擅自 git commit/push。
- 回答简洁，中文；最终总结列出改了哪些文件、怎么验证的。
- 作业模式提示：讲思路、给步骤，不只给答案（可在 AGENTS.md 里调）。

## 8. UI：Codex 风格终端界面
技术：prompt_toolkit（输入框、弹出补全、快捷键、非全屏）+ rich（markdown、代码高亮、diff）。
- 内联模式不占全屏：对话历史直接进终端滚动区（可用鼠标选中复制），底部固定输入区。
- 历史单元（抄 codex history_cell）：
  - 用户消息：`›` 开头灰底。
  - 助手：流式 markdown，代码块高亮。
  - 思考：折叠成一行灰色“思考中…”，Ctrl+T 看全文。
  - 工具：`• Ran pytest -q` / `• Read src/a.py` / `• Edited a.py (+12 −3)` 一行摘要，输出折叠只显示末尾几行；edit 显示彩色 diff。
  - 计划：todo 渲染成 ☐ ◐ ☑ 清单。
- 状态行：`• Working (12s · Esc 中断)` 带闪烁动画；底部 footer：模型 · 模式 · 上下文剩余% · 缓存命中% · 本会话 ¥。
- 输入框：Enter 发送、Shift+Enter/Ctrl+J 换行、↑↓ 历史、粘贴大段自动折叠成 [粘贴 120 行]。
- `/` 弹命令菜单，`@` 模糊搜文件插入路径，`!` 开头直接跑 shell。
- 斜杠命令：/new /resume /fork /compact /model（flash/pro）/permissions /plan /diff /undo /status /init（生成 AGENTS.md）/review（审当前改动）/copy /export /skills /mcp /quit。
- 审批：底部弹出选项列表，方向键选、数字键直达。

## 9. 记忆与技能（M4）
- 项目级：AGENTS.md（根到 cwd 分层，32KB 上限）；`/init` 自动生成。
- 用户级：~/.ee/AGENTS.md（个人偏好）+ ~/.ee/memory/MEMORY.md 索引（前 200 行常驻）+ 主题文件按需读；`remember` 工具写入。
- 技能：~/.ee/skills/*/SKILL.md，只常驻名字和描述，用到时追加正文。可直接读 `.claude/skills/` 兼容。
- 会话：~/.ee/sessions/*.jsonl，/resume 列表选择。

## 10. 里程碑与验收
- M1 核心（目标：能在真实项目里读、改、跑）：多模型层（openai_chat + 家族档案 + transforms + doctor） + core 循环 + 7 工具 + 权限三档 + 截断 + 缓存结构 + 基础 TUI（流式、工具单元、审批弹窗、状态行）+ exec 模式。验收：让它在一个小仓库里修一个 bug 并跑通测试；长会话命中率 >90%。
- M2 稳定：AGENTS.md、压缩/prune、死循环检测、坏调用兜底、重试、web_search/web_fetch、/undo。
- M3 子代理：task（explore/general/fork）、/plan 模式、证据门、/review。
- M4 记忆与资料：会话恢复、长期记忆、技能、PDF/docx 读取。
- M5：MCP 客户端、flash/pro 自动路由、/side 侧聊、可选网页界面。

## 11. Git 与开发工具（2026-10-25 用户追加）
- M1：检测仓库，底栏显示分支+是否有改动；git 状态走增量附录；只读 git 命令免问，commit/checkout/merge/rebase 等询问，push --force / reset --hard / clean -fd 拦截；/diff 彩色；gh CLI 可用时告知模型（写操作询问）。
- M2：/commit（模型按 diff 写提交信息，确认后提交）、/review（审查当前改动或某个分支/PR）、/undo 与 git 协同。
- M3：子代理可在 git worktree 里并行改代码（Codex /worktree 做法），完成后合并回主分支前给你看 diff。
- 平台：Windows 首要，PowerShell 默认 shell。

## 12. 桌面端（2026-10-25 用户更正：一直要的是桌面端，不是 CLI）
参照 Codex 桌面版（Electron + React 19 + Tailwind + Radix + CodeMirror/Shiki + xterm.js/node-pty；界面通过 app-server 协议驱动 Codex 内核）。
- 内核复用 M1 的 ee core：新增 `ee serve`（本地 app-server，WebSocket JSON-RPC：thread/start、turn/start、interrupt、approval/respond、settings/get|set、事件推送）。终端版 TUI 已于 2026-10-26 删除（用户只要桌面端）。
- 外壳：Electron + React + TypeScript + Tailwind + Radix + Shiki + xterm.js（提议，待用户确认，备选 Tauri）。Python 内核用 PyInstaller 打成 sidecar，Electron 启动时拉起。
- 布局三栏：左 项目/会话侧栏（新建、搜索、底部设置按钮）；中 对话（消息流、工具卡片折叠、计划清单、审批卡片、底部输入框带模型选择/权限模式/附件/@文件）；右 可收起面板：改动审阅（按文件 diff、撤销/暂存）、终端、文件预览。
- 设置页：模型与供应商（base_url、key 变量名、单价）、默认 shell、权限模式、主题、快捷键。
- 打包：GitHub Actions windows-latest 构建安装包（用户 GitHub 已连接）。


### 状态（2026-10-26）
- [x] ee serve 后端 + 测试；桌面 UI 三栏 + 设置页 + 审批 + 改动审阅
- [x] 真实 Electron 窗口验证、Windows 打包（PyInstaller + electron-builder + GitHub Actions）
- [x] D2：右栏终端（xterm.js + node-pty）、文件预览、/compact、AGENTS.md、防死循环、网页搜索、子代理、记忆（Windows CI 全绿）
- [x] D2 网页搜索（2026-10-26）：ee/ee/tools/web.py（web_search 后端 exa（默认，免费无 key，Exa 公共 MCP 端点 https://mcp.exa.ai/mcp，同 OpenCode）|duckduckgo（免费，抓 html.duckduckgo.com）|deepseek（付费，2026-11-01 起 $0.75/千次）|tavily|brave|off；主后端出错按 search_fallback（默认 ["duckduckgo"]）回退，全失败才报错；web_fetch 默认 direct（免费直连），可选 auto|deepseek|off，DeepSeek Fetch 失败自动直连；HTML→markdown 文本、截断可 offset 续读；默认 20s 超时；代理跟随 [network]），配置 [web]，设置页「工具与记忆」，界面搜索结果/抓取卡片（desktop/src/components/AgentItems.tsx），测试 tests/test_web.py（MockTransport，无真实请求）。Exa 已对真实服务实测（2026-10-26）；DuckDuckGo 解析只用 mock 验证（沙箱 IP 被 DDG 限流返回 202）。未做：Brave 接口没对真实服务验证、真实 DeepSeek Search 未实测。
- [x] D2 子代理（2026-10-26）：ee/ee/core/subagent.py + ee/ee/tools/agent.py（task 工具：explore 只读 / general 继承权限 / 自定义 agents/*.md；max_steps、model 可选；不嵌套），事件 SubagentStart/Update/End（core/events.py），server 挂到 task 条目 meta.subagent（threads.py），审批冒泡 call_id=父/子，中断传递，/undo 共享快照，用量并入主会话；界面子代理卡片；测试 tests/test_subagent.py。剩：多个 task 并行（现在按顺序）、fork 式子代理（复制父前缀复用缓存）、git worktree 隔离。
- [x] D2 记忆（2026-10-26）：ee/ee/core/memory.py + ee/ee/tools/memory.py（add/list/remove；user=~/.ee/memory.md，workspace=<项目根>/.ee/memory.md），新会话开始时注入为 AGENTS.md 后的固定 user 消息（_kind=agents，压缩保留，8KiB 上限，会话内不变），配置 [memory]，RPC memory/get|add|remove|save（server/memory_rpc.py），设置页开关 + 文本编辑；测试 tests/test_memory.py、tests/test_server_d2.py。剩：主题文件按需读（§9 的 MEMORY.md 索引方案未做，现在是单文件）。
- [x] D3-C 配置迁移 + 单价（2026-10-26）：ee/ee/core/migrate.py 版本链（config_version，缺省=1），load_user_file/load_config 读前迁移，逐键 write_setting 写回保留注释，原文件备份 config.toml.pre-v2.bak；v2：web.search_backend "deepseek"→"exa"、fetch_backend "auto"→"direct"；settings/set 写出时带 config_version，所以之后用户主动选 deepseek 不会被改回。设置页加「出错时改用」(web.search_fallback)。Profile 加 variants（按模型名覆盖），deepseek-v4-pro 单价 0.30/9/27 元每百万。测试 tests/test_migrate.py。
- [x] D3-B 斜杠命令与自定义提示（2026-10-26）：core/prompts.py + server/prompts_rpc.py，前端 lib/slash.ts + SlashMenu；内置 /init。
- [ ] D3 其余：A MCP、D 附件、E 分叉/编辑重发、F key 安全存储（方案见 research/d3-plan.md）
