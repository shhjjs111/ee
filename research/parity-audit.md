# ee 功能对标审计（2026-10-26）

范围：拿 ee 现状（HANDOFF.md 截至 D3：C/B/E/D 已完成，A MCP 进行中，F key 安全存储未做）对比 OpenAI Codex（CLI/桌面/IDE）、Claude Code、OpenCode、Cline、Aider、Cursor，必要时参考 Goose、Continue。
图例：ee 现状 = 有 / 部分 / 无；价值 = 高/中/低（按"ee 自用：写项目、做作业、整理资料、Windows 桌面"衡量）；难度 = S（≤1 天）/ M（2–4 天）/ L（≥1 周）。
缩写：CX=Codex，CC=Claude Code，OC=OpenCode，CL=Cline，AI=Aider，CU=Cursor，GS=Goose，CT=Continue。

## (a) 功能对照表

### 1. 会话与上下文
| 功能 | 谁有 | ee 现状 | 价值 | 难度 | 实现要点 |
|---|---|---|---|---|---|
| 会话持久化/恢复 | 全部 | 有 `core/store.py` | — | — | — |
| 分叉会话 | CX `/fork`、CC、OC | 有 `server/branch.py` | — | — | 后续补"分叉到 worktree"（见 5 节） |
| 编辑重发 | CC rewind、CL、CU | 有 `server/branch.py` | — | — | 快照只在内存 → 等磁盘快照修复重启后不能恢复文件 |
| 上下文压缩（自动+手动） | 全部 | 有 `core/compaction.py` | — | — | — |
| 旧工具输出裁剪 prune | OC | 无 | 中 | S | 只在压缩时把 40k token 以前的工具输出换成"[旧输出已清除]"，保持前缀缓存；改 `compaction.py` |
| 会话全文搜索 | CX（`rollout/src/search.rs`）、CC、CU | 部分（仅标题过滤，`server/app.py::_thread_list`） | 高 | S–M | 新 `core/search_index.py`：SQLite FTS5 索引 items 文本，`store.append` 时增量写；RPC `thread/search` 返回命中片段；左栏搜索框改走它 |
| 归档 / 置顶会话 | CX、CC 桌面 | 无 | 低 | S | meta 加 archived/pinned；`_thread_list` 过滤排序；Sidebar 菜单 |
| 侧聊 /side（不打断主会话） | CX（`tui/src/app/side.rs`）、CC 桌面 | 无 | 中 | S | 复用 `Session.side_complete`：复制当前 history 只读跑一问一答，不写回主 store；右栏抽屉显示 |
| Goal 模式（持续目标+完成判据） | CX `/goal`（`codex-rs/ext/goal/`） | 无 | 中 | M | Thread 级 goal 状态；回合结束若未达成判据自动续跑（受步数/花费上限），进度条可暂停/编辑 |
| 会话导出 Markdown | CC、OC share、CX | 无 | 低 | S | RPC `thread/export` 把 items 渲染成 md；Electron 保存对话框 |
| 运行中排队输入 | CX、CC | 有 `Session.queue_input` | — | — | — |
| 上下文/缓存命中/花费显示 | CX `/status`、CC statusline | 有 `core/context.py::UsageStats` | — | — | — |
| AGENTS.md 分层加载 | CX、CC、OC、CU | 有 `core/instructions.py` | — | — | 可加 `AGENTS.override.md`（CX）与按 glob 生效的规则（CU `.cursor/rules`）|
| 长期记忆 | CX memories、CC、CL memory bank | 有 `core/memory.py` | — | — | — |
| 自动记忆提取/整理 | CX（`ext/memories/`，extract+consolidate 模型） | 无 | 中 | M | 会话结束后用 flash 侧路请求抽取候选条目 → 设置页"待确认"列表，用户点保留才写 memory.md |
| @文件提及 | 全部 | 有 `core/fileindex.py` | — | — | 可加 @文件夹、@git diff、@网址（CL mentions：`apps/vscode/src/shared/context-mentions.ts`）|
| 图片输入 | CX、CC、CL、CU | 有 `core/attachments.py` | — | — | — |
| PDF/Word/Excel 读取 | CX 文档插件、CC | 无（design §3 M4 未做） | 高 | S | `tools/read.py` 按扩展名分派：pypdf / python-docx / openpyxl 抽文本，带页码；PyInstaller spec 加依赖 |

