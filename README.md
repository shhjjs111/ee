# ee

ee 的个人 AI 编程 Agent 桌面端（Windows 优先）。只走 OpenAI Chat Completions 协议，默认 DeepSeek，Qwen、Kimi、GLM、MiniMax、豆包、中转站 GPT 等都能接。

## 目录
- `ee/`：Python 内核和本地服务 `ee serve`（工具循环、多模型适配、权限审批、会话存储、git）
- `desktop/`：Electron + React 桌面界面（三栏布局：项目/对话、对话区、改动审阅；设置页）
- `research/`：设计文档和对 Codex、Claude Code、OpenCode、天枢的研究笔记
- `agent/`：最早的单文件原型（已不用）

## 开发运行（Windows）
```powershell
cd ee
pip install -e .
cd ..\desktop
npm install
npm run dev
```
需要 Python 3.11+ 和 Node 20+。指定 python：`$env:EE_PYTHON="C:\path\python.exe"`。
API key：在设置页填写（明文存于 `%USERPROFILE%\.ee\config.toml`），或设环境变量 `DEEPSEEK_API_KEY`。

## 状态（2026-10-26）
- 已完成：D1（后端、桌面界面、设置页、审批、改动审阅、Windows 安装包 + GitHub Actions）、D2（终端、文件预览、上下文压缩、AGENTS.md、防死循环、网页搜索、子代理、记忆）、D3-C（配置迁移、模型单价细分）、D3-B（斜杠命令 /compact /new /undo /model /mode /memory /init 与自定义提示 `~/.ee/prompts/*.md`）
- 进行中：D3（MCP、附件、分叉/编辑重发、API key 安全存储），方案见 `research/d3-plan.md`，接手进度见 `HANDOFF.md`
