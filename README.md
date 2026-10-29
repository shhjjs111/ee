# ee

ee 的个人 AI 编程 Agent 桌面端（Windows 优先）。只走 OpenAI Chat Completions 协议，默认 DeepSeek，Qwen、Kimi、GLM、MiniMax、豆包、中转站 GPT 等都能接。

## 目录
- `ee/`：Python 内核和本地服务 `ee serve`（工具循环、多模型适配、权限审批、会话存储、git）
- `desktop/`：Electron + React 桌面界面（对话优先：项目/会话栏 + 对话区，改动/文件/终端按需在右侧展开；设置页）
- `research/`：设计文档和对 Codex、Claude Code、OpenCode、天枢的研究笔记
- `agent/`：最早的单文件原型（已不用）

## 开发运行（Windows）
```powershell
uv venv .venv                                             # 仓库根，只建一次（本机 .venv 由 uv 创建，没有 pip）
uv pip install --python .venv/Scripts/python.exe -e "ee[test]"
cd desktop
npm install
npm run dev
```
没有 uv 时：`python -m venv .venv` 然后 `./.venv/Scripts/python.exe -m pip install -e "ee[test]"`。
需要 Python 3.11+ 和 Node 20+。指定 python：`$env:EE_PYTHON="C:\path\python.exe"`。
API key：在设置页填写（保存在 Windows 凭据管理器，`config.toml` 里不留明文；旧版明文会在启动时自动迁移），或设环境变量 `DEEPSEEK_API_KEY`。

### 桌面端界面验证
```powershell
cd desktop
npm test                 # 组件与逻辑单测（vitest + jsdom）
npm run typecheck        # 渲染层 + 主进程类型检查
npm run build            # 必须先构建：Electron 加载 dist/，不重建就看不到源码改动
npm run test:ui          # 真实 Electron 窗口的渲染基线（Playwright）
```
`test:ui` 测的是用户能看见的几何结果：首屏是否对话优先、输入框有没有常显滚动条、
工具面板打开后是否真有宽度、960×600 下中区是否还可用、深色令牌是否生效。
判定用真实尺寸测量而非截图相似度——本机没有基线图维护流程，尺寸断言更稳，失败时也更能指出问题。
失败截图与 trace 落在 `test-results/`（已 gitignore）。

界面布局的取舍与设计边界见 `.rivet/plans/ee-codex-桌面工作台-ui-改版完整计划.md`（本地工作记录，不入库）。

**务必用 `.venv`，不要用 PATH 里的 `python`**：`openai` 与 `tree_sitter` 是延迟导入，装错解释器时多数测试仍通过，只在触及真客户端、Windows 沙箱、仓库地图时失败——看起来是代码问题，其实是环境。实测同一批文件：PATH 的 Anaconda python 下 17 failed，项目 `.venv` 下 0 failed。全量测试：`./.venv/Scripts/python.exe -m pytest -c ee/pyproject.toml -q`（当前基线 1578 passed / 15 skipped / 0 failed，约 17 分钟）。

## 当前路线（2026-10-28）

当前实施入口：[research\roadmap-current.md](research/roadmap-current.md)。先执行 Wave 0–2：文档与 CI 基线、worktree/MCP/预算稳定化、会话生命周期；达到停点后再进入 D6 指挥会话和 LSP。旧研究文档不再作为当前待办。

下面是已有能力与历史交付摘要，其中的测试数字不是本批复验结果。本批验证状态以当前路线的执行记录为准。

