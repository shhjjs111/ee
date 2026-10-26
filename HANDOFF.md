# 接手说明（给接手的 AI / 另一个账号）

最后更新：2026-10-26 14:05（Asia/Shanghai）

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
- 2026-10-26 13:50 D3 方案定稿：research/d3-plan.md（A MCP、B 斜杠命令、C 配置迁移+单价、D 附件、E 分叉/编辑重发）。尚未写代码。并行进行中：secure-keys（API key 存 Windows 凭据管理器）。
