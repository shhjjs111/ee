# 接手说明（给接手的 AI / 另一个账号）

最后更新：2026-10-26 13:35（Asia/Shanghai）

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
- 修复（已推送 main，等 Windows CI 确认）：
  - `ee/proc.py:run_capture`：所有短命令（git/rg/taskkill）stdin=DEVNULL、超时杀进程树、收尾有超时；asyncio 子进程也 stdin=DEVNULL。
  - `ee serve --exit-on-stdin-eof`：启动时 dup 出 stdin 私有副本只在它上面读 EOF，fd0/STD_INPUT_HANDLE 换成 NUL（第三方代码起子进程也不会再继承那根管道）。
  - server：thread/start、thread/get、turn/start（首次建会话）、settings/*、models/list、workspace/*、thread/list、terminal/shell 全部 asyncio.to_thread；会话里 refresh_git / AGENTS.md 加载 / 模型客户端构造也挪到线程；通知广播线程安全。
  - 日志：后端 `ee-backend.log`（EE_LOG_DIR → %APPDATA%\ee\logs → ~/.ee/logs），记录 RPC 失败/慢 RPC、回合开始结束、模型请求开始/结束/异常 traceback、代理；事件循环卡住 ≥8s 自动把调用栈写进日志（`ee/log.py` LoopWatchdog）。界面侧 RPC 失败/重连/自动重启写 `backend.log`。
  - 模型请求（`ee/core/providers/net.py`）：连接 15s / 读取 90s / 重试 2 次（设置可改）；代理 跟随系统/直连/自定义，本机地址永远直连；失败给中文错误（含 base_url、HTTP 状态、走没走代理）；`turn/status` 事件显示“正在连接模型…/已连接，等待模型输出…”。
  - 桌面端：RPC 默认 20s 超时并显示错误；设置页加载失败显示错误+重试（原来 promise 没 catch → 永远转圈）；发送失败在对话里出错误条目、不再卡“正在工作”；WS 断开顶部提示，重连失败或内核退出自动重启内核（2 分钟内最多 3 次）。
  - CI：源码后端 e2e、打包 exe `selftest`、打包 exe e2e（假模型）、打包 exe 黑洞端点 e2e（必须在超时内报错）。
- 已核实没问题：默认模型 `deepseek-flash`（官方当前名，2026-10 文档）→ provider deepseek → 设置页存的 key；settings/set 后已有会话立即用新 key（有测试）。
- **用户决定（2026-10-26）：不急着出新安装包，先多推进功能，攒一起再出包。**

**D2 进度**
- [x] 上下文压缩 `ee/core/compaction.py`（自动约 80% + 手动）、`thread/compact` RPC、UI（点上下文圆环或输入 /compact）。
- [x] AGENTS.md/CLAUDE.md 自动加载 `ee/core/instructions.py`（~/.ee/AGENTS.md + 工作区根到 cwd，32KiB 上限），UI 头部芯片。
- [x] 防死循环 `ee/core/loopguard.py`，TurnDone reason="loop"，UI 显示“检测到重复操作，已停止”。
- [x] 右栏真终端：Electron main 里 node-pty 1.1.0 + xterm.js，多标签，跟随 shell 设置；node-pty 打包到 resources/node-pty（asar 外），npmRebuild=false；CI 加了 pty smoke 和安装包内 node-pty 检查（待 Windows CI 确认）。
- [x] 文件树/预览：`files/tree`、`files/read` RPC（限制在工作区内），Shiki 高亮、Markdown 渲染、图片；工具卡片里的路径可点击打开。右栏可拖宽。
- [x] Windows 无响应 bug 修复（见上；等 Windows CI 全绿后再出包，用户真机待验证）。
- [~] 网页搜索/抓取（默认 DeepSeek 自家 Search/Fetch API，可插拔，可关闭）、子代理（独立上下文、可只读、审批冒泡到 UI）、记忆（~/.ee/memory.md + 工作区 .ee/memory.md，注入位置稳定以利前缀缓存）。
- 上面 [x] 的代码在 Hark 工作区已完成，pytest 约 168 个通过、npm run build 通过；会和 bug 修复一起推送到本仓库（若你看到仓库里还没有，说明尚未推送）。
- 之后：所有功能稳定 → Windows CI 全绿 → 出新安装包给用户。

## 已知限制
- 改动审阅需要 Git。设置页保存会重写 `~/.ee/config.toml`（注释丢失，旧文件存为 .bak）；设置页里填的 key 是明文。
- 非 DeepSeek 模型的单价需在设置里填。DeepSeek 价格（2026-10-25 官网）：flash 高峰 命中 0.04 / 未命中 2 / 输出 8 元每百万 token；pro 为 0.30 / 9 / 27。
- 真实 Windows 上已安装，但出现上面的无响应 bug；真实 API key 回合尚未跑通。
- 安装包未签名，SmartScreen 需“更多信息 → 仍要运行”。

## 约定
- 每完成一步（甚至进行中）：更新本文件、README、design.md 状态，提交推送到本仓库 main。用户可能随时换账号接手，本文件必须始终反映最新进度。
- 推送会触发 Windows Actions，产出 artifact `ee-windows-installer`。