### 2. 编辑与撤销
| 功能 | 谁有 | ee 现状 | 价值 | 难度 | 实现要点 |
|---|---|---|---|---|---|
| 回合级 /undo | CX、OC、AI | 部分（内存快照 `tools/base.py::ToolState.snapshots`，shell 改的文件不覆盖，重启即失） | 高 | — | 由下一行替代 |
| 磁盘快照/检查点（影子 git） | OC `snapshot/index.ts`、CL checkpoints、CX `git-utils/baseline.rs`、CC checkpointing | 无 | 高 | M | 新 `core/snapshot.py`：`~/.ee/snapshots/<项目hash>/` 独立 `--git-dir`+`--work-tree=项目`，回合开始/每个写工具后 `add -A && write-tree`（不建分支、不碰用户 .git）；条目记 tree hash；/undo、编辑重发改为 `read-tree`+`checkout-index` 恢复；修层号错位 |
| 检查点时间线 UI（任意点对比/恢复） | CL、CC Esc Esc rewind | 无 | 高 | M | 依赖快照；用户气泡旁"恢复到此"：仅文件/仅对话/两者；diff 用 `git diff tree1 tree2` 复用 `DiffView.tsx` |
| 改动审阅（按文件 diff+撤销） | CX 桌面、CC 桌面 | 有 `server/gitops.py`、`RightPanel.tsx` | — | — | — |
| 按块接受/拒绝、暂存 | CX 桌面、CU | 无 | 中 | M | `gitops.py` 生成单 hunk patch 走 `git apply [--cached] [-R]`；DiffView 每块加按钮 |
| 非 git 项目的改动审阅 | CX（无 git 也能看）、OC | 无（"改动审阅需要 Git"） | 中 | S | 快照完成后直接用影子仓库 diff，非 git 目录也可审阅 |
| 编辑后自动格式化 | OC `format/`、CC hooks、CU hooks | 无 | 中 | S | 内置格式化表（ruff/black/prettier/gofmt…，按扩展名+命令存在才启用），写工具成功后跑；或做成默认 hook |
| 在外部编辑器/IDE 打开 | CX、CC 桌面 | 部分（`shell:openPath`） | 低 | S | 设置里选 VS Code/Cursor，`code -g file:line` |

### 3. 执行与沙箱
| 功能 | 谁有 | ee 现状 | 价值 | 难度 | 实现要点 |
|---|---|---|---|---|---|
| 权限三档+规则+命令拆分 | CX、CC、OC | 有 `core/permissions.py` | — | — | — |
| 危险命令识别（含 PowerShell/cmd） | CX `shell-command/src/command_safety/` | 有 `core/permissions.py` | — | — | 可参考 CX `windows_dangerous_commands.rs`、`powershell_parser.rs` 补漏 |
| "以后都行"持久化为规则 | CX rules、OC `permission/saved.ts`、CC settings | 部分（仅会话内） | 中 | S | 审批选项加"永久允许"：写 `<项目>/.ee/config.toml` 或用户级 `[permissions].allow`；设置页可删 |
| 后台/长驻命令（dev server、交互输入） | CX `unified_exec.rs`+`write_stdin`、CC 后台 Bash/Monitor | 无（`tools/shell.py` 阻塞到超时） | 高 | M | shell 工具加 `background:true` 返回 job_id；新工具 `shell_output(job_id)`/`shell_kill`；进程表挂 Thread，回合结束不杀，界面显示运行中任务 |
| Windows 沙箱 | CX（`windows-sandbox-rs/`：mxc/elevated/unelevated） | 无 | 高 | L | 先做 CX 的 unelevated 方案：`CreateRestrictedToken`+低完整性/能力 SID，给工作区和临时目录加 ACL 允许写，其余只读；Python ctypes 实现在 `tools/sandbox_win.py`，shell 工具在全自动模式下用 `CreateProcessAsUserW` 启动 |
| 禁网/网络策略 | CX `network-proxy/` | 无 | 中 | M | 依赖沙箱；简单版：子进程环境注入指向本地拒绝代理 + 防火墙规则可选；规则按域名白名单 |
| 自动审批审查员 | CX auto-review/guardian（`core/src/guardian/`） | 无 | 中 | M | 全自动模式下"强制询问类"操作先交 flash 侧路判定（泄密/破坏性/绕过），通过则放行，否则弹给用户；`permissions.py` 加 `review` 动作 |
| 敏感文件保护（.eeignore） | CL `.clineignore`、CX deny-read | 部分（可写 deny 规则） | 中 | S | 读 `.eeignore`（gitignore 语法）→ read/grep/glob/shell 路径命中即拒；默认含 `.env`、`*.pem`、`id_rsa` |
| 提问工具（让模型问用户选项） | CX `request_user_input.rs`、CC AskUserQuestion、OC question | 无 | 中 | S | 新工具 `ask_user{question, choices}`，复用审批卡片通道（ApprovalRequest 变体），答案作为工具结果 |