## 已有能力与历史交付（截至 2026-10-28）
- 2026-10-28：审查报告的 7 个问题全部修复（已合并 main，全量 pytest 1480 passed / 0 failed）——`apply_patch` 不再静默改错位置（去 strip 级、多处命中报歧义、缩进按整体平移对齐）；按块「拒绝」不再改到邻近的重复内容（按 hunk 是否有上下文行区分搜索范围）；写文件改为**原子替换**（磁盘满/进程被杀不再留半截文件）；只读模式补上 PowerShell 管道成员调用（裸词/冒号/动态表达式三种）、危险长选项按 GNU 前缀缩写匹配；**只读白名单改为按命令的参数白名单**（未知选项一律不放行）；只读/计划模式**默认开沙箱 + 默认禁网**（`web_search`/`web_fetch` 不受影响）。另：子代理收尾时 diff 失败不再误删 worktree/分支（以前会连已提交的代码一起删）；混合换行文件改一处不再全文变 CRLF（逐行保留原换行符）。另：流式工具调用同一 index 下发多个调用时参数不再串槽（按 index 记录活跃槽）。
- 2026-10-27：自动化 v2 与 /loop、检查点时间线与按块接受、工具 details、上下文构成视图、增量摘要；安全审查修复（项目配置只收紧、口令不进子进程、按真实 shell 判命令、pytest 需确认、edit 宁报错不误改、撤销冲突确认、原子写入、DeepSeek 推理回传与档位、打开文件拦可执行）。Windows 命令沙箱（D5-8，默认关，设置页「工具与记忆 → 命令沙箱」开启）。
- 已完成：D1（后端、桌面界面、设置页、审批、改动审阅、Windows 安装包 + GitHub Actions）、D2（终端、文件预览、上下文压缩、AGENTS.md、防死循环、网页搜索、子代理、记忆）、D3-C（配置迁移、模型单价细分）、D3-B（斜杠命令 /compact /new /undo /model /mode /memory /init 与自定义提示 `~/.ee/prompts/*.md`）、D3-E（消息编辑重发、分叉对话）、D3-D（图片附件：粘贴/拖放，deepseek-flash 可看图）、D3-A（MCP：stdio/HTTP/SSE 服务器，设置页「MCP」分区管理和测试，支持项目 .mcp.json）、D3-F（API key 存 Windows 凭据管理器，日志/会话脱敏）、D4-1（磁盘快照：影子 git 仓库，shell 改的文件也能 /undo，重启后编辑重发仍能恢复文件）、D4-2（/review 代码审查：未提交/相对分支/某提交/按要求，只读回合、P0–P3 问题卡片可一键修；/commit：模型写提交信息，确认后提交）、D4-3（Plan 模式：只读调研出方案，批准后切默认/全自动执行，Shift+Tab 切模式）、D4-4（会话全文搜索：左栏搜索匹配对话内容并显示片段，中文可用）、D4-5（桌面通知：回合完成/出错/需要审批时弹 Windows 通知，点通知直达对话；关窗后托盘常驻）、D4-6（后台长命令：dev server/watch/交互程序后台运行，模型可读增量输出、写输入、结束；输入框「后台」按钮查看和停止）、D4-7（直接读 PDF/Word/Excel/PowerPoint：按页/工作表转文字，文件预览也能看）、D4-8（审批可“本项目以后都允许”；ask_user 让模型带选项向你提问；.eeignore 排除不让 ee 碰的文件）、D4-9（`ee exec` 无界面执行：脚本/CI/定时任务用，支持 --json 事件流、续接会话）、D5-4（Hooks：在工具调用前后、提交消息、回合结束时跑你的脚本，可拦截）、D5-5（Skills：SKILL.md 技能，兼容 .claude/.agents 目录）、D5-1（git worktree：新对话可在独立目录和分支里进行，项目菜单里合并或删除）、D5-2（子代理并行：只读子代理和只读工具同时跑，多个会改文件的子代理各进独立 worktree 分支并行，结束后由你决定是否合并）、D5-9（推理强度 默认/低/中/高/关闭思考，会话可切并按模型记住；按用途分模型：标题/压缩/只读子代理/计划/审查/提交信息；回答风格：默认/简洁（可自定义 ~/.ee/styles）；花费上限：会话/每日/每月，80% 提醒，到线仅提醒或暂停询问，ee exec 与自动化超限停止）
- 已有 D4-10 自动化/定时任务、仓库地图（repo_map）、D5-9，以及精简模式、工具描述压缩、edit 多处编辑、SYSTEM.md/APPEND_SYSTEM.md、消息队列、自动化 v2、检查点时间线和 Windows 沙箱实现。真实 DeepSeek 对比评测仍未在本批执行；当前下一步以 `research\roadmap-current.md` 为准，不重复按旧对标表建设。
- 2026-10-26：新增精简模式（lean，4 个工具）、edit 一次多处、SYSTEM.md/APPEND_SYSTEM.md、插话/完成后追加队列、模型错误分类、修剪旧工具输出、分叉子代理。
