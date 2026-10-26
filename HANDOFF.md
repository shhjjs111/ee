# 接手说明（给接手的 AI / 另一个账号）

最后更新：2026-10-26 13:20（Asia/Shanghai）

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
- 修复（进行中，代码在 Hark 工作区，未推送）：`ee/proc.py` 统一子进程（stdin=DEVNULL、超时杀进程树、收尾有超时）；serve 启动时把 fd0 换成 NUL、只在私有副本上读 EOF；所有慢 RPC 挪到 asyncio.to_thread；后端日志 `ee-backend.log` + 事件循环卡顿看门狗；模型请求超时/代理设置/中文错误/“正在连接模型…”状态。
- 其余排查点：
  1. AsyncOpenAI 默认 timeout 600s + 自己重试 3 次 → 网络/代理不通时界面几十分钟毫无提示。要加连接/读取超时、立即显示“正在连接模型…”、失败显示清晰中文错误。Windows 上 httpx 会读系统代理（注册表），要可配置（跟随系统/直连/自定义）。
  2. server 里有同步 subprocess（git 等）在事件循环上执行 → 阻塞 WebSocket，设置 RPC 转圈。全部改 to_thread。
  3. Electron 端 RPC 无超时、WS 断开不提示 → 加超时、重连、错误显示；后端 stderr 和 Python 端自己的日志都写进 logs。
  4. 设置保存后默认模型/key 是否真生效。
  5. PyInstaller 冻结版缺 hidden imports/证书。CI 里加：用打包后的 ee-backend.exe 跑 e2e（假模型 + 黑洞端点测超时）。
- **用户决定（2026-10-26）：不急着出新安装包，先多推进功能，攒一起再出包。**

**D2 进度**
- [x] 上下文压缩 `ee/core/compaction.py`（自动约 80% + 手动）、`thread/compact` RPC、UI（点上下文圆环或输入 /compact）。
- [x] AGENTS.md/CLAUDE.md 自动加载 `ee/core/instructions.py`（~/.ee/AGENTS.md + 工作区根到 cwd，32KiB 上限），UI 头部芯片。
- [x] 防死循环 `ee/core/loopguard.py`，TurnDone reason="loop"，UI 显示“检测到重复操作，已停止”。
- [x] 右栏真终端：Electron main 里 node-pty 1.1.0 + xterm.js，多标签，跟随 shell 设置；node-pty 打包到 resources/node-pty（asar 外），npmRebuild=false；CI 加了 pty smoke 和安装包内 node-pty 检查（待 Windows CI 确认）。
- [x] 文件树/预览：`files/tree`、`files/read` RPC（限制在工作区内），Shiki 高亮、Markdown 渲染、图片；工具卡片里的路径可点击打开。右栏可拖宽。
- [~] Windows 无响应 bug 修复（见上）。
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