### 4. 多代理与编排
| 功能 | 谁有 | ee 现状 | 价值 | 难度 | 实现要点 |
|---|---|---|---|---|---|
| 子代理 task | 全部 | 有 `core/subagent.py` | — | — | — |
| 子代理并行 | CX、CC、OC、CU | 部分（多个 task 串行） | 高 | M | session 里只读子代理（explore）同步并发 `asyncio.gather`；写型子代理必须各自 worktree 才并行；`[subagent].max_parallel` |
| git worktree 隔离 | CX（`codex-rs/worktree/src/`）、OC `worktree/index.ts`、CU `/worktree`、CL Kanban | 无 | 高 | M | 新 `core/worktree.py`：`~/.ee/worktrees/<项目>/<名>` + `git worktree add -b ee/<名>`；`.worktreeinclude` 复制被忽略文件；删除前打快照；合并回主分支前出 diff |
| 指挥会话 / 会话控制工具 | CX `tools/handlers/multi_agents_v2/`、CC agent teams、CU Projects coordinator | 无 | 高 | L | 工具 `threads_list/thread_create(branch?)/thread_send/thread_status/thread_wait` 调 `server/app.py` 的 Thread；子会话 meta.parent_id；审批冒泡到指挥会话；左栏树形+进度卡片 |
| best-of-n（同任务多模型并行比较） | CU `/best-of-n` | 无 | 中 | M | 依赖 worktree+并行；N 个 worktree 各跑一个模型，结束后并排 diff，选一个合并 |
| Fork 式子代理（复用父前缀缓存） | CX fork、design §4.8 | 无 | 中 | S | task 加 `fork:true`：复制父 history 前缀+末尾追加任务，DeepSeek 前缀缓存命中省钱 |
| Local ↔ Worktree 交接 | CX Handoff、CU 本地/云交接 | 无 | 中 | M | 依赖 worktree；"移到本地"= 在主目录 checkout 该分支或 `git apply` 补丁；会话 cwd 切换 |
| 云端任务 | CX Cloud、CU Cloud Agents、CC cloud | 无 | 低 | L | 个人自用不做；需要时可用 GitHub Actions 跑 headless ee |

### 5. 自动化
| 功能 | 谁有 | ee 现状 | 价值 | 难度 | 实现要点 |
|---|---|---|---|---|---|
| 定时任务 / Automations（收件箱） | CX Scheduled tasks（RRULE、本地或 worktree、收件箱+未读）、CC `/loop`+定时、GS `scheduler/`、CL `schedule` | 无 | 高 | L | `server/automations.py`：持久化 `~/.ee/automations.json`，asyncio 调度（cron/RRULE 用 croniter 或自写），每次运行新建会话（默认只读，可选 worktree），结果进左栏「自动化」收件箱+通知；开机补跑一次；设置页增删改/立即运行/历史 |
| 会话内定时跟进（/loop） | CC `/loop`、CX 会话内定时 | 无 | 中 | S | 复用调度器，目标是现有 thread：到点 `start_turn(固定提示)`；7 天过期 |
| 无界面单发 exec（JSON 输出） | CX `exec/`、CC headless `-p`、OC `run` | 无（`cli.py` 只有 serve/doctor） | 中 | S | `ee exec "任务" --cwd --json`：直接驱动 Session，事件按 JSONL 打印；自动化与 CI 都可复用 |
| 事件触发（PR/邮件） | CX web 版 | 无 | 低 | L | 不做 |
| 任务模板 / Recipes | GS `recipe/`、CL workflows | 部分（自定义提示 `core/prompts.py`） | 低 | S | 自定义提示 frontmatter 加 model/mode/参数，自动化可直接引用 |

