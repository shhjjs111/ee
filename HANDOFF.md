# 接手说明（给接手的 AI / 另一个账号）

最后更新：2026-10-26（Asia/Shanghai）

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
  1. 在真实 Electron 窗口里验证（预加载桥接、后端进程退出清理）。
  2. 打包：PyInstaller onedir 后端（`collect_data_files('ee')` 打进 prompts 和 profiles/*.toml）；`desktop/scripts/build-backend.mjs`（package.json 已引用但文件还没写）；electron-builder NSIS，`extraResources` → resources/backend；`.github/workflows/desktop-windows.yml`（windows-latest 构建并上传安装包）。
  3. `ee/README.md` 仍是旧终端版内容，需改写成桌面端中文说明；design.md 第 12 节状态要更新（TUI 已删除）。
- D2：右栏终端（xterm.js + node-pty）和文件预览（现为占位）、上下文压缩 /compact、会话 fork、AGENTS.md 自动加载、防死循环、网页搜索（倾向 DeepSeek 自家 Search/Fetch API）、子代理、记忆。

## 已知限制
- 改动审阅需要 Git。设置页保存会重写 `~/.ee/config.toml`（注释丢失，旧文件存为 .bak）；设置页里填的 key 是明文。
- 非 DeepSeek 模型的单价需在设置里填。DeepSeek 价格（2026-10-25 官网）：flash 高峰 命中 0.04 / 未命中 2 / 输出 8 元每百万 token；pro 为 0.30 / 9 / 27。
- 未在真实 Windows 和真实 API key 上测过。

## 约定
- 每完成一步：更新本文件、README、design.md 状态，提交推送到本仓库 main。
