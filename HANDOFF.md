# 接手说明（给接手的 AI / 另一个账号）

最后更新：2026-10-26 17:10（Asia/Shanghai，D4-5 桌面通知 + 托盘完成，待 Windows CI；下一步 D4-6 后台长命令）

> **接手第一步（2026-10-26 17:10）**：D4-5 已推送，先确认 Windows CI 绿（失败就修）；然后做 D4-6 后台长命令。每个功能先对比 OpenCode/Codex/Cline/Aider/Goose/Continue 的做法，HANDOFF 写参考来源。
> 2026-10-26 16:45 起由 Hark 项目「AI Agent 工作台」接手（同一 GitHub 账号 shhjjs111），用户要求火力全开按本文件路线推进、有问题就改。

> 2026-10-26 14:10 起由新账号（GitHub shhjjs111 的 Hark）接手。接手者：先读最下面「D3 进度」，从第一个 [ ] 继续。

## 用户要什么
- 用户 ee 自用的 AI 编程 Agent，**只要 Windows 桌面端，不要终端版**。UI 照 OpenAI Codex 桌面版（三栏：左项目/对话、中对话、右改动审阅）。
- 用途：构建项目、写代码、做作业、整理资料。
- **只实现 OpenAI Chat Completions 协议**，多模型通用（DeepSeek 默认，国产模型 + 中转站 GPT）。其他协议用户自己用网关转。
- 中文界面、中文交流。不基于用户的 Hunter1 项目。
- 绝不向用户索要 API key；key 从设置页或环境变量 DEEPSEEK_API_KEY 来。