### 6. 工具与扩展
| 功能 | 谁有 | ee 现状 | 价值 | 难度 | 实现要点 |
|---|---|---|---|---|---|
| MCP 客户端（stdio/HTTP/SSE） | 全部 | 部分（D3-A 进行中，`ee/ee/mcp/`） | 高 | — | 按 d3-plan A 节收尾 |
| MCP 资源/提示词/OAuth | CX、CC | 无 | 低 | M | resources/list→@提及；prompts/list→斜杠命令；OAuth 先不做 |
| Skills（SKILL.md 按需加载） | CX（`codex-rs/ext/skills/`）、CC、CL、OC `skill.ts` | 无 | 高 | M | `core/skills.py` 扫 `~/.ee/skills/*/SKILL.md`、`<项目>/.ee/skills/`，兼容 `.claude/skills`、`.agents/skills`；名字+描述作为固定 user 消息注入（保前缀缓存），`skill` 工具按需读正文；`$名字` 显式调用 |
| Hooks | CC（20+ 事件，command/http/prompt/agent）、CX `codex-rs/hooks/`、CL、CU `.cursor/hooks.json` | 无 | 高 | M | `core/hooks.py`：事件 SessionStart/UserPromptSubmit/PreToolUse/PostToolUse/Stop/PreCompact；stdin 传 JSON，退出码 2=拦截、stdout JSON 可改参数/追加上下文；项目级 hook 需信任确认；在 `session.py` 工具执行前后调用 |
| 插件包（skills+hooks+MCP+命令） | CX plugins、CC plugins、CU | 无 | 低 | L | 等 skills/hooks 稳定后再定目录格式 |
| 自定义斜杠命令 | CX、CC、OC | 有 `core/prompts.py` | — | — | — |
| 自定义工具（脚本） | OC custom-tools | 无 | 低 | M | `.ee/tools/*.py` 声明 schema，子进程执行；优先建议用 MCP |
| 网页搜索/抓取 | 全部 | 有 `tools/web.py` | — | — | — |
| 浏览器操控/截图 | CL `services/browser/`、CX browser/computer use、CC | 无 | 中 | M | 走 MCP（playwright-mcp）+ 设置页一键模板，比自写省事 |

### 7. 代码理解
| 功能 | 谁有 | ee 现状 | 价值 | 难度 | 实现要点 |
|---|---|---|---|---|---|
| grep/glob | 全部 | 有 `tools/search.py` | — | — | — |
| 仓库地图 | AI `repomap.py` | 无 | 高 | M | `core/repomap.py`：tree-sitter（`tree-sitter-language-pack`）抽定义/引用 → PageRank 排序 → token 预算内输出；作为工具 `repo_map` 或会话首条附录（只发一次，保缓存）；缓存到 `~/.ee/cache/tags` |
| LSP 诊断回灌 | OC `lsp/`、CC（插件） | 无 | 高 | L | `core/lsp/`：按扩展名拉起 pyright/tsserver 等（存在才启），JSON-RPC over stdio；写工具后 `didChange` 等诊断 ≤2s，把错误附在工具结果末尾；Windows 注意 .cmd 启动 |
| LSP 跳转/引用工具 | OC lsp 工具（实验） | 无 | 中 | M | 依赖 LSP；工具 `lsp{definition|references|hover}` |
| 语义索引/向量检索 | CT `core/indexing/`、CU | 无 | 低 | L | 不做；repo map + grep 足够 |

### 8. 审查与 git
| 功能 | 谁有 | ee 现状 | 价值 | 难度 | 实现要点 |
|---|---|---|---|---|---|
| /review 代码审查 | CX（`core/src/tasks/review.rs`、`prompts/templates/review/rubric.md`）、CC、CU Bugbot | 部分（`prompts/review.md` 存在未接入） | 高 | S | 斜杠命令 `/review [未提交|对比分支|某提交|自定义]`：只读子代理跑 review.md，结果按 P0–P3 列表卡片，可"让它修" |
| /commit 生成提交信息 | AI、CX | 部分（`Session.commit_message` 有，无 RPC/界面） | 中 | S | RPC `git/commit_message`、`git/commit`；改动审阅面板加"提交"按钮，可编辑信息 |
| 每次编辑自动提交 | AI | 无 | 低 | S | 不建议默认开；磁盘快照已覆盖撤销需求 |
| PR 创建/查看（gh） | CX、CC 桌面、CU | 部分（gh 探测 `core/gitinfo.py`） | 中 | M | `/pr`：推分支+`gh pr create`（需审批）；右栏显示 PR 状态/检查 |
| lint/test 自动修复循环、证据门 | AI `linter.py`、design §6 证据门 | 无 | 中 | S | 配置 `[verify] lint/test` 命令；改文件后自动跑 lint 把错误回喂；回合结束没跑过验证就追加提醒 |

