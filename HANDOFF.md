# 接手说明（给接手的 AI / 另一个账号）

最后更新：2026-10-26 20:45（Asia/Shanghai，全仓审查第三轮：分 4 块并行审完剩余模块，修 34 个 bug；pytest 465 过 2 跳过；Windows CI 首轮暴露快照恢复 pathspec 按 glob 匹配，已加 --literal-pathspecs；下一步 D5-2）

> **仓库审查（2026-10-26 19:15）**：全量 pytest/tsc（两份）/vitest/vite build 全过；ruff 只有未用导入（已清）。发现并修复只读白名单绕过：`git -c`（注入配置执行命令）、`git diff --output`/`--ext-diff`/`git grep -O`、`rg --pre`、`sort -o`/`uniq IN OUT`/`tree -o`/`find -fprintf` 被当成只读放行，现在都拒（`tests/test_permissions.py::test_readonly_whitelist_bypass_flags`）。第二轮（19:25）：Hook 的 PreToolUse 返回 allow 会跳过危险命令的强制确认（force），已修（hooks.py adjust_decision，测试 test_hook_allow_does_not_skip_forced_ask）；已核实：永久允许规则对 `git push && curl|sh`、`git push; rm` 这类复合命令不会放行；项目 hooks 信任按路径+内容指纹、超时杀进程树、退出码 2 拦截正确；automations.json 与规则文件原子写；read-only/plan 下后台 shell、shell_write 被拒。待查（低风险）：ToolStart 显示的是 Hook 改写前的参数；PostToolUse/Stop 忽略 continue:false；Windows 取消 hook 时 taskkill 同步阻塞最多 10 秒；cron 边界、worktree 删除、bgproc 编码、repomap/docread 异常、前端四个新页面尚未逐行审。注：本文件里 18:30 之后的时间戳是并行账号写的，比实际时间超前，以 git 提交时间为准。

> **审查第三轮（2026-10-26 20:45，4 块并行，每个 bug 先写失败测试再修）**：
> - 调度/自动化：夏令时回拨时 next_run 早于当前导致每 tick 连续误触发；日期/星期字段以 `*`（含 `*/n`）开头时按 cron 惯例取“且”；非 ASCII 数字报中文错误；运行中删除自动化不再写回幽灵记录；启动时把遗留“运行中”记录标为已中断；前端预览丢弃过期结果、重开清空、每天时间非法禁止保存（tests/test_automations_review.py、AutomationsPage.test.tsx）。
> - worktree/快照：git status 失败/超时不再被当成干净而 --force 删除（会丢未提交文件）；.git 指针失效的目录不再 rmtree；删不掉时保留登记、如实报告分支是否删除；Windows 保留名加 wt- 前缀、core.longpaths；运行中对话（含子目录）拒绝合并/删除；`git worktree remove --force` 强制确认；影子仓库屏蔽项目 .gitattributes 换行/过滤；大文件排除规则转义 glob；前端切项目丢弃迟到列表（tests/test_review_worktree.py、WorktreeDialog.test.tsx）。
> - 后台命令/Hooks：ToolStart 改为显示 Hook 改写后的参数；PreToolUse/PostToolUse 的 continue:false 结束回合，Stop 的 continue:false 优先于 block 并显示原因；取消 Hook 时 Windows taskkill 放到线程不阻塞事件循环；跨块 `\r\n` 归一；删 worktree 前先结束其后台命令；前端后台输出/Hook 测试运行丢弃乱序旧结果。
> - 工具权限/仓库地图/文档读取：只读白名单再修一批绕过——`sed` 的 e/w/-f、`GIT_CONFIG_*`/`GIT_EXTERNAL_DIFF` 等前置环境变量、`1>`/`2>` 编号重定向、一个 `>/dev/null` 放过其它重定向、PowerShell `( )`/`@()`/`{ }` 脚本块、`<( )`、`sort --compress-program`、`pytest --basetemp/--junitxml`；写路径带空格/引号可绕过工作区与 deny 检查；apply_patch 用 `input` 参数时不查路径；.eeignore 被软链接绕过；读命名管道/设备文件卡死事件循环；repomap 遇管道卡死且收入项目外软链接、子目录 cwd 不认 .eeignore（tests/test_review_tools.py）。副作用：PowerShell `| Where-Object { }` 默认需确认、只读下被拒。
> - 未修、待用户/产品决定：删 worktree 时 gitignore 文件（.env）不随自动保存；/undo 与编辑重发会覆盖回合后手改的文件且不提示；快照恢复遇 Windows 占用文件只部分恢复；被 lock 的 worktree 仍会删；运行中删自动化不中断那次运行；改时区后已算好的一次仍按旧时区；Electron 强杀后端留孤儿进程（需 Job Object）；GBK 输出显示乱码不崩；Hook stdout/read 大文件无上限；docread 无超时/解压上限；web_fetch 未拦内网地址；Windows 结尾点/空格、`::$DATA`、8.3 短名可能绕过 .eeignore（未在 Windows 实测）；pytest 在只读白名单里会执行仓库代码（有意保留）。