## 架构
- `ee/`：Python 内核。`ee serve` = 本地 WebSocket JSON-RPC 服务（127.0.0.1 + token），启动打印 `EE_SERVE_READY {"port":N}`。方法：app/info、thread/*、turn/start|interrupt、approval/respond、settings/get|set、models/list、workspace/*、files/search、git/diff、git/revert_file。事件：item/started|delta|updated|removed、turn/*、thread/updated|usage。
- `desktop/`：Electron + Vite + React 19 + TS + Tailwind 4 + Radix + Shiki。main 进程拉起后端（开发：`python -m ee serve`，可用 EE_PYTHON；打包后：resources/backend/ee-backend/ee-backend.exe）。
- `desktop/scripts/fake_model_server.py`：假模型服务器，无 key 演示一回合。
- 设计细节：`research/design.md`（第 12 节是桌面端）；四份源码研究笔记在 `research/`。

## 现状（D1）
- 已完成：后端 + 135 个 pytest 通过；桌面 UI `npm run build` 通过，vitest 4 个通过；浏览器内截图检查过布局。
- 未完成（按顺序接着做）：
  1. [x] 真实 Electron 窗口验证通过（Xvfb，`work/electron_smoke.py` 在 Hark 工作区，不在仓库）。
  2. [x] 打包：`ee/packaging/ee-backend.spec` + `entry.py`；`desktop/scripts/build-backend.mjs`；package.json 里 electron-builder NSIS 配置（`npm run dist:win:full` 一键）；占位图标 `desktop/build/icon.*`；`.github/workflows/desktop-windows.yml`（push main 或手动触发，产出 artifact `ee-windows-installer`）。
  3. [x] README 已改写。
  4. [x] Actions 在 windows-latest 上测试全过并产出安装包（artifact ee-windows-installer，约 129MB）。首次 CI 发现并修复了 PowerShell CLIXML 问题（改用临时 .ps1 + -File）。
  5. [x] 用户已在 Windows 真机安装（装在 D:\ee）。
- D2：右栏终端（xterm.js + node-pty）和文件预览（现为占位）、上下文压缩 /compact、会话 fork、AGENTS.md 自动加载、防死循环、网页搜索（倾向 DeepSeek 自家 Search/Fetch API）、子代理、记忆。

## 当前进行中（2026-10-26 13:15，如果你是接手者，从这里继续）
**用户最高优先级 bug**：装了 D1 安装包，填了 API key，发“你好”完全没反应；点设置有时一直转圈。
- 用户的 `%APPDATA%\ee\logs\backend.log` 只有 3 次正常启动（`EE_SERVE_READY`），没有任何报错/退出 → 后端起来了，卡在启动之后，且出错没写日志。
- Linux 上源码版跑 `ee/scripts/e2e_ws.py`（拉后端 + 假模型，settings/get→set→workspace/open→thread/start→turn/start“你好”）0.5 秒正常完成 → 问题是 Windows/冻结 exe 特有。
- **根因已在 Windows CI 上复现（2026-10-26 13:10，分支 repro-win-hang，run 40272706672）**：`--exit-on-stdin-eof` 的线程阻塞读 stdin 管道时，git 子进程（继承了同一根 stdin 管道）在 Windows 上启动即卡死；`subprocess.run(timeout=10)` 超时后 Windows 实现还会无限期 `communicate()` → `thread/start`（同步跑 git status）把事件循环永久卡住 → “你好”没反应、设置转圈，且不会有任何日志。同一脚本 stdin=DEVNULL 或不读 stdin 时 0.1s 正常。复现脚本 `ee/scripts/win_stdin_repro.py`、`ee/scripts/e2e_ws.py`。
- 修复（已推送 main，**Windows CI 全绿：run 40274756186，commit 8a6ac93**，artifact ee-windows-installer 约 131MB）：
  - `ee/proc.py:run_capture`：所有短命令（git/rg/taskkill）stdin=DEVNULL、超时杀进程树、收尾有超时；asyncio 子进程也 stdin=DEVNULL。
  - `ee serve --exit-on-stdin-eof`：启动时 dup 出 stdin 私有副本只在它上面读 EOF，fd0/STD_INPUT_HANDLE 换成 NUL（第三方代码起子进程也不会再继承那根管道）。
  - server：thread/start、thread/get、turn/start（首次建会话）、settings/*、models/list、workspace/*、thread/list、terminal/shell 全部 asyncio.to_thread；会话里 refresh_git / AGENTS.md 加载 / 模型客户端构造也挪到线程；通知广播线程安全。
  - 日志：后端 `ee-backend.log`（EE_LOG_DIR → %APPDATA%\ee\logs → ~/.ee/logs），记录 RPC 失败/慢 RPC、回合开始结束、模型请求开始/结束/异常 traceback、代理；事件循环卡住 ≥8s 自动把调用栈写进日志（`ee/log.py` LoopWatchdog）。界面侧 RPC 失败/重连/自动重启写 `backend.log`。
  - 模型请求（`ee/core/providers/net.py`）：连接 15s / 读取 90s / 重试 2 次（设置可改）；代理 跟随系统/直连/自定义，本机地址永远直连；失败给中文错误（含 base_url、HTTP 状态、走没走代理）；`turn/status` 事件显示“正在连接模型…/已连接，等待模型输出…”。
  - 桌面端：RPC 默认 20s 超时并显示错误；设置页加载失败显示错误+重试（原来 promise 没 catch → 永远转圈）；发送失败在对话里出错误条目、不再卡“正在工作”；WS 断开顶部提示，重连失败或内核退出自动重启内核（2 分钟内最多 3 次）。
  - CI：源码后端 e2e、打包 exe `selftest`、打包 exe e2e（假模型）、打包 exe 黑洞端点 e2e（必须在超时内报错）。
- CI 第 1 轮（run 40274051359）失败：d2-tools 的 web 工具 `import httpx`，但新版 openai 改用 httpx2、不再带 httpx → 已在 pyproject 加 `httpx>=0.27`。
- CI 第 2 轮（run 40274331756）失败 3 个测试，均为 Windows 特有：拒绝连接在 Windows 上表现为 2 秒连接超时（测试放宽）；test_server_files 用 write_text 在 Windows 写出 \r\n（改 write_bytes）。CI 各步加 `if: !cancelled()`，一轮能看到所有失败。
- CI 第 3 轮（run 40274756186）全部通过：pytest 201 过 / 2 跳过（POSIX 专用）；源码后端 e2e、打包 exe selftest、打包 exe e2e、黑洞端点 12.9s 内报“读取超时”错误、node-pty smoke 和安装包内 node-pty 检查都过。
- 下一步：用户决定攒功能再出包；需要时让用户装 run 40274756186（或之后的）安装包，真机验证“你好”能回复；如仍有问题，让用户发 `%APPDATA%\ee\logs\ee-backend.log` 和 `backend.log`（看门狗会写卡住位置）。
- 已知限制（本次相关）：系统代理只认环境变量和注册表 ProxyServer，不支持 PAC 自动配置脚本（用 PAC 的请在设置里填自定义代理）；只支持 http/https 代理；分支 `repro-win-hang` 保留为根因证据，可删。
- 已核实没问题：默认模型 `deepseek-flash`（官方当前名，2026-10 文档）→ provider deepseek → 设置页存的 key；settings/set 后已有会话立即用新 key（有测试）。
- **用户决定（2026-10-26）：不急着出新安装包，先多推进功能，攒一起再出包。**

**D2 进度**
- [x] 上下文压缩 `ee/core/compaction.py`（自动约 80% + 手动）、`thread/compact` RPC、UI（点上下文圆环或输入 /compact）。
- [x] AGENTS.md/CLAUDE.md 自动加载 `ee/core/instructions.py`（~/.ee/AGENTS.md + 工作区根到 cwd，32KiB 上限），UI 头部芯片。
- [x] 防死循环 `ee/core/loopguard.py`，TurnDone reason="loop"，UI 显示“检测到重复操作，已停止”。
- [x] 右栏真终端：Electron main 里 node-pty 1.1.0 + xterm.js，多标签，跟随 shell 设置；node-pty 打包到 resources/node-pty（asar 外），npmRebuild=false；CI 加了 pty smoke 和安装包内 node-pty 检查（待 Windows CI 确认）。
- [x] 文件树/预览：`files/tree`、`files/read` RPC（限制在工作区内），Shiki 高亮、Markdown 渲染、图片；工具卡片里的路径可点击打开。右栏可拖宽。
- [x] Windows 无响应 bug 修复（见上；等 Windows CI 全绿后再出包，用户真机待验证）。
- [x] 网页搜索/抓取 `ee/ee/tools/web.py`：DeepSeek `POST https://api.deepseek.com/v1/tools/search`（query≤128 字，count 1–50）与 `/v1/tools/fetch`，Bearer 复用 deepseek key；各 $0.75/千次，北京时间 2026-11-01 起收费。后端可选 DeepSeek/Tavily/Brave/关闭；抓取 自动/DeepSeek/直接 httpx/关闭；20s 超时。仅 mock 测试，真实接口未调。
- [x] 子代理 `task` 工具（`ee/ee/core/subagent.py`、`tools/agent.py`）：explore 只读 / general 继承权限 / agents/*.md 自定义；独立上下文、不可嵌套、审批冒泡、中断联动、/undo 可撤；UI 子代理卡片 `desktop/src/components/AgentItems.tsx`。多个 task 目前串行。
- [x] 记忆 `ee/ee/core/memory.py`、`tools/memory.py`、RPC `memory/get|add|remove|save`：~/.ee/memory.md + <项目>/.ee/memory.md，新会话开始时固定注入（AGENTS.md 之后，8KiB 上限），会话内不变以保前缀缓存。
- [x] 设置页「工具与记忆」栏 `AgentSettings.tsx`。pytest 203 通过（干净 HOME），npm build/test 通过。
- 上面 [x] 的代码在 Hark 工作区已完成，pytest 约 168 个通过、npm run build 通过；会和 bug 修复一起推送到本仓库（若你看到仓库里还没有，说明尚未推送）。
- 之后：所有功能稳定 → Windows CI 全绿 → 出新安装包给用户。

## 已知限制
- 改动审阅需要 Git。设置页保存会重写 `~/.ee/config.toml`（注释丢失，旧文件存为 .bak）；设置页里填的 key 是明文。
- 非 DeepSeek 模型的单价需在设置里填。DeepSeek 价格（2026-10-25 官网）：flash 高峰 命中 0.04 / 未命中 2 / 输出 8 元每百万 token；pro 为 0.30 / 9 / 27。
- 真实 Windows 上出现过无响应 bug（已修复并在 Windows CI 用打包 exe 验证）；用户真机 + 真实 API key 回合尚待验证。
- 安装包未签名，SmartScreen 需“更多信息 → 仍要运行”。

## 约定
- 每完成一步（甚至进行中）：更新本文件、README、design.md 状态，提交推送到本仓库 main。用户可能随时换账号接手，本文件必须始终反映最新进度。
- 推送会触发 Windows Actions，产出 artifact `ee-windows-installer`。

## 最新进度追加

- [x] 2026-10-26 13:43 免费搜索：默认 web_search 后端改为 Exa 公共 MCP（https://mcp.exa.ai/mcp，免 key，已实测可用），失败回退 DuckDuckGo HTML（仅 mock 测试，沙箱 IP 被限流）；DeepSeek 搜索改为可选付费项；web_fetch 默认直接 httpx 抓取。待办：老用户 config.toml 里已存 search_backend="deepseek" 的需迁移；search_fallback 设置页暂无选项。
- 用户决定：不急出新安装包，继续推进功能后统一出包。
- 2026-10-26 13:50 D3 方案定稿：research/d3-plan.md（A MCP、B 斜杠命令、C 配置迁移+单价、D 附件、E 分叉/编辑重发）。尚未写代码。API key 安全存储方案见 d3-plan.md F 节，未实现。2026-10-26 13:51 Hark 当日额度用尽（次日 12:17 恢复），D3 全部未写代码，接手从 C 节开始。

## D3 进度（接手从第一个 [ ] 开始；每项做完都提交推送并在此打勾）
本地开发：`cd ee && pip install -e . pytest && python -m pytest -q`；`cd desktop && npm ci --ignore-scripts && npx tsc --noEmit -p tsconfig.json && npx vitest run`。
- [x] C 配置迁移 + 单价（2026-10-26 14:30）：`ee/ee/core/migrate.py`（config_version 版本链；v2 = search_backend deepseek→exa、fetch_backend auto→direct；逐键写回保留注释；备份 `config.toml.pre-v2.bak`；坏文件/写失败只记日志）。`load_user_file`/`load_config` 读用户文件前迁移；`settings/set` 写出时带 `config_version`。设置 `web.search_fallback` 读写 + 设置页「出错时改用」。`Profile.variants` + `profile_for` 按模型名匹配，deepseek-v4-pro 单价 0.30/9/27。测试 `tests/test_migrate.py`（pytest 220 通过）。
  - 注意：`.pre-v2.bak` 里可能有明文 key（与原 config 相同）；F 节实现时要一起抹掉。flash 是否支持图像仍待查官方文档（D 节要用 vision）。
- [x] B 斜杠命令与自定义提示（2026-10-26 14:50）：后端 `ee/ee/core/prompts.py`（内置 `core/builtin_prompts/*.md` ← `~/.ee/prompts/*.md` ← `<项目根>/.ee/prompts/*.md`，frontmatter description/argument-hint，`$ARGUMENTS` 替换，无占位符则追加；保留名不可覆盖）+ `server/prompts_rpc.py`（prompts/list、prompts/expand）；内置 /init 生成 AGENTS.md。前端 `desktop/src/lib/slash.ts`（纯函数 + vitest）、`components/SlashMenu.tsx`；Composer：/compact /new /undo /model <名> /mode 只读|默认|全自动（二级菜单）/memory [文字]（无文字打开设置）/mcp（暂时打开「工具与记忆」，A 做完改成 MCP 分区，代码里有 TODO(D3-A)）+ 自定义提示（展开后作为用户消息发送）；store 加 `undo()`、`openSettings(section)`、`settingsSection`。未知 /xxx 当普通文字发送。测试 tests/test_prompts.py、src/lib/slash.test.ts（pytest 223、vitest 9 通过，vite build 通过）。
- [x] E 分叉 / 编辑重发（2026-10-26 15:15）：`ee/ee/server/branch.py`（RPC `thread/fork {thread_id,item_id}`→{thread,draft}、`thread/edit_resend {thread_id,item_id,text,revert_files=true,model?,mode?}`）。回合开始时用户条目记 `hist`（history 下标）和 `snap`（快照层号）（threads.py `_mark_turn_start`）；用前用 history[hist] 原文核对（压缩过/老会话报错）。Session 加 `user_text_at`、`truncate_to`、`revert_files_from(layer)`（倒序逐层恢复）；store 新增 `truncate {from_seq}` 记录；重启后从条目 hist 重建 `turn_starts`。快照只在内存：重启后编辑重发只截历史、出提示不动文件。分叉复制历史+条目（+ sessions/<id>/attachments，给 D 用），meta.parent_id，summary 带 parent_id；thread/delete 连同 sessions/<id>/ 目录删除。前端：用户气泡悬停出「编辑」「分叉」，内联编辑框；store `forkFrom`/`editResend`/`composerDraft`（分叉后预填输入框）；左栏分叉图标。测试 tests/test_branch.py（pytest 226 通过）。
  - 已知：/undo 弹出快照层后层号会错位（编辑重发可能恢复错层），概率低，未处理；编辑排队中插入的消息不支持。
- [x] D 图片附件（2026-10-26）：`ee/ee/core/attachments.py`（按文件头识别 JPEG/PNG/GIF/WebP；每条 ≤8 张、单张 ≤20MiB；存 `~/.ee/sessions/<id>/attachments/`）；turn/start 加 `attachments:[{name,data(base64),thumb}]`，可只发图；历史用户消息带 `_images`（路径），`transforms.wire_messages` 发送时 vision=true 转 `image_url` data URL 分块，vision=false 降级为文字说明 + 界面提示条目；用户条目带 attachments 元数据（缩略图）。DeepSeek 官方 Vision 文档（2026-10）：deepseek-flash 支持图片、deepseek-v4-pro 不支持 → deepseek.toml vision=true，pro variant vision=false。设置：模型可设「看图 自动/支持/不支持」（MODEL_KEYS 加 vision），models/list 返回 vision。分叉时 `_images` 路径换到新会话目录并复制附件。前端 `lib/attachments.ts`（canvas 压缩：长边≤2048，PNG>1.5MB 转 JPEG，GIF 原样，160px 缩略图）；Composer 粘贴/拖放/按钮加图、托盘可删、不支持看图时提示；气泡显示缩略图。测试 tests/test_attachments.py（pytest 231、vitest 9、vite build 通过）。
  - 已知：编辑重发只带新文字，原消息的图片不会重发；运行中不能追加图片（后端拒绝并提示）。
- [x] A MCP（后端 2026-10-26 14:40，界面 2026-10-26 15:10）：`ee/ee/mcp/`（config：[mcp] + 项目 .mcp.json，未允许的项目服务器不连、允许记 name:sha8；client：stdio（stdin PIPE、stderr 留尾 50 行、Windows .cmd 走 shell）/ streamable HTTP（JSON 或 SSE 响应、Mcp-Session-Id）/ 旧式 SSE，本机地址不走代理，应答服务器 ping/roots/list；schema：$ref/allOf/anyOf/type 数组/const 等规范化；manager：每事件循环一个，按配置指纹缓存连接，并行连接，断线重连一次，test 独立连接；tool：mcp__<srv>__<tool>，图片/音频存 tool-output/mcp-images/）。Session `_attach_mcp`（首次冻结前缀前连接，工具追加末尾，set_model 时保留，失败/未允许出提示）；子代理沿用父会话 MCP 工具（定义了 tools 的不带）；permissions `_check_mcp`（deny→只读模式仅 readOnlyHint 工具→ask 规则→auto/trusted/allow 规则 mcp__srv__* 通配/会话“以后都行”）并修了未知工具“以后都行”不生效；RPC `mcp/list`、`mcp/test`（server/mcp_rpc.py）；settings `mcp` 读写（_mcp_out/_mcp_in，校验名字）；serve 退出关闭 MCP 子进程。测试 tests/test_mcp.py + tests/fake_mcp_server.py（pytest 240 通过）。
  - 界面（完成）：设置页「MCP」分区 `desktop/src/components/McpSettings.tsx`：总开关 / 读 .mcp.json / 两个超时；「我的服务器」增删改（本地命令：command、参数（空格分隔、引号包空格）、工作目录、环境变量 KEY=VAL 每行；远程：URL、传输 自动/Streamable HTTP/旧式 SSE、请求头 名称: 值 每行）、启用、信任、测试连接（用未保存的草稿配置，显示状态点、错误、工具列表含只读标记）；「项目服务器」列出 .mcp.json 里的服务器，允许/撤销（写 mcp.allowed_project，保存后生效），允许后可测试；mcp/list 的配置错误单独列出。纯函数 `desktop/src/lib/mcp.ts`（parseArgs/formatArgs/parseKv/formatKv）+ vitest。Composer /mcp 直接打开 MCP 分区。工具卡片 meta.kind="mcp" 显示「调用了 <工具>（MCP · 服务器）」。vitest 13、tsc、vite build、pytest 240 通过。
  - 已知：项目服务器点「允许」后要先保存才能测试；MCP 的 env/headers 里的 token 仍是 config.toml 明文（F 只管模型和搜索 key，建议用 ${环境变量} 引用）。
- [x] F API key 安全存储（2026-10-26 15:12）：`ee/ee/core/secrets.py`。后端：Windows 凭据管理器（ctypes 调 CredWriteW/CredReadW/CredDeleteW，GENERIC，目标名 `ee/provider/<名>`、`ee/web/<tavily|brave|exa|deepseek>`）；不可用时回退 `~/.ee/credentials.json`（Windows 值用 DPAPI 加密写成 `dpapi:<base64>`；其他平台 keyring 优先，否则 0600 明文文件）；`EE_SECRETS_STORE=auto|wincred|keyring|file|memory` 可强制。API：`get/put/delete`（get 出错返回 None 并记日志，put 失败抛 SecretStoreError）、`store()/use()/info()`、`provider_key_name/web_key_name`、`plain_keys/move_plain_keys(data, overwrite)`、`strip_secret_lines/scrub_backups`、`redact/RedactFilter/remember_env_keys`。
  - 读取优先级：凭据存储 > 旧 config.toml 明文 > 环境变量（`ModelSpec.key_source()` 返回来源；`tools/web.py` 同序，web 专用 key 优先于 deepseek 供应商 key）。与 d3-plan 原写的「环境变量优先」不同：沿用 D1 以来「设置页填的优先」，避免残留的旧环境变量悄悄盖掉设置页的 key；`tests/test_secrets.py::test_key_priority` 锁定此顺序。
  - 迁移：config 版本 v3（core/migrate.py `_v3`）把 `providers.*.api_key` 与 `web.*_api_key` 挪进凭据存储并删掉对应行（保留注释和其他键）；存储里已有值不被旧明文覆盖；存不进去的保留明文照常可用。`save_user_file` 与迁移写的 `.bak` / `.pre-v*.bak` 都去掉 key 行；`serve` 启动时 `scrub_backups` 清理已有备份。
  - 设置：settings/get 返回 `api_key_source`（store/plain/env）+ 末四位 + `secrets`（backend/label/secure），绝不回传明文；set 先校验，再删除被清除/被删供应商的 key，再把新 key 存进凭据存储，存不进去报错且不写文件。前端供应商和搜索 key 显示来源，占位提示「保存在 Windows 凭据管理器」。
  - 脱敏：日志 handler 挂 RedactFilter（含异常堆栈），SessionStore.append、session._friendly_error、net.describe_error、RPC 错误统一 redact（已知 key 值 + `sk-…`/`tvly-…`/`Bearer …`/`api_key=…`）。
  - 自检：`ee-backend.exe selftest --secrets`（凭据管理器写读删 + DPAPI 往返），Windows workflow 已改为带此参数。测试 `tests/test_secrets.py`（Windows 上额外跑真实凭据管理器/DPAPI）；pytest 252 通过 2 跳过（Windows 专用），vitest 13、tsc、vite build 通过。
  - Windows CI（2caf068）：真实凭据管理器/DPAPI 测试与打包 exe `selftest --secrets`（wincred、dpapi ok）均通过；唯一失败是迁移备份按文本写导致 CRLF 变 CR CR LF，已改为按字节写。
  - 已知：bash 输出里的 key（如 `echo $DEEPSEEK_API_KEY`）仍会进模型上下文；MCP env/headers 里的 token 仍是明文；项目级 `.ee/config.toml` 里的明文 key 不迁移。
- [ ] 全部完成后：Windows CI 全绿 → 告诉用户装新安装包验证

## 借鉴其他 agent 的原则（用户 2026-10-26 授权）
- 用户 2026-10-26 15:24/15:29 明确：功能先参考大厂（OpenCode、Codex 等开源实现）当前源码的成熟做法，拿来后按 ee 的架构适配（Python 后端、JSON-RPC、Windows、条目持久化等），不要原样照搬，也不要另起炉灶。能直接抄的直接抄，不适配的改到适配（15:31）。目标是集百家之长（15:34）：每个功能先对比几家（OpenCode、Codex、Cline、Aider、Goose、Continue 等开源实现，闭源的 Claude Code、Cursor 只借思路）各自怎么做，取最好的那家或组合各家优点，再适配 ee；在 HANDOFF 该项下写一句“参考了谁、取了哪点”。
- 可以直接拿开源 agent 的好代码改编：Codex（Apache-2.0）、OpenCode（MIT）、Cline、Aider、Goose、Continue（均 Apache-2.0）等。用户说仅自己学习使用：代码、文档里都不写出处，也不要第三方声明文件（用户 2026-10-26 明确要求清理已有的）。
- Claude Code、Cursor 等闭源产品只借思路，不抄代码。

## D4–D6 路线（2026-10-26 15:25 按 research/parity-audit.md 和用户要求重排；★=用户点名或原计划项；D3-F 完成后从 D4-1 开始）
对照审计 research/parity-audit.md：95 项，已有 23、部分 19、缺 53，每项有价值/难度/实现要点/参考实现路径。用户要求：磁盘快照仍排第一；/review、/commit、Plan 模式、会话搜索这些小活前移；加入桌面通知+托盘、后台长命令、PDF/Word/Excel 读取；LSP 推到 D6。

**D4 — 地基 + 小而高价值**（按顺序）
1. [x] ★磁盘快照（2026-10-26 15:40）：影子 git 仓库替换内存快照，每回合前后快照工作区（含 shell 改的文件），重启后 /undo、编辑重发仍能恢复文件
   - 做法：参考 OpenCode 的 snapshot（独立 git 目录 + work-tree 指向工作区、write-tree 得到内容寻址的 tree、按差异文件恢复），按 ee 适配：tree 记在用户条目上持久化、/undo 按回合栈逐个撤销、编辑重发恢复到该消息之前、非 git 项目也能用、失败回退内存快照。
   - `ee/ee/core/snapshot.py`：`DiskSnapshot(worktree, home)`，git 目录 `~/.ee/snapshot/<工作区真实路径 sha1 前 16 位>`（里面有 ee-worktree.txt 记录对应目录）；git 调用一律带 `core.autocrlf=false`（字节原样，CRLF 不变）、`core.quotepath=false`、`core.longpaths=true`、禁用 hooks，环境清掉 GIT_*。`capture()`=`add -A`+`write-tree`（60 秒超时，超时后本次运行停用）；`changed(a,b)`=`diff --name-only -z --no-renames`；`restore(tree, paths)`=树里有的 `checkout -f tree -- 路径`，没有的删除（并清理空目录），只动给定文件。排除：工作区 .gitignore + 项目 .git/info/exclude；非 git 项目默认排除 node_modules/.venv/__pycache__ 等；未跟踪且 >2MB 的文件写进影子仓库 info/exclude。工作区是用户目录或盘根、没装 git 时不可用。`for_dir()` 同目录共用实例并加锁。
   - Session：`turn_trees`（每回合 before/after/undone）、`disk()`、`disk_capture()`；回合开始在发 TurnStart 前抓 before（TurnStart.tree），`run_turn` 收到 TurnDone 时抓 after（TurnDone.tree_after）。`undo()` 先走磁盘：从最近的未撤销回合往前找第一个有差异的，恢复后标 undone（`last_undo_tree`）；磁盘不可用才用旧内存快照。`revert_trees(entries)` 给编辑重发用。恢复后清内存快照层和已读缓存，并给模型加系统提示。子代理 `disk_enabled=False`（改动算在主会话这一回合里）。
   - threads：用户条目记 `tree`、`tree_after`、`undone`（及 `snap_epoch`）；重启时从条目重建 `turn_trees`。app `thread/undo` 撤销后 `mark_undone`。branch `edit_resend` 有 tree 优先磁盘恢复，否则旧内存快照；修复：旧内存快照层号只在同一进程（`snap_epoch` 相同）才用，之前重启后层号 0 会误报“已恢复”。
   - 配置：`[snapshot] disk = false` 可关闭（回退内存快照）。测试 `tests/test_snapshot.py`（7 个：增删改/中文路径、CRLF 字节、.gitignore 与大文件、非 git 默认排除、用户目录不可用、shell 改动可撤销且重启后可用不重复、用户手改的其他文件不受影响），`test_branch.py` 改为重启后也能恢复 + 新增关闭磁盘快照的回退用例；pytest 260 通过 2 跳过。
   - 已知/后续：影子仓库不自动 gc（gc.auto=0），长期使用会变大，D5 检查点时间线时加清理；大仓库每回合 `add -A` 有开销（同 OpenCode）；/undo 后层号错位的旧问题在磁盘模式下不再存在，内存回退模式仍有；暂无设置页开关（只能改 config.toml）。
2. [x] ★/review 代码审查命令 + /commit（2026-10-26 16:05）
   - 参考了谁、取了哪点：审查对象四选一（未提交 / 相对基准分支取 merge-base / 某提交 / 自定义要求）、P0–P3 分级、“只报这次改动引入的、可证明、作者会改的问题”的评审标准、末尾结构化 JSON（overall + findings 带文件行号和把握度）取自 Codex 的 review；审查在主会话里跑（审完可直接说“修掉”）、命令可带额外要求取自 OpenCode / Claude Code 的 /review；“修复选中的问题”按钮取自 Cursor Bugbot 的一键修思路；提交信息“有暂存只提交暂存、否则全部暂存”、跟随仓库最近提交风格取自 Aider 的 /commit；改动面板里生成→可编辑→确认提交的流程取自 Cline/VS Code 的源代码管理面板。
   - 后端：`core/review.py`（targets 列分支/最近 20 个提交/推荐基准 origin/HEAD→main/master；collect 取 diff，ref 校验防参数注入；build_prompt 拼评审标准 + diff，超 80k 字符截断并让模型自己用 git 看剩余；parse_findings 解析 ```ee-review 块）；`prompts/review.md` 评审标准、`prompts/commit.md` 提交信息要求；`server/review_rpc.py`：`review/targets`、`review/start`（回合强制只读，结束恢复原模式；界面显示 “/review 未提交的改动：要求”，模型收到完整提示；用户条目 kind=review，不能编辑重发/分叉）、`git/commit_message`（有会话用会话的模型，否则临时建会话）、`git/commit`。threads.start_turn 新增 display/readonly/kind 参数。/review、/commit 加入保留名，自定义提示不能覆盖。
   - 前端：`components/ReviewDialogs.tsx`（审查弹窗：四种对象 + 分支/提交下拉 + 额外要求；提交弹窗：自动生成、可改、重新生成、Ctrl+Enter 提交、显示要提交的文件；ReviewCard：总评 + P0–P3 列表、点路径打开文件、展开说明、勾选后“修复选中的 N 个”按当前权限模式发给模型）；`lib/review.ts` 解析/拆块/修复消息（流式中未闭合的块也隐藏）；斜杠菜单加 /review [要求]、/commit；改动审阅栏加「审查」「提交」两个按钮。
   - 测试：`tests/test_review.py` 6 个（对象与 diff、错误、解析、只读回合拦写且恢复模式、生成并提交、只提交暂存）；`src/lib/review.test.ts` 4 个；pytest 266 通过 2 跳过，vitest 17 通过。
   - 已知/后续：审查提示（含 diff）进入主会话历史，占上下文（靠自动压缩）；PR 审查（gh pr diff）未做；审查进行中插入的消息也在只读下执行。
3. [x] ★Plan 模式（2026-10-26 16:25）：只读出方案 → 点「按此执行」切到默认/全自动继续
   - 参考了谁、取了哪点：计划模式作为第四种权限模式、Shift+Tab 轮换模式、模型用专门工具提交方案后停下等批准取自 Claude Code（ExitPlanMode）；“方案工具调用即结束回合、用户回复即修改意见”取自 Cline 的 plan_mode_respond；计划模式权限 = 只读（写文件/非只读命令/改记忆一律拒）取自 OpenCode 的 plan agent；批准时选“默认/全自动”再执行取自 Claude Code 的批准选项；子代理在计划模式下强制只读沿用 ee 现有规则。
   - 后端：permissions `MODES` 加 `plan`（标签“计划”，`READONLY_MODES` 统一判只读）；context `PLAN_NOTE` 在计划模式时写进 <permissions> 环境块（切模式时随增量环境信息告诉模型）；新工具 `tools/plan.py` `present_plan{title?, plan}`（非计划模式调用报错；成功时 meta `kind=plan_proposal`、`end_turn`），session 在一步工具执行完后看到 end_turn 且没有排队消息就直接结束回合（不再请求模型）；子代理在 plan 下只读。
   - 前端：模式下拉加“计划”，输入框 Shift+Tab 轮换 默认→全自动→计划→只读；`/plan [任务]` 切到计划模式（带任务就直接发）；`/mode 计划`；PlanProposalCard（方案 markdown + 「按此执行（默认模式）」「按此执行（全自动）」，之后有新消息就显示“已处理”）；设置页默认模式可选计划。
   - 测试：`tests/test_plan.py` 3 个（权限、完整流程：写被拒→提交方案后不再请求模型→批准切默认后能写且模型看到模式变化、非计划模式报错继续）；pytest 269 通过 2 跳过，vitest 17 通过。
   - 已知/后续：方案不单独存文件（在会话里）；ask_user 结构化提问留到 D4-8。
4. [x] ★会话全文搜索（2026-10-26 16:45）：左栏搜索同时匹配标题和对话内容，内容命中在会话下显示片段
   - 参考了谁、取了哪点：持久化的会话索引 + 按会话文件增量更新的思路取自 Codex（rollout 搜索与 session index）；命中会话下直接显示匹配片段取自 Cline 任务历史搜索的展示方式；中文用 SQLite FTS5 trigram 分词（不用装分词器），两字以下退回 LIKE，是 ee 自己的组合。OpenCode/Continue/Aider/Goose 只有标题或无内容搜索，没取。
   - 与原计划的偏差：没在 store.append 热路径里写库，改为搜索前按会话文件 mtime+size 增量重建变化的会话——每次流式写条目都不碰 SQLite，编辑重发截断/覆盖/外部删除都天然正确；个人用量下重建单个会话是毫秒级。
   - 后端：`core/search_index.py`（~/.ee/index/search.db；docs 表一条目一行：用户消息、助手回复、工具摘要+输出(截 2 万字)、会话标题；files 表记 mtime/size；FTS5 不可用时退回普通表 + LIKE；snippet 取命中处前后 36 字）；RPC `thread/search{query, cwd?, limit?}` → `{results: [{thread, hits: [{item_id, kind, seq, snippet}]}]}`，按 bm25 排序，每会话最多 3 条；`thread/delete` 同步删索引。
   - 前端：Sidebar 搜索框停顿 250ms 调 thread/search，标题命中或内容命中都列出，内容命中的会话下显示一行片段（点击打开会话），搜索中显示转圈。
   - 测试：`tests/test_search.py` 3 个（建索引/不变不重建/英文 FTS/中文 trigram/两字 LIKE/工具输出/追加增量/截断后不命中/删除移除、snippet、RPC 含 cwd 过滤与删除）；pytest 272 通过 2 跳过，vitest 17 通过。
   - 已知/后续：点片段只打开会话，还不会滚动定位到命中条目（item_id 已返回，可后续做）；搜索结果不高亮关键词。
5. [x] 桌面通知 + 托盘常驻（2026-10-26 17:10）：回合完成 / 出错 / 防死循环停下 / 需要审批时 Windows 通知（从不 / 窗口不在前台 / 总是）；关窗后托盘常驻（自动化前置）
   - 参考了谁、取了哪点：通知三档（从不 / 窗口不在前台 / 总是）与“回合完成 + 需要审批”两类提醒取自 Codex 桌面版的通知设置；托盘常驻、托盘菜单（打开 / 新对话 / 设置 / 退出）、Electron Tray + Notification 的写法取自 Goose 桌面端（同为 Electron）；需要审批时闪烁任务栏取自 Cline/VS Code 的“需要注意”提示思路；关窗首次弹“仍在后台运行”提示取自常见 Windows 托盘应用（Slack/Discord）。OpenCode/Aider 只有终端响铃，没取。
   - 后端：设置 `ui.notify`（never|unfocused|always，默认 unfocused）、`ui.notify_approval`（默认开）、`ui.close_to_tray`（默认开），settings/get|set 读写校验，choices 带 notify_modes。测试 `tests/test_ui_settings.py`。
   - 前端：`src/lib/notify.ts` 纯函数（turn/completed → 完成/出错/防死循环/到最大步数，用户中断不通知；thread/updated 状态变成 waiting_approval 时通知一次；正文取最后一条助手回复去 markdown 的开头，出错取错误信息）+ `notify.test.ts`；store 在收到事件时调 `window.ee.notify`，启动和保存设置时 `pushDesktopPrefs` 把设置交给主进程；App 监听点通知打开会话、托盘菜单动作，并把进行中/等审批的会话数报给托盘提示。设置页「常规 → 通知与后台」三项；`Toggle` 挪到 ui.tsx 共用。
   - Electron：`electron/tray.ts`（setAppUserModelId=com.ee.desktop 让 Windows 通知可用；托盘图标 resources/icon.ico；按设置和窗口焦点决定弹不弹；点通知 show+focus 并发 `app:openThread`；需要审批且不在前台时 flashFrame，聚焦时停；close_to_tray 时 × 改为 hide，第一次提示一次；托盘「退出」/before-quit 才真正退出并杀终端和后端）。main.ts 接入，second-instance 也能把隐藏的窗口拉出来；package.json extraResources 把 build/icon.ico、icon.png 带进 resources。
   - 已知/后续：没有托盘区的 Linux 桌面上关窗照常退出；通知点击后只打开会话，不定位到审批条目；托盘图标暂不随状态变色（D5 可加角标）；真机 Windows 通知效果待用户验证（CI 无法看到弹窗）。
6. [ ] 后台长命令：bash 工具可后台启动（dev server、watch、长测试），返回进程会话 id；新工具 shell_output（读增量输出）、shell_write（写 stdin，支持交互程序）、shell_kill；回合结束不杀，会话关闭时杀进程树；UI 列出后台命令
7. [ ] PDF / Word / Excel 读取：read 工具按扩展名分派 pypdf / python-docx / openpyxl，带页码/表名；打包 spec 加依赖
8. [ ] 永久允许规则（审批“以后都允许”写进配置）+ ask_user 提问工具 + .eeignore
9. [ ] headless `ee exec --json`（自动化执行器的基础）
10. [ ] ★自动化 / 定时任务 v1：项目上按 cron/间隔/每天几点跑提示词；ee serve 内调度器，持久化 ~/.ee/automations.json；每次新建会话、默认只读；结果进左栏「自动化」收件箱 + Windows 通知；关窗后托盘继续调度；错过的开机补跑一次；设置页增删改、立即运行、运行历史（依赖 1、5、9）

**D5 — 并行与扩展**
1. ★git worktree 管理（.worktreeinclude、删除前快照） 2. ★子代理并行（只读并发，写型走 worktree） 3. 自动化 v2（worktree 模式、会话内 /loop） 4. ★Hooks（6 个核心事件，退出码 2 拦截，信任表复用 MCP） 5. Skills（SKILL.md，兼容 .claude/.agents） 6. ★仓库地图（tree-sitter tags） 7. 检查点时间线 UI + 非 git 项目审阅 + 按块接受 8. ★Windows 沙箱 v1（受限令牌 + ACL，只能写工作区，可选禁网） 9. 回答风格（讲解/作业辅导）、按用途分模型、推理强度、预算告警 10. 修剪旧工具输出、fork 式子代理

**D6 — 编排与深度理解**
1. ★指挥会话：threads_list / thread_create（可指定分支，自动建 worktree）/ thread_send / thread_status / thread_wait；跨会话审批冒泡；左栏树形从属；指挥会话里每个子会话一张进度卡片 2. ★LSP 诊断回灌（pyright、typescript-language-server 先行）+ LSP 工具 3. best-of-n、本地 ↔ worktree 交接 4. Goal 模式、/side 侧聊 5. 自动审批审查员 6. 命令面板 / 快捷键自定义、活动视图、会话归档置顶、导出 7. 自动记忆提取（待用户确认） 8. 自动更新、GBK 解码回退、PAC 代理、PR 工作流、lint/test 循环
- 后置不做：插件包、浏览器（经 MCP 即可）、语音、自定义工具、WSL、云任务、向量索引。

关键依赖：通知+托盘 → 自动化；磁盘快照 → 自动化 / worktree / 时间线 / 编辑重发可靠恢复；headless exec → 自动化；worktree → 并行写型子代理 → 指挥会话 / best-of-n；Windows 沙箱 → 放心全自动 + 无人值守自动化。