### 9. 界面体验
| 功能 | 谁有 | ee 现状 | 价值 | 难度 | 实现要点 |
|---|---|---|---|---|---|
| 系统通知（完成/需审批） | CX（从不/后台时/总是）、CC 桌面 | 无 | 高 | S | Electron `Notification`（main.ts）；后端 turn/completed、approval 请求时触发；窗口失焦才弹；Windows 需设 AppUserModelId |
| 托盘常驻/关窗不退出 | CX 桌面、CC 桌面 | 无 | 高 | S | Electron `Tray`+关窗隐藏；后端继续跑（自动化前置）；托盘菜单：显示/新会话/退出 |
| Activity 视图（运行中/待审批/未读） | CX Activity（Ctrl+Alt+U） | 无 | 中 | S | 左栏顶部按状态过滤；thread summary 加 unread |
| 命令面板 + 快捷键自定义 | CX keymap、OC keybinds、CC | 部分（Ctrl N/Ctrl K 搜索） | 中 | M | Ctrl+Shift+P 命令面板（复用 slash 定义）；keybindings.json 可改 |
| 全局唤起快捷键 | CC 桌面 | 无 | 低 | S | Electron `globalShortcut` |
| 主题 | 全部 | 有（系统/深/浅，`lib/store.ts`） | — | — | 自定义配色低价值 |
| 计划清单 | 全部 | 有 todo 工具 | — | — | — |
| Plan 模式 | CC、OC plan agent、CL plan/act、CU、CX `/plan` | 部分（只读模式近似，无"出方案→确认→执行"） | 高 | S | 模式 plan：只读权限 + 注入 plan 提醒（OC `plan.txt` 思路）；模型产出方案后界面出"按此执行"按钮，切默认模式并注入"可以改文件了" |
| 回答风格（讲解/学习模式） | CC output styles（Explanatory/Learning） | 部分（靠 AGENTS.md） | 中 | S | `~/.ee/styles/*.md` + 内置"讲解""作业辅导"；作为固定 user 消息注入（不改 system） |
| 语音输入 | AI voice、CC、CX | 无 | 低 | M | 浏览器 MediaRecorder + 兼容 /audio/transcriptions 的网关 |
| 应用预览（dev server 内嵌） | CC 桌面 preview、CU | 无 | 低 | M | 右栏加 webview 标签，依赖后台命令 |

### 10. 模型与成本
| 功能 | 谁有 | ee 现状 | 价值 | 难度 | 实现要点 |
|---|---|---|---|---|---|
| 多家族档案+怪癖处理 | OC transform | 有 `core/providers/` | — | — | — |
| 模型自检 doctor | — | 有 `doctor.py` | — | — | 可在设置页加按钮 |
| 推理强度 / 思考开关 | CX `/reasoning`、CC | 无 | 中 | S | ModelSpec 加 `reasoning_effort`/`thinking` 透传（按档案字段名），Composer 下拉 |
| 按用途分模型（压缩/标题/子代理/plan） | CL plan/act 分模型、AI architect/editor | 部分（子代理可指定 model） | 中 | S | `[models.roles] compact/title/explore/plan/review = "deepseek-flash"`，session 侧路请求按角色取 |
| 预算上限/花费告警 | CC、CX 用量 | 无 | 中 | S | 会话/每日花费超阈值暂停并提示；UsageStats 已有金额 |
| 失败回退备用模型 | CX gateway failover | 无 | 低 | S | 连续 5xx/超时切到 `fallback_model` 并提示（缓存清零） |
| 用量统计面板 | CX `/usage`、CU | 无 | 低 | S | 汇总 sessions 用量按日/项目画图 |