> **接手第一步（2026-10-26 18:30）**：确认最新 Windows CI 绿；所有并行分支已合并（feat/* 可删）。之后 D5-2 子代理并行、D5-9 推理强度/按用途分模型。每个功能先对比 OpenCode/Codex/Cline/Aider/Goose/Continue 的做法，HANDOFF 写参考来源。
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
6. [x] 后台长命令（2026-10-26 17:40）：shell 工具 `background=true` 后台启动（dev server、watch、长测试、交互程序），返回后台命令 id；新工具 shell_output / shell_write / shell_kill；回合结束不杀，删除会话 / 内核退出时杀进程树；UI 列出后台命令
   - 参考了谁、取了哪点：`background` 参数 + 读增量输出（BashOutput 的“自上次读取以来的新输出”、可选正则 filter）+ 结束工具取自 Claude Code（run_in_background / BashOutput / KillShell）的思路；启动后先等几秒收集开头输出（很快结束的命令直接拿到结果）、写 stdin 后等一会儿收集回应、按会话 id 管进程取自 Codex 的 unified exec（exec_command yield_time + write_stdin）；“回合结束不杀、会话结束才杀”取自 Codex；界面上列出后台进程可看输出 / 停止取自 Cline 的“命令仍在运行，继续”与 VS Code 终端列表。OpenCode/Aider/Goose 没有后台进程管理，没取。
   - 后端：`ee/core/bgproc.py`（BgManager 每会话一个，子代理共用父会话的；stdin 独立管道、stdout+stderr 合并、增量 UTF-8 解码；内存环形缓冲 40 万字符 + 完整日志 `~/.ee/tool-output/bg-<会话随机名>-<id>.log`；模型已读游标；每会话最多 8 个同时运行；Windows CREATE_NEW_PROCESS_GROUP|CREATE_NO_WINDOW + taskkill /T，POSIX 新进程组 + killpg；PowerShell 临时 .ps1 在进程结束后删；atexit 兜底杀）。`tools/shell.py` 加 `background` 参数（先等 3 秒收集开头输出）；`tools/bgshell.py`：shell_output{id, wait≤120, filter}、shell_write{id, input, close_stdin, wait=2}、shell_kill{id}。工具表顺序：present_plan 之后、apply_patch 之前（配置 `background_shell = false` 可关）。权限：后台启动走 shell 同一套命令规则（审批标题写“后台运行命令”）；shell_output/shell_kill 一律允许（只能碰 ee 自己启动的）；shell_write 只读/计划模式拒绝，其余允许（启动时已批准，同 Codex）。thread 把启动/退出推 `shell/updated` 通知；RPC `shell/list|read|kill`（server/shell_rpc.py）；thread/delete 与 serve 退出时结束后台命令。
   - 前端：输入框工具栏「后台 N」按钮（`components/BgCommands.tsx`）：列出本会话后台命令（状态、运行时长），「输出」弹窗看最后 6 万字符（运行中每 1.5 秒刷新，显示完整日志路径），「停止」结束进程树；store `bgProcs`（打开会话时 shell/list，之后跟 shell/updated）。工具卡片：后台启动显示「后台运行 <命令> b1」，shell_output/write/kill 显示「查看了/输入到/结束了后台命令 b1」。
   - 测试：`tests/test_bgshell.py` 6 个（生命周期：开头输出/等待/写 stdin/正则过滤/退出码/日志文件/已结束不能写/未知 id；很快结束的命令；kill 与 close；权限；工具注册与开关；server：回合结束后仍在跑、shell/list|read|kill、删除会话清理）；pytest 280 通过 2 跳过，vitest 20、tsc、vite build 通过。
   - 已知/后续：后台命令是管道不是伪终端（交互程序可能缓冲输出，提示词里让模型 Python 用 -u）；内核重启后后台命令不会恢复（内核退出时已全部结束）；子代理启动的后台命令归主会话，子代理结束不杀；还没做“前台命令超时自动转后台”。
7. [x] PDF / Word / Excel / PowerPoint 读取（2026-10-26 18:00）：read 工具按扩展名自动转文字，带页码/表名；文件预览同样可看
   - 参考了谁、取了哪点：read 工具按扩展名自动抽取文字（模型不用另外调工具、不用自己写脚本）取自 Cline 的 extractTextFromFile（pdf/docx/xlsx）；PDF 的 pages 选页参数和“大 PDF 默认只给前面一段并提示用 pages 读其余”取自 Claude Code 的 Read；Excel 先列全部工作表和大小、再读指定工作表取自 Goose 的 xlsx 工具；Word 标题/列表/表格转 markdown 并保持文档顺序是 ee 自己的组合。Codex/OpenCode/Aider 不支持二进制文档，没取。
   - `ee/core/docread.py`：pypdf（每页「=== 第 N 页 ===」，pages 支持 3、2-5、1,3,7-9、8-，默认前 50 页，无文字层的页标出“可能是扫描件”，加密 PDF 试空密码）；python-docx（按 body 顺序遍历段落和表格，标题样式→#，列表→- ，表格→markdown，横向合并单元格只出一次）；openpyxl 只读 + data_only（公式取缓存值；列出全部工作表及范围；每行「行号 | A | B…」，最多 2000 个非空行、60 列，日期格式化，超出提示用 shell 跑 pandas）；python-pptx（每张幻灯片文字、表格、备注，pages 选张）；旧版 .doc/.xls/.ppt 明确报不支持；按 (路径, mtime, 大小, 参数) LRU 缓存 16 个。
   - read 工具：新参数 pages、sheet；转换在线程里跑；转换后的文字照常按行号 + offset/limit 分段（头部一行说明类型/页数/工作表）；meta.doc。提示词 read.md 写明可直接读这些格式、不要自己装库写脚本。
   - 文件预览：files/read 对这些格式返回转换后的文字（kind=text, doc=pdf|docx|xlsx|pptx），Word/PPT 按 markdown 渲染，PDF/Excel 纯文本，顶部提示“转换出的文字”；解析失败显示原因。
   - 依赖：pyproject 加 pypdf、python-docx、openpyxl、python-pptx；PyInstaller spec 加 hiddenimports（pypdf/docx/openpyxl/pptx/lxml）和 docx/pptx 的模板数据；`selftest` 增加文档组件检查（真建一个 docx/pptx，打包漏模板会直接失败）。
   - 测试：`tests/test_docread.py` 7 个（页码解析、PDF（手写最小 PDF，不依赖样本）含空页/选页/分段、docx 标题列表表格顺序、xlsx 多表/日期/按名字和序号选表/不存在的表、pptx 选张和备注、旧格式/损坏文件报错、缓存与文件变化、files 预览）；pytest 287 通过 2 跳过。
   - 已知/后续：扫描版 PDF 没有 OCR（可以让支持看图的模型看截图，后续可做“PDF 页转图片”）；.xls/.doc 旧格式不支持；Excel 图表、Word 图片不提取；python-pptx 带进 Pillow，安装包会大一些。
8. [x] 永久允许规则 + ask_user 提问工具 + .eeignore（2026-10-26 18:30）
   - 参考了谁、取了哪点：审批多一个“本项目以后都允许”、规则按项目存在用户目录（不进仓库）取自 Claude Code 的 “don't ask again for this project”（settings.local）与 Codex 的 execpolicy 规则追加；命令前缀规则沿用 ee 已有的 shell(前缀 *) 语法。ask_user：回合内等待回答、给 2～6 个选项 + 可自由回答、推荐项放第一个取自 Claude Code 的 AskUserQuestion 与 Cline 的 ask_followup_question（options）；子代理/无人值守不提供取自 Codex（exec 无交互）。.eeignore：gitignore 语法、拦读写工具、搜索结果过滤取自 Cline 的 .clineignore，文件名风格同 Aider 的 .aiderignore / Continue 的 .continueignore。
   - 永久规则：`ee/core/rules.py`（~/.ee/permissions.json，按项目规范化路径存 allow 列表，子目录继承；rule_from_decision：shell→shell(前缀 *)、工作区外写→write(目录/*)、MCP/其他工具→工具名；强制询问（无前缀）的不能永久允许）。Session 初始化时并入 policy.allow；审批选项新增 `forever`（threads.respond 接受，审批条目带 `rule` 字段供界面显示）；permissions `shell(npm run *)` 也命中裸 `npm run`。RPC `permissions/list|remove`（server/rules_rpc.py，删除时同步去掉已打开会话里的规则）。界面：审批卡片「本项目以后都允许」（悬停显示规则），设置「工具与记忆」底部「永久允许的操作」列表可删。
   - ask_user：`tools/ask.py` + 事件 `QuestionRequest`（core/events.py）；thread 里加 `question` 条目并放进 approvals（会话状态变 waiting_approval → 桌面通知“需要你回答”；中断时标 cancelled）；RPC `question/respond {thread_id, call_id, answer}`，approval/respond 不能回答提问。配置 `ask_user = true`，子代理里关闭。界面 QuestionCard：单选点选项即发送、多选勾选后提交、可自由输入；回答后折叠成一行。
   - .eeignore：`ee/core/eeignore.py`（gitignore 子集：注释、!、目录 /、锚定、* ? [] **；工作区到 git 根之间的多个 .eeignore 叠加，近的优先；按文件 mtime 缓存）。permissions 对 read/write/edit/apply_patch 命中直接拒绝（原因写明“被 .eeignore 排除，也不要用 shell 绕过”）；glob/grep 结果过滤。设置页说明里提到。
   - 测试：`tests/test_rules_ask_ignore.py` 8 个（规则推导/存取/子目录/删除、裸前缀命中、forever 全流程含新会话生效与删除同步、ask_user 全流程与中断取消、子代理无 ask_user、.eeignore 匹配与叠加、工具拦截与搜索过滤与缓存失效）；notify.test 加提问通知；pytest 295 通过 2 跳过，vitest 20。
   - 已知/后续：.eeignore 拦不住 shell 命令（只靠提示）；文件面板还不标 .eeignore；永久规则只有 allow，没有界面添加 deny；ask_user 不支持一次问多个问题。
9. [x] headless `ee exec --json`（2026-10-26，并行分支 feat/d4-9-exec 合并）：无界面跑一个回合就退出，给脚本、CI 和自动化（D4-10）当执行器
   - 参考了谁、取了哪点：子命令 `exec`、`--json` 输出 JSONL 事件流（第一行会话信息，最后一行回合结果）、`-o/--output-last-message`、`--resume <id>` / `--last` 续接、Ctrl+C 中断回合取自 Codex exec；不加 `--json` 时 stdout 只打最终回答、进度走 stderr 也取自 Codex；最后一行 `turn_done` 带最终文字、会话 id、用量、耗时、退出码，`--max-steps` 和 `--allow/--deny` 追加权限规则取自 Claude Code `-p` 的 result 消息与 `--max-turns`、`--allowedTools/--disallowedTools`；提示词可在参数里或用 `-`（或不写）从 stdin 读取自 Codex 与 OpenCode run；`--no-persist` 取自 Goose run。没取 Codex 的 `--skip-git-repo-check`（ee 默认只读，非 git 项目有磁盘快照兜底）。
   - 后端 `ee/ee/headless.py`：复用桌面端 `Thread`（子类 ExecThread），条目格式、落盘、磁盘快照、AGENTS.md、记忆、MCP 与桌面端一致；会话存 ~/.ee/sessions，meta `source="exec"`（thread/list 返回 source），桌面端能接着聊、能 /undo。建会话前 `Thread(configure=...)` 改配置：ask_user=false、--max-steps、追加 allow/deny。`--mode` 默认 read-only；default/auto 下审批由 `--approve` 决定：deny（默认）自动拒绝并告诉模型“无人值守”；auto 自动批准但强制询问类危险命令一律拒绝（ApprovalRequest 新增 `force`）。
   - JSONL 事件：session、turn_start、assistant（每步完整文字）、assistant_delta（--stream）、tool_start、tool_end（输出首尾共 4000 字）、approval、plan、subagent_start/end、notice、usage、error、turn_done{reason,text,session_id,duration_ms,usage,exit_code}。
   - 细节：计划模式最终回答 = present_plan 的方案；stdin 按 utf-8-sig 解码（去 PowerShell BOM）；`-o` UTF-8 自动建目录；退出码 0 完成 / 1 出错 / 2 参数错误 / 3 步数上限 / 4 防死循环 / 130 Ctrl+C；Ctrl+C 第一次中断回合、第二次立即退出，结束后恢复原处理器；结束时一律 `session.bg.close()` 并关闭 MCP；`--resume` 校验 id 防路径穿越。
   - 测试 `tests/test_exec.py` 11 个（纯文本/stdout 与 stderr 分离/落盘 source=exec/无 ask_user；--json --stream 事件序列；只读拒写；--approve deny/auto 与危险命令；-o 与 stdin（BOM、空输入=2）；resume/--last 与非法 id；--no-persist；退出码；Ctrl+C=130；退出时结束后台命令）。合并后 pytest 311 通过 2 跳过。
   - 已知/后续：续接时 --mode 会改写会话模式；桌面端同时开着同一会话时要重新加载才看到 exec 追加的内容；exec 不把目录加进桌面端工作区列表；暂不支持 -i 图片、--output-schema、管道内容与参数合并；一次只跑一个回合；Windows 真机 Ctrl+C/控制台编码待 CI 与用户验证。
10. [x] ★自动化 / 定时任务 v1（2026-10-26 20:10）
   - 参考：每次运行新建对话、左栏「自动化」收件箱加未读、可选新 worktree、默认最窄权限取自 Codex 桌面版；5 段 cron、按自动化存运行历史、立即运行、暂停启用取自 Goose scheduler；调度三选一并预览接下来几次取自 Cline 的 cron 预设；上次没跑完跳过本次、错过只补跑一次借 Claude Code 思路；执行器复用 D4-9 无人值守逻辑；cron 自己解析，不加依赖。
   - 后端：`core/schedule.py` 5 段 cron（`*`、范围、列表、步长、月/星期名、0 和 7 都是周日，日与星期同时限定取“或”），本地时间求下次，5 年内无触发报错；“每天”转 cron，“间隔”从上次开始运行起算。`server/automations.py`：`~/.ee/automations.json` 原子写（Windows 占用重试，坏文件改名 .bad），历史每个最多 100 条、总 1000 条；字段含项目、提示词、模型、权限模式（默认只读）、自动审批 deny/auto、worktree、超时（默认 60 分钟）、调度、启用、上次/下次时间；`ee serve` 启动 5 秒后起 asyncio 循环（最多睡 30 秒，增删改立即唤醒）；不并发（记 skipped）；next_run 已过只补跑一次（trigger=catchup）；每次新建 AutomationThread（meta source=automation、automation_id），用 `headless.unattended_configure` 关 ask_user、`headless.auto_approve` 自动审批；历史记状态、会话 id、摘要、未读，推送 `automation/*` 通知。RPC `automations/list|create|update|delete|run_now|runs|mark_read|preview`。
   - 前端：左栏「自动化」入口带未读角标；`AutomationsPage.tsx` 列表（调度说明、下次运行、上次结果、启用、立即运行、编辑、删除）+ 运行收件箱（点开会话并标已读、全部已读）+ 新建/编辑弹窗（调度三选一实时预览 3 次、模型、权限、审批、超时、worktree）；`automationStore.ts` 订阅通知，运行结束 `window.ee.notify` 弹通知；自动化产生的对话不进左栏项目列表（正在看的、搜索结果除外）。
   - 测试：`tests/test_automations.py` 25 个、`schedule.test.ts` 3 个；合并后 pytest 381 过 2 跳过、vitest 27。
   - 已知/后续：管理入口在左栏自动化页而非设置页；只在 ee 运行（含托盘）时调度，无开机自启；一次运行一个回合；worktree 模式分支不自动清理；无会话内 /loop、RRULE（D5-3）；启动补跑早于桌面端连上时那条通知会漏（角标/收件箱正常）；真机 Windows 通知与托盘调度待验证。

**D5 — 并行与扩展**
1. [x] ★git worktree 管理（2026-10-26 19:00，见下方「D5 进度」） 2. ★子代理并行（只读并发，写型走 worktree） 3. 自动化 v2（worktree 模式、会话内 /loop） 4. [x] ★Hooks 5. [x] Skills 6. [x] ★仓库地图（tree-sitter tags，2026-10-26 20:00，见「D5 进度」） 7. 检查点时间线 UI + 非 git 项目审阅 + 按块接受 8. ★Windows 沙箱 v1（受限令牌 + ACL，只能写工作区，可选禁网） 9. 回答风格（讲解/作业辅导）、按用途分模型、推理强度、预算告警 10. 修剪旧工具输出、fork 式子代理

**D5 进度**

- **D5-6 仓库地图**（合并自 feat/d5-repomap）
  - 参考：tree-sitter 定义 → 引用图 PageRank → token 预算内二分裁剪 → 按 mtime 缓存、名字加权降权取自 Aider repomap，各语言定义查询由其 tags 查询改写；按需工具、可只看子目录取自 Cline list_code_definition_names；只给签名行取自 Continue；默认不往前缀塞地图取自 Codex/OpenCode 靠工具探索的思路（省 token、保缓存）。
  - 做法：`core/repomap.py` 用 tree-sitter-language-pack 0.13.0（固定；1.x 改为运行时联网下载语言库，不适合离线）解析 Python/JS/TS/TSX/Go/Rust/Java/C/C++/C#/Ruby/PHP/Kotlin/Lua；引用统一统计标识符叶子节点，只在同语言族连边；PageRank 0.6 + 被引用广度 0.4，通用名降权、顶层 ×1.5，focus 作个性化向量；git 用 ls-files，否则 walk，跳过 SKIP_DIRS/隐藏/.eeignore/>1MB，上限 3000 文件、限时 20 秒可续；缓存 `~/.ee/cache/repomap/<sha1>.json`，语言包版本变则失效；主目录/盘根拒绝；依赖缺失不注册工具。
  - 工具 `repo_map{path?, focus?[], max_tokens?}`：只读，只读/计划模式与 explore 子代理可用，默认约 1500 token、最大 8000。配置 `[repo_map] enabled, auto_inject=false, max_tokens=1536, inject_tokens=1024, max_files=3000, time_limit=20`；auto_inject 时在记忆、技能之后注入固定 user 消息（`_sub=repomap`，会话内不变，生成限时 5 秒）。
  - 打包：spec 只 hiddenimports 13 种语言 + C#/yaml/embedded_template（约 25MB，全量约 170MB）；selftest 真解析 Python/TSX 并检查 14 种语言可加载。分支 Windows CI run 40300182937 打包+selftest 通过。
  - 测试 `tests/test_repomap.py` 13 个；1500 文件约 27 万行首次约 4.8 秒、命中缓存约 0.5 秒。
  - 已知：排名为启发式；签名只取一行；局部变量同名算引用有噪声；缓存为 JSON；无设置页开关；桌面端按普通工具显示；加语言需同时改 EXT_LANG、QUERIES、spec 的 REPOMAP_LANGS。
- [x] D5-1 git worktree 管理（2026-10-26 19:00）
  - 参考了谁、取了哪点：目录放在用户目录下按仓库分组、分支名 `ee/<名>`、元数据单独存一份索引取自 Codex 的 worktree 模块（paths/metadata）；`.worktreeinclude`（被忽略但需要的文件在新建时复制过去）取自 Claude Code 桌面版/Cursor 的同名做法；“新对话直接在新 worktree 里开始”的开关、项目菜单里的 worktree 列表取自 Codex 桌面版与 Cursor 的 `/worktree`；删除前把未提交改动自动提交到分支上（不丢活）、合并前要求主目录干净、冲突时自动 abort 是 ee 自己的保守组合（OpenCode 的 worktree 只管建删）。
  - 后端 `ee/core/worktree.py`：create（检查是 git 仓库且有提交、名字 slug、分支已存在报错、`git worktree add -b ee/<名> <路径> <base 提交>`、按 .worktreeinclude 复制 `git ls-files --others --ignored --exclude-standard` 里匹配的文件（总量 200MB 上限）、写 ~/.ee/worktrees/index.json）；list_for/status（存在否、未提交改动、相对 base 的提交数；在 worktree 里也能找到主仓库）；info_for_dir；diff（相对 base 的全部改动含未跟踪，按文件拆）；remove（先自动提交未提交改动 → 要删分支且有未合并提交时不带 force 直接拒绝、什么都不动 → worktree remove + prune → 可选删分支）；merge（主目录必须干净；--no-ff 或 --squash；冲突时 merge --abort / reset --merge 恢复原样并报错）。没配 git 身份时用 ee 兜底。RPC `worktree/list|create|diff|merge|remove`（server/worktree_rpc.py；删除前若有对话在里面运行则拒绝，删后结束其后台命令；merge 后发 `git/changed` 刷新审阅面板）。`thread/start` 新增 `worktree: true|{name,base}`；在已有 ee worktree 目录里开对话也会记上 worktree；会话 meta/summary 带 `worktree{path,branch,repo,name}` 和 `workspace`（主仓库路径）。
  - 前端：新对话时输入框工具栏「本地 / 新 worktree」切换；左栏 worktree 里的对话归到主仓库下并显示分支图标；项目「…」菜单「Worktree…」打开 `components/WorktreeDialog.tsx`（新建、在这里新对话、合并 / 压成一份、删除（可选删分支、强制））。
  - 测试 `tests/test_worktree.py` 5 个（建/include 复制/重名、列表状态与 worktree 内反查、diff、脏主目录拒绝合并、合并、删分支；删除自动保存与未合并保护；冲突自动 abort；squash 与非 git 仓库；thread/start worktree 全流程）；pytest 300 通过 2 跳过。
  - 已知/后续：worktree 的审阅面板显示的是相对 HEAD 的未提交改动（合并前想看全部改动用 worktree/diff，界面暂未接）；没有“移回本地”（handoff）；index.json 不会自动清理被手动删掉的目录（列表里显示“目录已不存在”，可删除）。

- [x] D5-4 ★Hooks（2026-10-26，并行分支 feat/d5-hooks 合并）
  - 参考了谁、取了哪点：事件名、stdin JSON 字段（hook_event_name/tool_name/tool_input/tool_response/prompt/stop_hook_active）、退出码 0 继续 / 2 拦截并把 stderr 回给模型、JSON 输出 decision/reason/additionalContext/permissionDecision/updatedInput/continue、`A|B` 与正则 matcher 借 Claude Code 思路（字段尽量兼容，现成脚本基本能直接用）；同一事件多个 hook 并行、结果合并、命令可写数组直接运行取自 Codex；超时杀进程树、出错只提示不中断取自 Cline；Stop 每回合最多连续继续 3 次、项目 hooks 按“路径 sha8:内容 sha8”允许（内容变了要重新允许、项目文件不能给自己授权）是 ee 自己的组合。
  - 后端 `core/hooks.py`：用户级 `~/.ee/config.toml` 的 `[hooks] enabled/allowed_project` + `[[hooks.<事件>]]`（matcher、command（字符串经 shell / 数组直接运行）、shell、timeout 1–600 默认 60、enabled、name；也认嵌套写法）；项目级 `<项目>/.ee/hooks.toml` 与 `.ee/config.toml` 的 hooks（cwd 往上到 git 根）；用户级与允许列表只从用户文件读。事件：SessionStart（startup/resume/clear/compact）、UserPromptSubmit（拦截时消息不进历史，TurnDone reason=blocked）、PreToolUse（在权限检查前；拦截 / allow 免审批但 deny 规则仍生效 / ask 强制审批 / updatedInput 改参数）、PostToolUse（原因与上下文追加到工具结果）、Stop（要求继续）、Notification（需要审批时后台运行不等待）。环境变量 EE_PROJECT_DIR/EE_SESSION_ID/EE_HOOK_EVENT/EE_HOOK_SOURCE；异常退出码/超时/启动失败在对话里出“Hook · 事件 · 名字”提示。子代理只跑 Pre/PostToolUse/Notification。`config.dump_toml` 支持表数组（设置页保存不写坏 `[[hooks.X]]`）。RPC `hooks/list|test`；settings 读写 hooks。
  - 前端：设置页新增「Hooks」分区 `components/HooksSettings.tsx`（总开关、我的 Hooks 按事件增删改与测试运行、项目 Hooks 允许/撤销、配置问题、写法说明）；`lib/hooks.ts`；回合结束条目显示“消息被 Hook 拦截”。
  - 顺带修复（合并时）：MCP 项目服务器的允许列表改为只从用户配置读（之前项目 `.ee/config.toml` 能写 `mcp.allowed_project` 给自己授权），test_mcp 锁住。
  - 测试：`tests/test_hooks.py` 22 个 + `lib/hooks.test.ts` 3 个。
  - 已知/后续：运行中排队插入的消息不触发 UserPromptSubmit；被拦截回合不弹桌面通知；只有 command 型，没有 http/prompt 型与 PreCompact/SessionEnd/SubagentStop；设置页保存会丢 config.toml 注释；项目 hooks 允许后要先保存才能测试。
- [x] D5-5 Skills（2026-10-26，并行分支 feat/d5-skills 合并）
  - 参考了谁、取了哪点：技能清单固定注入上下文 + 总长度预算（默认 8000 字符，超了先缩短说明再省略并注明）、使用规则（点名或任务匹配就用、相对路径相对技能目录、优先跑现成脚本）、`$名字` 显式调用把全文附进消息、`.agents/skills` 目录取自 Codex；专门的 `skill` 工具返回正文 + 目录 + 文件列表、兼容 `.claude/skills`、按技能名 deny 取自 OpenCode；SKILL.md frontmatter 约定与渐进式披露借 Claude Code 思路。选 skill 工具而不是让模型直接 read：能识别禁用、工作区外路径不用额外判断、顺带给文件列表、界面有专门卡片（清单里仍写路径，子代理可用 read）。
  - 后端 `core/skills.py`：目录优先级（低→高）`~/.agents/skills`、`~/.claude/skills` ← `~/.ee/skills` ← 项目根到 cwd 每层的 `.agents/.claude/.ee/skills`（越深越优先，同层 .ee 优先；同名覆盖记 shadows；软链接去重）；自写 YAML 子集解析 frontmatter；不合格的技能不加载并列给界面；清单作为固定 user 消息放在 AGENTS.md 与记忆之后、会话内不变（保前缀缓存）；`/名字`（开头）与 `$名字` 显式调用把全文作备注附进这条消息。`tools/skill.py` 只读，`[skills] enabled` 决定是否提供；中途新建的技能可加载但不进当前清单。配置 `[skills] enabled/compat/disabled/max_chars`；RPC `skills/list|read|dir`；子代理也有清单和工具。
  - 前端：斜杠菜单列出技能（徽标「技能」）；设置「工具与记忆」技能卡片（总开关、兼容开关、逐个启用、覆盖提示、加载错误、打开技能目录）；工具卡片“加载了技能 X”。
  - 测试：`tests/test_skills.py` 10 个 + slash.test 1 个。
  - 已知/后续：只发现一层 `<名>/SKILL.md`；输入框没有 `$` 自动补全；项目技能没有信任确认（只是说明文字，执行脚本仍过 shell 审批）；frontmatter 扩展字段（allowed-tools 等）忽略；改启用状态要新对话才生效。

**D6 — 编排与深度理解**
1. ★指挥会话：threads_list / thread_create（可指定分支，自动建 worktree）/ thread_send / thread_status / thread_wait；跨会话审批冒泡；左栏树形从属；指挥会话里每个子会话一张进度卡片 2. ★LSP 诊断回灌（pyright、typescript-language-server 先行）+ LSP 工具 3. best-of-n、本地 ↔ worktree 交接 4. Goal 模式、/side 侧聊 5. 自动审批审查员 6. 命令面板 / 快捷键自定义、活动视图、会话归档置顶、导出 7. 自动记忆提取（待用户确认） 8. 自动更新、GBK 解码回退、PAC 代理、PR 工作流、lint/test 循环
- 后置不做：插件包、浏览器（经 MCP 即可）、语音、自定义工具、WSL、云任务、向量索引。

关键依赖：通知+托盘 → 自动化；磁盘快照 → 自动化 / worktree / 时间线 / 编辑重发可靠恢复；headless exec → 自动化；worktree → 并行写型子代理 → 指挥会话 / best-of-n；Windows 沙箱 → 放心全自动 + 无人值守自动化。
