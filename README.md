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
API key：在设置页填写（保存在 Windows 凭据管理器，`config.toml` 里不留明文；旧版明文会在启动时自动迁移），或设环境变量 `DEEPSEEK_API_KEY`。

## 状态（2026-10-26）
- 已完成：D1（后端、桌面界面、设置页、审批、改动审阅、Windows 安装包 + GitHub Actions）、D2（终端、文件预览、上下文压缩、AGENTS.md、防死循环、网页搜索、子代理、记忆）、D3-C（配置迁移、模型单价细分）、D3-B（斜杠命令 /compact /new /undo /model /mode /memory /init 与自定义提示 `~/.ee/prompts/*.md`）、D3-E（消息编辑重发、分叉对话）、D3-D（图片附件：粘贴/拖放，deepseek-flash 可看图）、D3-A（MCP：stdio/HTTP/SSE 服务器，设置页「MCP」分区管理和测试，支持项目 .mcp.json）、D3-F（API key 存 Windows 凭据管理器，日志/会话脱敏）、D4-1（磁盘快照：影子 git 仓库，shell 改的文件也能 /undo，重启后编辑重发仍能恢复文件）、D4-2（/review 代码审查：未提交/相对分支/某提交/按要求，只读回合、P0–P3 问题卡片可一键修；/commit：模型写提交信息，确认后提交）、D4-3（Plan 模式：只读调研出方案，批准后切默认/全自动执行，Shift+Tab 切模式）
- 进行中：D4-4 会话全文搜索，之后 D4 其余（会话搜索、通知托盘、后台长命令、PDF/Word/Excel、自动化）→ D5（worktree、并行子代理、Hooks、Skills、Windows 沙箱）→ D6（指挥会话、LSP），路线见 `HANDOFF.md`，对照审计见 `research/parity-audit.md`