### 11. 安全与隐私
| 功能 | 谁有 | ee 现状 | 价值 | 难度 | 实现要点 |
|---|---|---|---|---|---|
| API key 安全存储 | CX keyring、CC Keychain | 无（明文 config.toml，D3-F 未做） | 高 | M | 按 d3-plan F 节：Credential Manager（ctypes CredWriteW）+DPAPI 回退，env > 凭据 > 旧明文 |
| 日志/会话脱敏 | CX `secrets/src/sanitizer.rs` | 部分（`net.redact`） | 中 | S | 统一 `redact()` 接入 log Filter、store.append、错误文案（F 节已列） |
| 项目级 MCP/hook 信任确认 | CX、CC | 部分（MCP `allowed_project`） | 中 | S | hooks/skills 复用同一信任表（name:sha8） |
| 网页/工具内容提示注入防护 | CX guardian、CC | 无 | 低 | S | web/MCP 结果加"不可信内容"包裹标签与系统提示约束 |

### 12. Windows 特有
| 功能 | 谁有 | ee 现状 | 价值 | 难度 | 实现要点 |
|---|---|---|---|---|---|
| shell 选择 pwsh/PowerShell/Git Bash/cmd | CX、CC | 有 `tools/shellenv.py` | — | — | — |
| 输出编码 | CX | 部分（强制 UTF-8；GBK 程序可能乱码） | 中 | S | 解码失败时按 `locale.getpreferredencoding()`/cp936 重试（`tools/shell.py:56`） |
| CRLF/BOM 保留 | — | 有 `tools/fileops.py` | — | — | — |
| 系统代理 | — | 部分（不支持 PAC，`providers/net.py`） | 低 | M | WinHTTP `WinHttpGetProxyForUrl` 解析 PAC |
| 安装包签名+自动更新 | CX、CC 桌面 | 无（未签名，手动装） | 中 | M | electron-updater + GitHub Releases（CI 已产包）；签名可后补 |
| WSL 集成 | CX `/codex/windows/wsl` | 无 | 低 | M | shell 选项加 wsl，路径互转 |

共 95 行，其中 ee 已有 23、部分 19、无 53。

## (b) 优先路线图

排序原则：价值/成本比优先；先做别的功能的地基（通知+托盘、磁盘快照、headless 执行器、worktree）。HANDOFF 里已计划的 10 项全部放入，标 ★。与 HANDOFF D4 原顺序的差异：LSP（L 级）后移到 D6，/review、Plan 模式、会话搜索（都是 S）提前到 D4。

前置：D3 收尾（A MCP、F key 安全存储）。

### D4 — 地基 + 小而高价值
| 顺序 | 项目 | 难度 | 依赖 | 可改编的开源代码（许可证 / 路径） |
|---|---|---|---|---|
| 1 | 系统通知 + 托盘常驻 | S | 无 | 无需（Electron API）；设置项参照 CX notifications 三档 |
| 2 | ★磁盘快照（影子 git，替换内存快照，修层号错位） | M | 无 | OpenCode MIT：`packages/opencode/src/snapshot/index.ts`（v1.19.4）、新版 `packages/core/src/snapshot.ts`；Cline Apache-2.0：`sdk/packages/core/src/session/checkpoint-restore.ts`、`checkpoint-diff.ts`；Codex Apache-2.0：`codex-rs/git-utils/src/baseline.rs` |
| 3 | ★/review 接入 + /commit 接入 | S | 无 | Codex：`codex-rs/core/src/tasks/review.rs`、`codex-rs/prompts/templates/review/rubric.md`、`codex-rs/core/src/session/review.rs`；OpenCode：`packages/core/src/plugin/command/review.txt` |
| 4 | ★Plan 模式（出方案→按此执行） | S | 无 | OpenCode：`packages/core/src/plugin/plan.ts`、v1 `session/prompt/plan.txt` 思路；Cline：`docs/core-workflows/plan-and-act.mdx` 行为 |
| 5 | ★会话全文搜索（FTS5） | S–M | 无 | Codex：`codex-rs/rollout/src/search.rs`、`session_index.rs`（思路，Python 自写 sqlite3） |
| 6 | PDF/Word/Excel 读取 | S | 无 | 无需（pypdf / python-docx / openpyxl） |
| 7 | 后台命令 + shell_output/kill | M | 无 | Codex：`codex-rs/core/src/tools/handlers/unified_exec.rs`、`shell_spec.rs` |
| 8 | 永久允许规则 + ask_user 提问工具 + .eeignore | S | 无 | Codex：`codex-rs/core/src/exec_policy.rs`、`tools/handlers/request_user_input.rs`；OpenCode：`packages/core/src/permission/saved.ts`；Cline：`sdk/examples/hooks/PreToolUse_ClineignoreGuard.sh` |
| 9 | headless `ee exec --json` | S | 无 | Codex：`codex-rs/exec/src/lib.rs`、`event_processor_with_jsonl_output.rs` |
| 10 | ★自动化/定时任务 v1（本地目录、默认只读、收件箱、补跑） | L | 1、2、9 | Goose Apache-2.0：`crates/goose/src/scheduler/full.rs`、`scheduler/common.rs`；Cline：`apps/cli/src/commands/schedule/handlers.ts`、`apps/cli/src/wizards/schedule/cron-presets.ts`；行为参照 CX Scheduled（RRULE、收件箱未读） |

### D5 — 并行与扩展
| 顺序 | 项目 | 难度 | 依赖 | 可改编的开源代码 |
|---|---|---|---|---|
| 1 | ★git worktree 管理（含 .worktreeinclude、删除前快照） | M | D4-2 | Codex：`codex-rs/worktree/src/{git,lib,metadata,paths}.rs`、`codex-rs/git-utils/src/worktree.rs`；OpenCode：`packages/opencode/src/worktree/index.ts` |
| 2 | ★子代理并行（只读并发；写型走 worktree） | M | 1 | Codex：`codex-rs/core/src/tools/parallel.rs`（读写锁思路）、`tools/handlers/multi_agents/spawn.rs` |
| 3 | 自动化 v2：worktree 模式 + 会话内 /loop | S | D4-10、1 | 同 D4-10 |
| 4 | ★Hooks（6 个核心事件，退出码 2 拦截） | M | 无（信任表复用 MCP） | Codex：`codex-rs/hooks/src/engine/{dispatcher,matcher,command_runner,output_parser}.rs`、`events/*.rs`；Cline：`apps/vscode/src/core/hooks/HookProcess.ts`、`hook-factory.ts` |
| 5 | Skills（SKILL.md，兼容 .claude/.agents） | M | 无 | Codex：`codex-rs/ext/skills/src/loader/{discovery,metadata}.rs`、`render.rs`；OpenCode：`packages/core/src/skill.ts` |
| 6 | ★仓库地图 | M | 无 | Aider Apache-2.0：`aider/repomap.py`、`aider/queries/tree-sitter-language-pack/*-tags.scm`（可直接搬，Python 同语言） |
| 7 | 检查点时间线 UI + 非 git 项目审阅 + 按块接受 | M | D4-2 | Cline：`apps/vscode/src/core/controller/checkpoints/checkpointRestore.ts`（三种恢复方式） |
| 8 | ★Windows 沙箱 v1（unelevated：受限令牌+ACL）+ 可选禁网 | L | 无（自动化全自动模式强烈建议先有） | Codex：`codex-rs/windows-sandbox-rs/src/{acl,allow,cap,process,env,path_normalization}.rs`、`codex-rs/core/src/windows_sandbox.rs` |
| 9 | 回答风格（讲解/作业辅导）、按用途分模型、推理强度、预算告警 | S×4 | 无 | 无需 |
| 10 | prune 旧工具输出、fork 式子代理 | S×2 | 无 | OpenCode v1：`packages/opencode/src/session/compaction.ts`（prune 段）|

### D6 — 编排与深度理解
| 顺序 | 项目 | 难度 | 依赖 | 可改编的开源代码 |
|---|---|---|---|---|
| 1 | ★指挥会话（threads_* 工具、审批冒泡、树形左栏、进度卡片） | L | D5-1、D5-2 | Codex：`codex-rs/core/src/tools/handlers/multi_agents_v2/{spawn,send_message,wait,list_agents,interrupt_agent,followup_task}.rs`、`multi_agents_spec.rs`（工具描述里的纪律）；Cline：`docs/sdk/guides/multi-agent-teams.mdx` |
| 2 | ★LSP 诊断回灌（pyright、typescript-language-server 先行）+ LSP 工具 | L | 无 | OpenCode：`packages/opencode/src/lsp/{client,server,diagnostic,launch,language}.ts`（v1.19.4） |
| 3 | best-of-n、Local↔Worktree 交接 | M | D6-1 或 D5-1 | 行为参照 CU `/best-of-n`、CX Handoff |
| 4 | Goal 模式、/side 侧聊 | M+S | 无 | Codex：`codex-rs/ext/goal/src/{runtime,steering}.rs`、`templates/goals/continuation.md`；`codex-rs/tui/src/app/side.rs` |
| 5 | 自动审批审查员 | M | D5-8 更佳 | Codex：`codex-rs/core/src/guardian/{review,prompt,decision}.rs` |
| 6 | 命令面板/快捷键自定义、Activity 视图、会话归档置顶、导出 | M | 无 | Codex：`codex-rs/tui/src/keymap/`（思路）；OpenCode：`packages/tui/src/config/keybind.ts` |
| 7 | 自动记忆提取（待确认） | M | 无 | Codex：`codex-rs/ext/memories/src/`（`local/`、`prompts.rs`） |
| 8 | 自动更新、GBK 解码回退、PAC、PR 工作流、lint/test 循环 | S–M | 无 | Aider：`aider/linter.py`（lint 循环）；electron-updater |
| 后置 | 插件包、浏览器（经 MCP）、语音、自定义工具、WSL、云任务、向量索引 | — | — | Cline `apps/vscode/src/services/browser/`；Continue `core/indexing/`（Apache-2.0，不建议做） |

关键依赖链：通知+托盘 → 自动化；磁盘快照 → 自动化 / worktree 删除前快照 / 时间线 UI / 编辑重发可靠恢复；headless exec → 自动化执行器；worktree → 并行写型子代理 → 指挥会话 / best-of-n / 自动化 worktree 模式；Windows 沙箱 → 放心开全自动 + 无人值守自动化。

## 调研来源（2026-10-26 访问）
- Codex 文档：https://developers.openai.com/codex/app/automations 、/codex/notifications.md、/codex/long-running-work.md、/codex/reference/slash-commands.md、/codex/environments/git-worktrees.md、/codex/windows/windows-sandbox.md、/codex/hooks.md、/codex/code-review.md、/codex/agent-configuration/subagents.md、/codex/agent-configuration/rules.md、/codex/customization/memories.md、/codex/sandboxing/auto-review.md（均在 developers.openai.com 下）
- Codex 源码树：https://github.com/openai/codex （main，Apache-2.0）
- Claude Code 文档：https://code.claude.com/docs/en/ 下 hooks、checkpointing、output-styles、scheduled-tasks、agent-teams、desktop、interactive-mode；CHANGELOG：https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md
- OpenCode：https://github.com/anomalyco/opencode （原 sst/opencode，MIT；tag v1.19.4 与 dev），文档 `packages/web/src/content/docs/{lsp,keybinds,themes,plugins,agents,formatters,permissions,custom-tools}.mdx`
- Cline：https://github.com/cline/cline （Apache-2.0），`docs/core-workflows/{checkpoints,plan-and-act}.mdx`、`docs/customization/{hooks,clineignore,cline-rules}.mdx`、`docs/usage/kanban.mdx`、`docs/features/auto-approve.mdx`
- Aider：https://aider.chat/docs/repomap.html 、/docs/git.html、/docs/usage/lint-test.html、/docs/usage/watch.html、/docs/usage/modes.html；源码 https://github.com/Aider-AI/aider （Apache-2.0）
- Cursor：https://cursor.com/docs/cloud-agent 、https://cursor.com/docs/bugbot 、https://cursor.com/docs/rules 、https://cursor.com/docs/configuration/worktrees 、https://cursor.com/changelog/3-0 、https://cursor.com/blog/projects
- Goose：https://github.com/aaif-goose/goose （原 block/goose，Apache-2.0）；Continue：https://github.com/continuedev/continue （Apache-2.0）

未核实/说明：Windsurf 未单独调研（功能与 Cursor 重叠）；表中开源路径来自 2026-10-26 的 GitHub 文件树，未逐个读代码确认实现细节（baseline.rs、snapshot/index.ts 已打开确认）；Claude Code、Cursor 为闭源，只列行为。
