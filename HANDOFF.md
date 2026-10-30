# 接手说明（给接手的 AI / 另一个账号）

> **本文档是历史归档**，正文（「历史记录归档」之后）只用于追溯「当初为什么这么定」，
> **不是当前开工指令**。开工请先读：
> - `research\roadmap-current.md` —— 唯一当前路线入口，阶段与状态都在这里；
> - `AGENTS.md` —— 开发环境、测试怎么跑、代码约定。
>
> 正文中「当前进行中」「下一步」「未提交」等字样是记录当时的快照，已过时，
> 不能据此提交、推送或重做功能。正文里的历史测试/CI 数字未在本批复验。

## 开发环境（一句话）

一律用仓库根的 `.venv`（`./.venv/Scripts/python.exe`），不要用 PATH 里的 Anaconda `python`
（缺 openai/tree_sitter，会造成假失败）。测试跑子集、命令、约定详见 `AGENTS.md`。

## 历史记录归档（以下不再是开工指令）

历史最后更新：2026-10-28（Asia/Shanghai，**审查报告 7 个问题全部修复并推送 main** `ebf9c80`；全量 pytest **1480 passed / 15 skipped / 0 failed**（15m44s）；**Windows CI run #66 绿**；下一步 D6）

> **接手第一步（2026-10-28）**：这一轮的修复**已经合并到 main**（`90b3512`，快进合并，全量 0 failed）。
> 修的是"静默改错/静默放行"类问题（详见下面「本轮：安全与正确性六连修」）。
> **下一步**：① 确认 Windows CI 绿（推 main 会自动跑，跑完看 artifact）；② 回到 D6
> （手册 `research/d6-plan.md`），第一项是 D6-1 指挥会话——**注意 `feat/d6-1-orchestrator`
> 分支上有半成品，已按用户要求撤出，备份在 `.rivet/scratch/d6-1-wip/`（不进仓库）**。
> 真实 DeepSeek 评测仍需用户自己的 key（`eval_profiles.py --preset a2/a3 --runs 3`）。

> **本轮：安全与正确性六连修（2026-10-28，分支 `fix/edit-safety`，全部先写失败测试再修）**
>
> 起因：用户逐条报缺陷（问题 1–4），我每条都先用探针复现再改，过程中又自查出两处。六个提交：
>
> - **`3efbbe3` apply_patch 会误改**（问题 1）：`seek()` 逐级放宽 精确→rstrip→**strip**，取第一处
>   命中且不查歧义。实测：重复代码静默改第一处且**缩进被吞掉**（`    x = 1` → `x = 2`）；
>   `-return 1` 命中 `        return 1` 后 new 原样写入 → `return 2`，产出的 Python 直接语法错误。
>   修法：`seek()` 拆 `seek_hits()`/`seek()`，多处命中报歧义并列全部行号；删 strip 级；
>   新增 `_indent_hits()`/`_reindent_new()`（与 `fileops._indent_shift_matches` 同规则：只接受
>   整块统一平移）把 new 按 old→原文 偏移对齐；`@@` 定位行也要求唯一。
>   **测试 `tests/test_apply_patch_safety.py`（14 例）**。
>
> - **`d2b9aff` 动态表达式不能当 PowerShell 管道成员名**：旧结论误认为变量间接调用已被上游检查拦住，后来已纠正。
>   `_has_code_syntax` 对括号/脚本块的检查不足以覆盖变量成员名；`ForEach-Object $env:MYACTION`、
>   `$x.Kill`、`% $env:X` 需要单独识别。新增 `_is_dynamic()`，变量或动态表达式直接拒绝。
>   此段原记录混入标题导致损坏，按现有修复说明重新整理；不改变历史修复结论。
>   **测试 `tests/test_readonly_ps_pipeline.py`（93 例）**。
>
> - **`0495fd3` 补上参数冒号形式**：`-MemberName:Kill` 绕过——上一版 `low.split(":",1)[0]` 只拿
>   base 比对选项名，**冒号后的值被丢弃**。修法：base/val 分别处理；新增 `_resolve_member_opt()`
>   支持前缀缩写（≥4 字符，`-m` 会同时命中 `-MemberName`/`-Match`，不能猜）。
>
> - **`56b3c44` PowerShell 管道操作符不再放行动作**：`_READ_ONLY` 里有 `foreach-object` 等，
>   `_segment_safe` 对它们直接返回 True（`SAFE_COMMANDS[name] is None` 就放行），**完全不看参数**。
>   `Get-ChildItem C:\ -Recurse -File | ForEach-Object Delete` 只读模式直接删光。
>   修法：`_ps_pipeline_safe()` 逐参数判，`-MemberName`/`-Process`/`-ExpandProperty`/`-Begin`/`-End`
>   的值按动作判，裸词命中动词表或 Verb-Noun 即拒。`{ }` 脚本块原本就被 `_has_code_syntax` 拦着
>   （既有保守设计，未改动）。
>
> - **`9872374` 危险长选项按 GNU 前缀缩写匹配**（问题 4）：`git grep --out=` 这类写法绕过——
>   原来是 `a.startswith("--output")`，方向反了（只能匹配完整写法）。实测 13 条里 **11 条放行**，
>   含 `git log --out=`（原代码只对 diff 子命令做了判断）。修法：`_opt_is()`/`_any_opt()`，
>   方向改成 `危险选项.startswith(用户写法)`；顺带把 `--paginate` 纳入（实测当前不可利用——
>   `child_env()` 强制 `GIT_PAGER=cat`，但作为纵深防御）。
>   **测试 `tests/test_options_abbrev.py`（93 例）**。
>
> - **`fb3a019` 改为按命令的参数白名单**（问题 4 的彻底做法，用户指定）：黑名单要穷举所有危险
>   写法，白名单只要保证"里面没有能执行/写文件的选项"。新增 `SAFE_OPTS[cmd]`（50+ 命令，含
>   30 个 PowerShell cmdlet）+ `_check_opts()`/`_opt_allowed()`（GNU 唯一前缀匹配、短选项合并写、
>   `-5` 简写、PowerShell 参数大小写不敏感）。接线在 `_segment_safe` 的 `SAFE_COMMANDS` 校验之后，
>   作为前置门，不替代各命令原有的专项检查。**测试 `tests/test_safe_options.py`（171 例）**。
>
> - **`5f66f14` 写文件改为原子替换**（问题 3）：`fileops.write_text()` 直接 `open(path,"wb")`，
>   `O_TRUNC` 先清零再写——探针实测 "原始 6800 字节 → 中途失败 → 只剩 4 字节"。
>   write/edit/apply_patch 三个工具共用它。修法：改用已有的 `core.atomic.write_bytes`
>   （临时文件 + fsync + os.replace）——基础设施早就存在（`fix/redo-data` 引入，
>   config/permissions/记忆/桌面设置都在用），fileops 是漏网的一处。
>   **测试 `tests/test_write_atomic.py`（7 例）**。
>
> - **`68fa0e5` / `9fa8566` / `29d7050` 只读模式默认开沙箱 + 默认禁网 + 临时目录提示**：
>   白名单补不到底（上面四轮各补一种语法），真正的隔离是 `[sandbox]`（限制**写**，而只读模式的
>   威胁正是写）。只读/计划模式下即使 `enabled=false` 也开沙箱（`readonly_default`，默认 true），
>   默认禁网（`readonly_network_default`，默认 false=禁）。**`web_search`/`web_fetch` 不经沙箱，
>   不受影响**（有测试钉住这个前提）。工作区在 `%TEMP%` 下时给出明确提示，别让用户对着
>   "拒绝访问。"猜。**测试 `test_sandbox_readonly_default.py`(8) / `test_sandbox_readonly_network.py`(9) /
>   `test_sandbox_preflight.py`(4)**。
>
> **本轮验收**：`test_safe_options + test_options_abbrev + test_permissions + test_review_tools +
> test_redo_shell_split + test_readonly_ps_pipeline + test_hooks` = **593 passed / 6 skipped**；
> 另：沙箱相关 44 passed / 2 skipped、编辑相关 91 passed、hunks 31 passed。
> **全量 pytest：1461 passed / 15 skipped / 0 failed**（15m30s，2026-10-28 合并前跑的，零失败）。
>
> **续修：问题 5 / 6（2026-10-28，分支 `fix/worktree-loss`，已合并 main `d6e8437`）**
>
> - **问题 5（高）子代理代码会被删掉**（`core/worktree.py:finish_agent`）：
>   `_git([...], check=False)` 失败时返回空字符串，和"确实没有改动"无法区分；
>   原逻辑只看 `if not files:` 就 `remove(p, delete_branch=True, force=True)`。
>   于是 diff 超时 / git 报错 / 目录暂时不可用 → 子代理**已提交的代码**连 worktree 带分支一起被删。
>   `commits` 算出来了却从未参与删除判断。
>   修法：只有 ① diff 成功 ② 结果为空 ③ 提交列表查得到且为空，三条同时满足才清理；
>   否则保留并在返回值里给 `kept` / `kept_reason`。diff/log 改用 try/except 显式捕获。
>   **测试 `tests/test_worktree_finish_safety.py`（5 例）**。
>
> - **问题 6（中）混合换行文件改一处全文变 CRLF**（`tools/fileops.py`）：
>   原实现只要检测到任一 CRLF 就把整篇当作 CRLF，写回时统一替换换行；混合换行文件
>   （Windows 编辑器 + 脚本生成）改一行会导致全文换行变化。原损坏的内联代码片段已改为此说明。
>   修法：`TextFile` 新增 `line_eols` 逐行记原换行符，`_render` 按行还原，
>   新增行才用主风格（多数派，平手取首个出现的）。
>   **测试 `tests/test_mixed_eol.py`（7 例）**。
>
> **续修验收**：全量 pytest **1473 passed / 15 skipped / 0 failed**（15m49s）；
> 编辑链 7 个测试文件 98 passed、worktree 链 4 个文件 47 passed。
>
> **续修踩的坑（我引入又修掉的）**：⑥ `TextFile` 新字段忘了 `from dataclasses import field`，
> import 就崩；⑦ `_render` 第一版把 `line_eols` 末尾那个占位 `""` 当成可用换行符，
> 导致末尾追加一行丢掉结尾换行（`d = 4` 而非 `d = 4\n`）——被既有测试
> `test_crlf_multi_edit_keeps_crlf` 抓住。

> **问题 7：同 index 多工具调用参数串槽（2026-10-28，`ebf9c80`）**
>
> `_accumulate()` 判断"新调用"要求**旧参数已完整**（`_complete(...)`）。中转站常把两个调用
> 都放 index=0、参数分块流式、只有第一块带 id；A 还没传完 B 就到 → B 的第一块被拼进 A：
>
>     A(id=a, '{"p":') → 建槽 calls[0]
>     B(id=b, '{"q":') → A 不完整 → 仍拼进 calls[0] → A 变成 '{"p":{"q":'（非法 JSON）
>
> 修法（用户建议）：记录**每个 index 当前活跃的是哪个槽**（`active: dict[index → 下标]`）。
> 带 id 的块开新槽并接管该 index；不带 id 的块追加到 active 指向的槽。
> **测试 `tests/test_tool_call_accumulate.py`（7 例）**；基线对照确认旧逻辑只有 1 个槽且参数被污染。
> 已知限制：B 接管后无 id 的后续块无法再区分归属（协议本身没给判据），
> 截断的调用会被 `comp.tool_calls` 过滤掉，不会误执行。
>
> **审查报告其余 6 项的状态**（用户提交的 review 报告，已逐条核实并修复）：
> ①`apply_patch.seek` strip 匹配 → `3efbbe3`；②`ForEach-Object` 绕过 → `56b3c44`+`0495fd3`+`d2b9aff`；
> ③`write_text` 非原子 → `5f66f14`；④长选项缩写 → `9872374`+`fb3a019`；
> ⑤`worktree` diff 失败误删 → `d6e8437`；⑥混合换行被抹平 → `d6e8437`。
> 报告里"main 仍在 c538b52"的说法已过时——实际已推进到 `ebf9c80`。
>
> **审查报告"还没查完"的 5 项**（本轮未动，留给后续）：
> ① `threads.start_turn` 在回合运行中排队时也调用 `set_model`，可能中途换模型（未核实）；
> ② `shell:showItem` 没有路径校验（低危）；③ `rpc.ts` 换连接时旧请求要等 20 秒超时；
> ④ 删除会话时 `details`/`bgProcs` 没清理；⑤ `McpSettings` 等组件竞态、hooks/MCP 启动链路未逐行审。

> **CI 偶发失败修复（2026-10-28，`120a4fc`，CI run #66 绿）**
>
> run #64（`d6e8437`）是红的，但 run #65（`0e23ce4`，只多一个文档提交、代码完全相同）是绿的。
> 同一套代码两种结果 → 偶发。拉 CI 日志查下来，**唯一失败**是：
>
>     tests/test_errors.py::test_rate_limit_retried_honoring_retry_after
>     assert (268.156 - 267.968) >= 0.2        ← 实测 0.188s
>
> 服务端给 `retry-after-ms: 200`，产品代码确实按它 `await asyncio.sleep(delay)` 了
> （`openai_chat.py:257`），**不是产品缺陷**。0.188s 是 Windows 定时器粒度（15.6ms）
> + 事件循环取整的抖动：本机连测 8 次得 0.203/0.219 交替，CI 机器上碰到了 0.188。
>
> 修法：断言 `>= 0.2` → `>= 0.15`，并把 elapsed 带进失败消息（下次直接看到实际值）。
> **反向验证过没失效**：完全不等待时 elapsed=0.0000s 仍会被抓，正常等待 0.2030s 通过。
>
> 教训：**时间类断言不要卡在协议给的精确数值上**，要留出定时器粒度级别的余量，
> 同时确保余量仍能区分"真的做了"与"完全没做"。

> **本轮踩的坑（都是我自己引入又修掉的）**：① hunks 第一版只加 MAX_DRIFT=200 无效（实际距离仅
> 10 行，在上限内）；② 第二版改 drift=0 误伤正常场景；③ 最终按"hunk 是否有上下文行"区分才对；
> ④ `SAFE_OPTS` 初版漏了整段 PowerShell cmdlet，导致 `Get-ChildItem -Recurse | Select-Object Name`
> 被拒（6 个既有测试抓住）；⑤ `_opt_allowed` 忘了 PowerShell 参数大小写不敏感。
>
> **已知未做**：`WinError 10106`（受限令牌下 `python -m ee` 在 `%TEMP%` 起不来）
> 未根治，只在正常目录下不触发、且失败时有 `[沙箱启动失败]` 报错不会静默；`hunks._find` 的
> 相邻重复命中问题已按上下文行缓解，但内容完全重复时仍可能命中邻近处（有 8 例测试覆盖已知场景）；
> 沙箱默认只在只读/计划模式开，普通模式仍需用户在设置页或 config 里显式开。

> **[x] D5-8 Windows 命令沙箱 v1（2026-10-27 22:25 合并 feat/d5-8，22:35 加设置页开关）**：参考 Codex 的受限令牌 + ACL 思路。`[sandbox] enabled=false, network=true`。开启后 shell 前台/后台命令经隐藏子命令 `ee __sandbox_exec` 启动器运行：CreateRestrictedToken(DISABLE_MAX_PRIVILEGE|LUA_TOKEN|WRITE_RESTRICTED，限制 SID = 工作区能力 SID、登录 SID、Everyone)，CreateProcessAsUserW 挂起创建、放入 KILL_ON_JOB_CLOSE 的 Job 后恢复。只有工作区和 `~/.ee/sandbox/tmp/<id>`（TEMP/TMP）可写，读不限制；能力 SID 存 `~/.ee/sandbox/sandbox.json`，ACE 不撤销（随机 SID 只此工作区持有；每条命令撤加会很慢且并发互撤），`sandbox.revoke()` 可清理。network=false 只用环境变量尽力而为（死代理、pip/npm/cargo/uv 离线、GIT_SSH_COMMAND=false）。非 Windows 开启时结果末尾提示“未生效”；Windows 上失败报“沙箱启动失败”不回退。hooks 不纳入。项目层只能开沙箱、只能关联网（sanitize_project_layer）。Windows CI 实测：写工作区内成功/外拒绝（关沙箱对照可写）、TEMP 可写、禁网生效、子进程继承限制、后台命令受限、cmd/pwsh 工作目录正确（tests/test_sandbox.py）。设置页「工具与记忆 → 命令沙箱」：启用沙箱、允许联网两个开关，非 Windows 显示“不支持，未生效”；保存后对空闲对话立即生效、运行中的回合结束后生效（AgentSettings.test.tsx）。已知限制：不是安全边界（Everyone/登录会话可写处仍可写）；直连不走代理的程序可联网；ee 以管理员运行时只授权 Administrators 的目录在沙箱内不可读；每条命令多一个启动器进程约 0.1–0.3s。

> **审查修复重做（2026-10-27 21:45，合并 fix/redo-*）**：另一会话的 6 路审查修复（audit4）未推送丢失，按审查报告逐项重做，每个 bug 先写失败测试再修。
> - **fix/redo-security-config**：`config.sanitize_project_layer` 让项目 `.ee/config.toml` 只能收紧：丢 providers/hooks、web/network 的地址/key/代理字段、未知 provider 的模型与 headers、mcp.servers/allowed_project；更宽松的 permissions.mode 与 allow 丢弃，ask/deny 只追加，mcp.enabled/project_json 只能关；丢弃项写日志并在设置页“常规”顶部提示（`settings/get.project_ignored`）。后端口令 `take_serve_token()` 读后从 os.environ 删除，`proc.child_environ()` 在 shell/后台命令/hooks/MCP/run_capture 子进程中剔除。限制：项目层仍可改 budget、max_steps、style、shell 枚举。测试 tests/test_redo_security_config.py。
> - **fix/redo-shell-split**：pytest 移出只读白名单（会执行 conftest.py，需确认）。`PermissionPolicy.shell` 记录实际 shell，`split_command(cmd, syntax)` 按 posix/PowerShell/cmd 各自规则拆分，不知道 shell 取三套最严，引号不配对或 cmd `%VAR%` 需确认。判危险前先归一（去引号转义、去包装前缀、递归 bash -c/cmd /c/pwsh -Command/-EncodedCommand、去 git 全局选项、展开 PS 参数缩写、拆 rd/s/q），原文与归一结果都匹配 deny/强制确认；命令名不确定时全自动也强制确认。未覆盖：PS .NET 方法删除、Start-Process、splatting、cmd `!VAR!`。测试 tests/test_redo_shell_split.py（154 例）。
> - **fix/redo-edit**：edit 模糊匹配去掉逐行 trim、首尾锚点、NFKC；符号归一只用于定位；缩进只接受整体平移并按原文对齐；未改的行照抄原文，有疑义就报错并给原文（**宁可报错不误改，重点保留**）。补回 A2 压缩丢掉的工具规则（old_string 唯一或 replace_all、保留缩进、不含行号、新建用 write、不用 shell 改代码），预算不放宽（full 4421/4500，lean 1196/1200）。SYSTEM.md 只替换基础段，工具规则照常追加。eval_profiles.py：GBK 控制台不崩、超时结束进程树、不经 cmd.exe、新增 `--preset a2|a3`（worktree 检出 a6e2b22~1 / 4cc28dc~1 对比，`--base` 可自定义）。未做：apply_patch 的 trim 匹配同类风险。
> - **fix/redo-data**：/undo 与编辑重发检测回合后手改，返回 `{needs_confirm, conflicts}`，确认后先存时间线恢复点再恢复；快照恢复先预检可写，不过一个不动，中途失败如实列已恢复/未恢复；锁定 worktree 拒删，删前备份被忽略文件到 `~/.ee/worktree-backups/`（200MB 上限，失败不删）；会话 JSONL 坏行/半行跳过；`core/atomic.py` 原子写入接入 config/permissions/记忆/desktop 设置等。限制：内存快照回退无冲突检测；Windows 文件锁为模拟测试。
> - **fix/redo-kernel**：DeepSeek reasoning_content 按官方 Thinking Mode 文档在带 tools 时全部回传（`reasoning_passback="all"`）；`Session._history_lock` 串行化回合与压缩；会话文件存完整冻结前缀，重启后字节一致复用；`<think>` 只解析开头块；推理档位 DeepSeek 为 默认(high)/低/最高(max)/关闭思考，其它 provider 不变；`[prune] enabled` 默认 false（会破坏 DeepSeek 缓存）；`~/.claude/skills` 默认不读（`[skills] claude_home=true` 开启，暂无设置页开关）。
> - **fix/redo-desktop**：系统提示词分区报错根因是通用校验拒 `thread_id: null`，处理函数声明 `thread_id=None` 时放行；新增 tests/test_rpc_contract.py 自动对照前端 rpc.call 与后端签名。`shell:openPath` 拦截可执行/脚本扩展名（只在资源管理器定位），路径限 ee 目录、工作区、日志目录。`lib/errors.ts` 统一 toast + 全局未处理拒绝兜底。主进程 `electron/textdecode.ts` 流式 UTF-8 回退 GBK，内核 git/钩子/MCP 报错用 smart_decode。Ctrl/⌘+K 聚焦对话搜索。
> - 验收（合并后本地）：pytest 1035 passed / 5 skipped；vitest 37 文件 138 例；两份 tsc；vite build；e2e_ws 假模型全流程通过（本地跑需 `PYTHONPATH=ee`，且 HOME 被覆盖时 openai 须装在系统 site-packages）。待真实 DeepSeek key：`eval_profiles.py --preset a2/a3 --runs 3` 与推理回传验证。

> **本机测试环境修复（2026-10-27，三个提交）**：接手时本地全量是 `29 failed / 810 passed`，HANDOFF 记的是 `835 passed`，差额全部查清并修掉，现为 **838 passed / 13 skipped / 0 failed**（13m06s）。三类根因：
>
> **① Windows 子进程编码（真产品缺陷，`9cc8e46`）**：MCP 服务器子进程按本机代码页（中文 Windows = GBK）输出，而 MCP stdio 传输规定 UTF-8。服务器一发出中文（工具描述、工具结果常有）就写成 GBK 字节，客户端按 UTF-8 解析失败记成「stdout 非 JSON」并一直等到超时，**用户看到的是「连接超时」**。修法：`mcp/client.py:start()` 给子进程注入 `PYTHONIOENCODING=utf-8` / `PYTHONUTF8=1`（用户自配 env 在后，仍优先）。同一模式命中 `eval_profiles.py`：事件流里 usage 带 `¥`，GBK 编不出来让子进程崩在 `turn_done` 之前，成绩单被判 `crashed`。
>
> **② `user_ee_dirs()` 漏掉真实主目录（真产品缺陷，`9cc8e46`）**：原来只排除 `ee_home()` 与 `Path.home()`。工作区位于真实主目录下时（`C:\Users\alice\proj\foo` 这种很常见的布局），上层 `~/.ee/config.toml` 会被当成项目级配置再叠一遍，**静默盖掉用户级设置**。补上 `os.path.expanduser("~")` 来源；`hooks.py` 的项目文件查找、`sysprompt.py` 的项目级 SYSTEM.md 是同一不变量，一并修正。新增 `tests/test_config_scope.py`（4 例）钉住。
>
> **③ 测试依赖开发机环境（`59afb17` + `1a59975`）**：`skills.py:209` 扫 `Path.home()/.agents/skills`，开发机装了技能时每个会话开头多注入一条技能清单消息，让「history 有几条消息」的断言随机失败（CI 干净所以没暴露）。修法：`Session`/`Thread`/`Backend` 加可注入的 `user_home`，6 个创建点透传；`conftest` 加 autouse fixture —— **只替换 `Path.home` 的返回值，不动 HOME/USERPROFILE**，否则 `expanduser("~")` 会跟着漂移，真实 `~/.ee` 反而漏进排除集之外（这一条我踩过，先按改环境变量做的，结果把 ② 的 bug 现场复现了一遍）。另外 `test_bgshell` 原来用 `resolve_shell("auto")` 生成命令语法、会话却按配置用 git-bash，两者不一致时会生成错误语法（PowerShell 的 `& '...'` 交给 bash → 语法错误）；`wait_for` 默认 5s 太紧（见下）。
>
> **⚠ 本机 PowerShell 5.1 启动异常慢（环境问题，不是 ee 的 bug，未改代码）**：实测 `powershell.exe -Command "exit 0"` **4.3 秒**，而 `cmd` 0.05s、`git` 0.06s、`csc.exe`（同 .NET Framework）0.09s、`dotnet` 0.03s。换 `-NoLogo`/`-NoProfile`/`-EncodedCommand`/`-MTA`/干净环境变量/输出到 DEVNULL/代理黑洞全部不变；连跑 5 次不变快（排除 JIT）；`-ServerMode` 只要 1.75s 但无法执行命令。**用户朋友的机器上 PowerShell 正常**，所以这是本机环境问题（最可能是实时防护扫描，未验证也不建议为此关防护）。**结论：没有改 `auto` 的默认解析顺序** —— 项目默认值是对的，用户机器有差异，设置页「默认 shell」6 个选项本来就能改（`SettingsPage.tsx:451`、`settings.py:446`）。
>
> **跑测试必须用 `.venv` 的 python**：`../.venv/Scripts/python.exe -m pytest -q`。用系统 `python` 会因缺 `openai` 产生 30+ 个假失败（我接手时先撞的就是这个）。`PIP_NO_INDEX=1` 避免测试里的 pip 联网超时。

> **本轮合并验收（2026-10-27，本地 main）**：`835 passed, 12 skipped`（10m20s，`PIP_NO_INDEX=1` 避免测试里的 pip 联网超时）；前端 `vitest 106`、两份 tsc、`vite build` 通过；`ee/scripts/e2e_ws.py` 假模型全流程通过、`ee selftest` 通过。**代码提交 `ec082e1` 已推送 origin/main；之后为 HANDOFF 修订，CI 以代码提交为准。** 12 个跳过是 Windows 平台/权限差异（软链接、mkfifo、tzset、POSIX、Windows sleep 演示、8.3 短名未生成时）。

> **后端内核审查（2026-10-27，fix/audit-core，范围 core/providers/tools，每个 bug 先写失败测试，tests/test_audit_core.py）**：全量 pytest、e2e_ws 通过；ruff 0.17 默认规则报 222 条，几乎全是风格类（RUF012/S110/BLE001 等），未改，无 F 类错误。修复：① fork 子代理的 task/ask_user 占位工具没带「工具使用规则」，子代理每一步都误报“system/tools 与会话开始时不同”（_ForkBlocked 照抄 rules）；② 400 上下文超长/余额不足的错误里提到 max_tokens 等参数名（中转站常带 invalid_request_error）时被当成“参数不支持”永久去掉 max_tokens 并重试，现在 overflow/quota 不走降级（openai_chat.stream）；③ grep/glob 的纯 Python 回退（Windows 上没有 rg 时）在事件循环上同步遍历、读文件，大仓库会卡住整个后端（含中断），改到线程里，并跳过管道/设备文件；④ shell 输出按 4096 字节块各自解码，多字节中文切在块边界变成乱码，改增量解码；⑤ grep 被中断时 rg 子进程不结束；⑥ 自定义子代理定义：一个坏文件（GBK 编码/max_steps 非整数）让全部自定义子代理静默消失，UTF-8 BOM（记事本）开头的定义不被识别，值带引号不去——现在逐文件容错、BOM/GBK、去引号、记日志；⑦ edit 同一处在 edits 与顶层 old_string 各给一遍时误报重叠，去重。已核实无问题：两条队列的送入/退回时序（取出到写历史间无挂起点、TurnDone 后 drain 到任务结束间无挂起点）、修剪的批次/缓存/重放、sysprompt 信任与 BOM/CRLF、花费账本锁与原子写、tool_rules 拼装去重。未改（低风险/待定）：流式中途的 APIError（无 HTTP 状态）一律不重试；tool 消息缺 tool_call_id 时 wire_messages 每次随机补 id（破坏缓存，实际不会出现）；子代理用量合并会让修剪的 requests 计数提前前进（最多让下一次修剪略早）。

> **仓库审查（2026-10-26 19:15）**：全量 pytest/tsc（两份）/vitest/vite build 全过；ruff 只有未用导入（已清）。发现并修复只读白名单绕过：`git -c`（注入配置执行命令）、`git diff --output`/`--ext-diff`/`git grep -O`、`rg --pre`、`sort -o`/`uniq IN OUT`/`tree -o`/`find -fprintf` 被当成只读放行，现在都拒（`tests/test_permissions.py::test_readonly_whitelist_bypass_flags`）。第二轮（19:25）：Hook 的 PreToolUse 返回 allow 会跳过危险命令的强制确认（force），已修（hooks.py adjust_decision，测试 test_hook_allow_does_not_skip_forced_ask）；已核实：永久允许规则对 `git push && curl|sh`、`git push; rm` 这类复合命令不会放行；项目 hooks 信任按路径+内容指纹、超时杀进程树、退出码 2 拦截正确；automations.json 与规则文件原子写；read-only/plan 下后台 shell、shell_write 被拒。待查（低风险）：ToolStart 显示的是 Hook 改写前的参数；PostToolUse/Stop 忽略 continue:false；Windows 取消 hook 时 taskkill 同步阻塞最多 10 秒；cron 边界、worktree 删除、bgproc 编码、repomap/docread 异常、前端四个新页面尚未逐行审。注：本文件里 18:30 之后的时间戳是并行账号写的，比实际时间超前，以 git 提交时间为准。

> **审查第三轮（2026-10-26 20:45，4 块并行，每个 bug 先写失败测试再修）**：
> - 调度/自动化：夏令时回拨时 next_run 早于当前导致每 tick 连续误触发；日期/星期字段以 `*`（含 `*/n`）开头时按 cron 惯例取“且”；非 ASCII 数字报中文错误；运行中删除自动化不再写回幽灵记录；启动时把遗留“运行中”记录标为已中断；前端预览丢弃过期结果、重开清空、每天时间非法禁止保存（tests/test_automations_review.py、AutomationsPage.test.tsx）。
> - worktree/快照：git status 失败/超时不再被当成干净而 --force 删除（会丢未提交文件）；.git 指针失效的目录不再 rmtree；删不掉时保留登记、如实报告分支是否删除；Windows 保留名加 wt- 前缀、core.longpaths；运行中对话（含子目录）拒绝合并/删除；`git worktree remove --force` 强制确认；影子仓库屏蔽项目 .gitattributes 换行/过滤；大文件排除规则转义 glob；前端切项目丢弃迟到列表（tests/test_review_worktree.py、WorktreeDialog.test.tsx）。
> - 后台命令/Hooks：ToolStart 改为显示 Hook 改写后的参数；PreToolUse/PostToolUse 的 continue:false 结束回合，Stop 的 continue:false 优先于 block 并显示原因；取消 Hook 时 Windows taskkill 放到线程不阻塞事件循环；跨块 `\r\n` 归一；删 worktree 前先结束其后台命令；前端后台输出/Hook 测试运行丢弃乱序旧结果。
> - 工具权限/仓库地图/文档读取：只读白名单再修一批绕过——`sed` 的 e/w/-f、`GIT_CONFIG_*`/`GIT_EXTERNAL_DIFF` 等前置环境变量、`1>`/`2>` 编号重定向、一个 `>/dev/null` 放过其它重定向、PowerShell `( )`/`@()`/`{ }` 脚本块、`<( )`、`sort --compress-program`、`pytest --basetemp/--junitxml`；写路径带空格/引号可绕过工作区与 deny 检查；apply_patch 用 `input` 参数时不查路径；.eeignore 被软链接绕过；读命名管道/设备文件卡死事件循环；repomap 遇管道卡死且收入项目外软链接、子目录 cwd 不认 .eeignore（tests/test_review_tools.py）。副作用：PowerShell `| Where-Object { }` 默认需确认、只读下被拒。
> - 未修、待用户/产品决定：删 worktree 时 gitignore 文件（.env）不随自动保存；/undo 与编辑重发会覆盖回合后手改的文件且不提示；快照恢复遇 Windows 占用文件只部分恢复；被 lock 的 worktree 仍会删；运行中删自动化不中断那次运行；改时区后已算好的一次仍按旧时区；Electron 强杀后端留孤儿进程（需 Job Object）；GBK 输出显示乱码不崩；Hook stdout/read 大文件无上限；docread 无超时/解压上限；web_fetch 未拦内网地址；Windows 结尾点/空格、`::$DATA`、8.3 短名可能绕过 .eeignore（未在 Windows 实测）；pytest 在只读白名单里会执行仓库代码（有意保留）。

> **并行进行中（2026-10-27 14:20 更新）**：D5-7、D5-3、Pi 第二批（A6/A9/A12）已合并进本地 main；`fix/audit-desktop` 与 thread_id 高危修复也已合并。**只剩 D5-8 Windows 沙箱未完成**：远端没有 `feat/d5-8`，原 `/workspace/work/ee-feat-d5-8` 不在本机，代码需找回或重做。D6 未开始。

> **2026-10-26 23:50 状态**：Pi 第一批（A1/A2/A7/A8 精简模式与工具描述压缩、A3 edit、A4 SYSTEM.md、A5 两条队列、A10 错误分类）和 D5-10 全部合并进 main，无未合并分支；pytest 673 过 2 跳过、vitest 52。**沙箱无 DeepSeek key，以上都没做真实任务对比**：打包前用户设好 DEEPSEEK_API_KEY 后跑 `python ee/scripts/eval_profiles.py --runs 3`（full vs lean，10 个任务），再决定 lean 是否设为默认；注意 A2 压缩了 full 模式的工具描述、A3 改了 edit schema，这两项影响默认行为，若真实对比变差就回退对应提交。下一步：D5-3 自动化 v2、D5-7 检查点时间线、D5-8 Windows 沙箱、Pi 第二批（A6 details 契约、A9、A12），再 D6。
> **CI 修复（2026-10-27 00:20）**：14ce3c1/afbfc4d 起 Windows CI 失败，唯一原因是 `tests/test_sysprompt.py::test_user_system_replaces_base_and_patch_still_appended`：测试用 `write_text("…\r\n…")` 写 SYSTEM.md，Windows 文本模式又把 `\n` 译成 `\r\n`，文件实为 `\r\r\n`，读出多一个空行（产品代码按字节读并归一 CRLF，没问题）。修法：测试改用 `write_bytes(...encode("utf-8"))` 精确写 BOM+CRLF。其余 664 个测试与三个 E2E（含新加的排队/撤回/中断退回检查）在 Windows 上都过。验证：诊断分支 run 40356258429 全绿；main 上 d3559a6 的 run 40358056553 全绿。

> **接手第一步（2026-10-26 22:58 更新）**：先确认最新 Windows CI 绿（最新提交 b41f6a5「回答风格只留默认/简洁」，推送时 CI 还在跑；上一个 488233b 已绿）。用户原则：Pi 只作参考、不强行适配，没实用价值的功能宁可不要（已删内置「讲解」「作业辅导」风格）；平时不催用户真机测试，攒齐功能后统一打包测。**下一步：按 `research/pi.md` 落地 Pi 借鉴项，第一批 = 精简模式、工具描述压缩、edit 改进、SYSTEM.md / APPEND_SYSTEM.md、两条消息队列**。用户决定：自动压缩保持 80% 阈值，不加 128k 上限。每一项都必须用 DeepSeek 真实任务实测（对比改前改后的成功率 / token / 花费），不好用就不合入。之后再按路线做 D5-10（修剪旧工具输出 + fork 式子代理）、D5-3 自动化 v2、D5-7 检查点时间线、D5-8 Windows 沙箱、D6-1 指挥会话。D5-9 遗留待做见「D5 进度 · D5-9 · 剩余待做」。**用户决定（22:48）：暂不出安装包，先攒功能；Windows 真机手测（含 D5-9 新界面、真实 DeepSeek key 的推理强度/关闭思考）统一放到功能差不多后打包时一起做。**
> （旧）**接手第一步（2026-10-26 21:20）**：确认最新 Windows CI 绿（D5-2 的提交）。下一步 D5-9：回答风格（讲解/作业辅导）、按用途分模型（`[models.roles]` compact/title/explore/plan/review）、推理强度、预算告警；之后 D5-3 自动化 v2、D5-7 检查点时间线、D5-8 Windows 沙箱、D6-1 指挥会话（依赖 D5-2 已就绪）。每个功能先对比 OpenCode/Codex/Cline/Aider/Goose/Continue 的做法，HANDOFF 写参考来源。
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
- HANDOFF 自身每提交一次，远端 HEAD 会再前进一格：不要在文档里写死“当前最新 hash”。记录代码提交号、测试结果和未完成项；最新 hash 用 `git log -1 origin/main` 查。
- 推送会触发 Windows Actions，产出 artifact `ee-windows-installer`。

## 最新进度追加

- [x] 2026-10-26 13:43 免费搜索：默认 web_search 后端改为 Exa 公共 MCP（https://mcp.exa.ai/mcp，免 key，已实测可用），失败回退 DuckDuckGo HTML（仅 mock 测试，沙箱 IP 被限流）；DeepSeek 搜索改为可选付费项；web_fetch 默认直接 httpx 抓取。待办：老用户 config.toml 里已存 search_backend="deepseek" 的需迁移；search_fallback 设置页暂无选项。
- 用户决定：不急出新安装包，继续推进功能后统一出包。
- 2026-10-26 13:50 D3 方案定稿：research/d3-plan.md（A MCP、B 斜杠命令、C 配置迁移+单价、D 附件、E 分叉/编辑重发）。尚未写代码。API key 安全存储方案见 d3-plan.md F 节，未实现。2026-10-26 13:51 Hark 当日额度用尽（次日 12:17 恢复），D3 全部未写代码，接手从 C 节开始。

### 本轮：配置作用域修复（2026-10-27 14:30，**未提交**，待新会话在 ee-main 里走 deliver_task）

- **不变量**：用户级 `~/.ee` 配置只能作为用户级叠一次，绝不能被当成“项目级配置”再叠一遍。
- **缺陷**：`load_config` 用 `p.resolve() != user_cfg.resolve()` 守卫，而 `user_cfg` 来自 `ee_home()`（`EE_HOME` 可改）。只要 `EE_HOME` 指向别处（测试，或用户按 `ee/ee/config.py:6` 文档明写支持的改位置），守卫失效——真实主目录的 `~/.ee/config.toml` 被当成项目配置叠上来，**静默盖掉用户级设置**。用户改了 `EE_HOME` 后设置页存的值被忽略，是真 bug 而非测试问题。
- 同一不变量在另外两处也漏了，一并修：`ee/ee/core/hooks.py`（项目 Hooks 会把 `~/.ee/config.toml` 读成项目配置）、`ee/ee/core/sysprompt.py`（`~/.ee/SYSTEM.md` 可能被当成项目级 SYSTEM.md）。
- **做法**：`ee/ee/config.py` 新增 `user_ee_dirs()`，返回 `{ee_home(), Path.home()/".ee"}` 的解析集；`load_config`、`hooks.project_files`、`sysprompt.project_file` 三处统一按该集合排除。顺带修 `if p is user_cfg` 的身份比较（`Path` 按值比较不保证同实例），改成显式 `resolve()` 比较。
- **验证（RED→GREEN 均实测）**：新增 `ee/tests/test_config_scope.py`（4 个用例）。先红（`api_key_env` 实得 `DEEPSEEK_API_KEY`）；把 `user_ee_dirs` 临时替换成旧行为后同一用例立刻重红，证明红因来自被测层；修复后 `test_config_scope + test_secrets + test_config_shell + test_sysprompt + test_hooks` = **62 passed**，原先失败的 `test_secrets.py::test_key_priority` 已通过。
- **既有失败（与本次无关，勿误判）**：`tests/test_mcp.py` 3 个失败（`test_stdio_client_roundtrip` 等，报“连接超时”）。用 `git archive HEAD` 导出干净副本单独跑，同样 `3 failed, 6 passed` —— HEAD 上就存在。未修。
- **未提交的原因**：本会话工作区根是 `E:\ee建设`（不是 git 仓库，仓库在子目录 `ee-main`），`deliver_task` 无法建立归属基线。改动 4 个文件已全部落盘、`git status` 只有它们：`ee/ee/config.py`（+31/−2）、`ee/ee/core/hooks.py`（+6/−3）、`ee/ee/core/sysprompt.py`（+5/−2）、`ee/tests/test_config_scope.py`（新增 76 行）。
- **下一步**：在 `E:\ee建设\ee-main` 作为工作区根开新会话，`git status` 确认 4 个文件后走 `deliver_task` 提交。建议 message：`fix: 用户级 .ee 配置不再被当成项目级配置重复叠加`。


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
1. [x] ★git worktree 管理（2026-10-26 19:00，见下方「D5 进度」） 2. [x] ★子代理并行（2026-10-26 21:20，见「D5 进度」） 3. [x] 自动化 v2（worktree 模式、会话内 /loop，2026-10-27，见「D5 进度」） 4. [x] ★Hooks 5. [x] Skills 6. [x] ★仓库地图（tree-sitter tags，2026-10-26 20:00，见「D5 进度」） 7. [x] 检查点时间线 UI + 非 git 项目审阅 + 按块接受（2026-10-27，见「D5 进度」） 8. ★Windows 沙箱 v1（受限令牌 + ACL，只能写工作区，可选禁网） 9. [x] 回答风格（讲解/作业辅导）、按用途分模型、推理强度、预算告警（2026-10-26 22:45，见「D5 进度」） 10. [x] 修剪旧工具输出、fork 式子代理（2026-10-26 23:30，见「D5 进度」） 11. [x] Pi 第二批 A6/A9/A12（2026-10-27，见「D5 进度」）

**D5 进度**

- **thread_id 高危修复 + fix/audit-desktop**（2026-10-27，本地 main）
  - `thread/delete` 原来直接把 `thread_id` 拼进 `~/.ee/sessions/<id>/`：传 `..` 会 `rmtree(~/.ee)`，传空串会删掉全部会话。现在 `core/ids.py` 只允许 `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`，`Backend.call` 所有 RPC 入口校验，`_athread`/`_thread`/`thread_delete` 再校验一次；`thread_delete` 还检查解析后的路径必须直接位于 `sessions/` 下。`thread/delete`、`files/search` 移到工作线程；补齐 cwd/path/query/limit/text 参数类型校验（非法返回 -32602）。
  - `fix/audit-desktop`：后端 `--parent-pid` 父进程看门狗 + 退出超时硬退出 + 结束子孙进程（`server/lifeline.py`）；主进程重启/启动失败/退出清理；RPC 超时/断线请求不再迟到补发；旧连接状态和迟到 thread/get 不覆盖新界面；设置/MCP/Hooks/技能/记忆切换项目丢弃旧响应；审阅/提交/文件预览只认最后一次请求；发送失败/操作失败给提示。测试 `tests/test_server_audit.py`（17）、`tests/test_lifeline.py`、前端新增 rpc/store/panels/settings 测试。
  - 已知：Windows 真机未手测；父进程看门狗的实际强杀场景待验证。
- **D5-3 自动化 v2 + 会话内 /loop**（feat/d5-3，2026-10-27）
  - 自动化每次运行可新建 `auto-<自动化名>-<时间>` worktree（重名加后缀），运行记录带开始/结束/用时/用量/花费/模型/最多 4000 字日志和改动摘要（文件 +/-）；完成后可合并、压成一份或丢弃；运行中的 worktree 在 Worktree 对话框禁用合并/删除；成功且没有任何改动自动清理（**产品决策待用户确认是否保留**，目前保留）。
  - 会话内 `/loop`：支持间隔（10m/30s/2h/1d、every/每…）、每天/工作日定时；`/loop list`、`/loop stop <编号>|all`；只在内存，ee 退出即停；忙时 pending、不重复排队；会话删除一并停止。前端 LoopTray、运行历史、worktree 摘要/合并/丢弃、AutomationsPage 测试。
  - 测试：`tests/test_automations_v2.py`、`tests/test_loops.py` 26 个；前端 98 个（含 D5-7）通过。已知：Windows 真机未手测；/loop 无持久化。
- **Pi 第二批 A6/A9/A12**（feat/pi-batch2，2026-10-27）
  - A6：`ToolResult` 正式拆 `output`（给模型）和 `details`（只给界面、持久化到条目）；`tools/details.py`；read/grep/glob/shell 给结构化字段、截断信息、耗时；前端按 details 渲染、可打开全文；旧条目 meta/diff 兼容。
  - A9：压缩有上次摘要时只把新内容与旧摘要合并（`prompts/compact_update.md`）；主路保住前缀缓存、换压缩模型时只发固定消息+旧摘要、侧路只渲染新内容+`<previous-summary>`；没有新内容不压缩；记录压缩节省。80% 自动压缩阈值不变。
  - A12：`core/breakdown.py` + RPC `thread/context_breakdown`，按系统提示/工具定义/AGENTS.md/记忆/技能/仓库地图/压缩摘要/历史消息/工具输出/图片统计 token 与占比、修剪/压缩节省和阈值；点上下文圆环打开 `ContextPanel`，内含立即压缩；只读，不冻结前缀。
  - 测试：`tests/test_details.py`、`test_compaction_incremental.py`、`test_context_breakdown.py` 等 94 个相关后端用例、前端 106 个通过。已知：breakdown 是估算；A9 需真实 key 长会话验证。

- **审查第三轮遗留 · 安全与健壮性**（fix/audit-safety，2026-10-27）：修掉「审查第三轮 · 未修」里 5 项无需产品决策的。
  - 参考：Codex 的子进程输出按 UTF-8 优先、失败再按本机编码解；Apache POI 的 zip 解压比/条目上限思路；常见 SSRF 防护（解析后检查 + 每跳检查 + 连接时钉 IP）。
  - web_fetch（`tools/web.py`）：拦本机/内网/链路本地/组播/保留/云元数据（169.254.169.254、阿里云 100.100.100.200、metadata.google.internal、*.localhost），字面 IP 的宽松写法（127.1、2130706433、0x7f.0.0.1、IPv4 映射/NAT64/6to4 的 IPv6）也认；DNS 解析出的任一地址是内网就拒；重定向改手动跟随、每跳都查（最多 10 跳）；直连时在 httpcore 网络后端里连接前再解析检查并连到检查过的 IP（防 DNS rebinding，TLS SNI 不变）。198.18.0.0/15 放行（Clash 等 fake-ip）。`[web] allow_private = true` 放行（默认 false）。DeepSeek Fetch 由对方服务器抓，不拦；搜索后端不受影响。
  - docread（`core/docread.py`）：文件 ≤200MB；docx/xlsx/pptx 先读 zip 目录：条目 ≤1 万、解压后总量 ≤512MB、单条（>2MB）压缩比 ≤200；解析循环检查 60 秒截止与 400 万字输出上限；`extract_async` 用专用 2 线程池 + 等待超时，read 工具改用它（卡死的解析不占默认线程池、不卡事件循环）。PDF 的解压炸弹靠 pypdf 自带上限。
  - 输出上限：Hook stdout/stderr 各读 1MB，多的读掉丢弃并注明（`core/hooks.py` `_communicate_capped`）；shell 输出内存里只留开头+结尾各 200 万字符并注明省略（`tools/shell.py` `CappedText`）；read 超过 8MB 的文本文件逐行扫描、每行最多读 64KB，只留要显示的行（`tools/read.py` `_scan_window`）；整体读入（edit/write 等）上限 32MB（`fileops.MAX_TEXT_BYTES`）；普通 read 的读文件挪到线程。后台命令原本就有缓冲上限。
  - 解码：新 `tools/textdecode.py`（`smart_decode`、`StreamDecoder`）：先严格 UTF-8（多字节被拆在两块之间也能拼对），遇到非法 UTF-8 改用 `locale.getpreferredencoding`（本机就是 UTF-8 时用 cp936），本条输出之后一直用它。shell、后台命令、Hook 都改用它。
  - Windows 路径（新 `core/winpath.py` 纯函数 `normalize_win_path`）：去每级结尾点/空格、`:流名:$DATA` 备用数据流、`\\?\` / `\\?\UNC\` 前缀，含 `~` 时用 GetLongPathNameW 对最长存在前缀展开 8.3 短名；`.eeignore` 判断和权限的写路径（deny 规则、工作区内外、`.git` 判断忽略大小写）在 Windows 上先归一。
  - 测试：`tests/test_audit_safety_web.py`、`test_audit_safety_docread.py`、`test_audit_safety_output.py`、`test_audit_safety_winpath.py`（真实 8.3/ADS 用例只在 Windows 跑；卷上没生成短名时 skip）。
  - 已知限制：走代理时目标由代理解析，只能做请求前预检（不能防 rebinding）；DNS 解析失败时预检放行（真正连接也会失败）；allow_private 只能在 config.toml 改，设置页没加开关；超时的文档解析线程无法强杀，只能在下一个检查点停（单页 PDF 解析内部不检查）；GBK 回退是按整条输出“粘住”的，同一条输出里 UTF-8 与 GBK 混排时后半段 UTF-8 会乱码；shell 的 `emit` 实时流不受内存上限限制（界面侧自行截断）。
- **D5-7 检查点时间线 + 非 git 项目审阅 + 按块接受/拒绝**（feat/d5-7，2026-10-27）
  - 参考了谁、取了哪点：Cline 检查点取“每回合一个检查点、点开对比这一回合改了什么、恢复前先存当前状态可反悔”（ee 只恢复文件；连对话一起回退仍走编辑重发）；OpenCode snapshot 取“影子 git 仓库 tree 对比、按差异文件恢复、定期 gc”（ee 用 refs/ee/keep/* 钉住要留的 tree 再 `gc --prune=1.hour.ago`，一小时宽限防止和正在抓的快照竞争）；Codex/Cursor 取“diff 每块接受/拒绝”：拒绝 = 把这块反向应用到当前文件，接受 = 只做标记、收起这块（不碰 git 暂存区）。
  - 纯函数 `ee/core/hunks.py`：parse_hunks（只按 \n 切，保留 \r，`\ No newline at end of file` 记到对应侧）、reverse_apply/apply（先按行号精确找、再上下偏移找、再忽略 \r 找；忽略 \r 命中时换回去的行沿用文件换行风格）、fingerprint（只看块内容、去 \r、按界面文本解码，不含行号）。字节用 utf-8+surrogateescape 往返，GBK/任意字节不坏。
  - 快照 `core/snapshot.py` 新增 diff_text/numstat（缓存）/existing/gc、`repo_key()`；`core/checkpoints.py`：每个会话保留最近 keep_days 天或最近 keep_turns 个回合的 tree（含恢复点），工作区已删且无会话引用的影子仓库整个删（先去只读属性，Windows）；后端启动 2 分钟后清理一次，一天最多一次，正在运行的工作区跳过。配置 `[snapshot] keep_days = 14, keep_turns = 100`（DEFAULTS 里有）。
  - RPC `server/checkpoint_rpc.py`：checkpoint/list（回合号、时间、用户消息摘要、文件数 +/-、已撤销/进行中/已清理）、checkpoint/diff（mode=turn 回合前→后；before/after = 当前→恢复目标，确认框用）、checkpoint/restore（side=before/after；先把当前存成恢复点 meta.restore_points，最多 50 个；同步用户条目 undone 和 session.turn_trees，/undo 接着按新状态走；给模型加系统提示、清已读缓存、对话里加提示）、checkpoint/gc、review/hunk（accept/unaccept/reject）。git/diff、git/revert_file 加可选 thread_id：非 git 项目改用会话开始的快照 → 当前。“已接受”存 `~/.ee/review/accepted.json`，改动没了自动清掉。
  - 顺手修：`gitinfo._run` 加 `core.quotepath=false`，之前 git 项目里中文文件名在审阅列表里显示成 `"\344\270..."` 且单文件撤销失败（先写了失败测试）；split_diff 改为只按 \n 切。
  - 前端：右栏「改动」页头加「改动 / 时间线」切换（记在 localStorage）；非 git 项目显示“自对话开始 HH:MM”的快照对比，单文件撤销 = 恢复成对话开始时的版本；文件展开后每块有「接受」「拒绝」（拒绝先确认，已接受的块收起可撤销，文件全部接受显示 ✓）；时间线新的在上，点开看这一回合的 diff，「恢复到这回合之前/之后」「恢复到这个状态」先算出要改的文件列表再确认。文件 `components/Timeline.tsx`、`DiffView.tsx`（HunkDiff）、`RightPanel.tsx`、`lib/hunks.ts`。
  - 测试：tests/test_hunks.py 17（真实 git diff 生成，CRLF、中文、GBK/任意字节、7 种无结尾换行组合、0 上下文、随机 40 组）、tests/test_checkpoints.py 6（时间线列/对比/恢复/反悔/重启后可用、非 git 审阅 + 按块 + 撤销、git 按块含 CRLF 与未跟踪新文件、gc 保留与清理与孤儿仓库、配置容错、中文文件名）；hunks.test.ts 5、Timeline.test.tsx 4。pytest 696→（见合并提交）过 2 跳过，vitest 61。
  - 已知限制：恢复只管快照里的文件（.gitignore 忽略的、>2MB 的未跟踪文件不动）；0 上下文的纯增删块在行号过期时可能定位错（界面每次都重新取 diff，不会遇到）；内容重复的文件里偏移查找可能命中相邻的同样文本；“接受”只是标记，不暂存；时间线 diff 只读（不能在里面按块拒绝）；git 项目的时间线也可用，但审阅页仍按 HEAD；没有在界面截图检查（沙箱缺 Chromium 依赖），Windows 真机未手测。
- **Pi A2+A7+A1+A8 提示词与工具表瘦身**（合并自 feat/pi-lean，2026-10-26 23:50）
  - 参考：Pi 的“工具描述一两句、细则随启用的工具进系统提示词”和默认只开 4 个工具。
  - A2：`prompts/tools/*.md` 以 `## 规则` 分隔，前为 schema 描述（一两句），后面每条规则只在工具启用时按顺序去重拼成系统提示词「# 工具使用规则」段（`tools.tool_rules`）；base.md 去掉与工具重复的条目；参数描述去重；edit 不再说返回 diff。保留的踩坑规则：shell 语法以 context-update 为准、常驻命令用 background 不用 `&`/start/nohup、read「与上次相同」再读拿全文。
  - A7：`todo`、`present_plan` 开关（默认开；关 present_plan 时计划模式用文字给方案）；`memory.inject`/`memory.tool` 拆分（`enabled` 仍是总开关）。
  - A1：`profile = "full" | "lean"`（默认 full），会话开始定下记在 meta（旧对话按 full）；lean = read/write/edit/shell + `system/lean.md`（约 200 token），shell background 返回 pid 与日志路径；权限/Hooks/只读白名单不变；`builtin_base(profile, agent)`，SYSTEM.md 替换「内置基础段 + 工具使用规则」；`ee exec --profile`；设置页「工具与记忆 → 模式」。
  - A8：记忆、技能清单、仓库地图注入头缩短；无 skill 工具时技能规则改为用 read 读 SKILL.md。
  - 实测（DeepSeek-V3 tokenizer，无 MCP）：system+tools 7,458 → full 3,822（−49%）/ lean 1,088（−85%）。
  - 测试：`test_prefix_budget.py`（字符/2.2 近似，lean ≤1200、full ≤4500）、`test_lean_profile.py`、`test_eval_profiles.py`、AgentSettings.test.tsx。真实对比脚本 `ee/scripts/eval_profiles.py`（key 只读环境变量，10 个任务：修 bug×4、新功能、多文件×2、补测试、写文档、作业；隐藏验收测试；`--selfcheck` 不调模型检查任务有效性）。
  - 已知：lean 完成率未实测；lean 没有 memory/skill/todo/子代理工具（说“记住”存不了）；lean 结束后台命令走 shell（会弹审批），Windows 未实测；技能清单精简后触发率待验证。

- **Pi A4 SYSTEM.md / APPEND_SYSTEM.md + A10 错误分类与重试**（合并自 feat/pi-system-md，2026-10-26 23:35）
  - 参考：Pi 的系统提示词文件发现规则（项目级信任后才生效，否则全局；APPEND 同规则）与重试分类（可重试/余额不重试/上下文溢出交给压缩），只借思路。
  - A4：`core/sysprompt.py`。顺序 `<项目>/.ee/SYSTEM.md`（cwd 往上到 git 根取最近，需信任）→ `~/.ee/SYSTEM.md` → 内置 base（`context.builtin_base(profile)`）；APPEND 同规则二选一；最终 = 基础 → 模型补丁（`[system_prompt] model_patch` 可关）→ 追加 → 回答风格。信任复用 hooks 的“路径指纹:内容指纹”（`hooks.file_allow_id`），单独存 `[system_prompt] allowed_project`，只认用户级配置，内容一改需重新信任；未信任/空/>64KiB/非 UTF-8 不生效并发 Notice。会话开始冻结；无自定义时 system 逐字节不变。RPC `sysprompt/info|preview|trust|untrust|set`；设置页「系统提示词」分区；文档 `ee/docs/system-prompt.md`（系统提示词不是安全边界）。
  - A10：`net.classify` 分 rate_limit/server/timeout/network（重试）与 quota/auth/overflow/bad_request/not_found/other（不重试）；遵守 Retry-After/retry-after-ms（>60 秒不等）；余额不足、key 无效、上下文超长给中文指引；回合中上下文超长自动压缩一次再重试（尊重 compact.auto）；`ModelError.kind`。
  - 测试：test_sysprompt 11、test_errors 38、SystemPromptSettings.test。
  - 已知：项目信任按绝对路径，worktree 需单独信任；报错文本匹配靠积累；未用真实 key 实测。
- **Pi A5 两条消息队列**（合并自 feat/pi-queue，2026-10-26 23:35）
  - 参考：Pi 的 steering/follow-up，取“两条队列、one/all、中断退回输入框”。
  - 做法：`core/queue.py` InputQueue。插话在每步工具后或模型不再调工具时送入；待追加只在回合本该结束时送入（插话之后、Stop Hook 之前）；present_plan 有插话继续、仅有追加则结束并退回；审批期间插话不绕过审批。`[queue] steering_mode=all, follow_up_mode=one`（插话多是即时纠正、追加多是独立任务），设置页「常规 → 执行」可改。回合结束未送入的一律经 `QueueReturned` 退回（中断、出错、上限、Hook 等）。RPC：`turn/start` 加 `queue: steer|follow`、新增 `turn/withdraw`、通知 `turn/queue_returned`；送入后才落盘。前端：运行中 Enter 插话、Ctrl/⌘+Enter 完成后再做（发送下拉也可选），输入框上方托盘可撤回，退回内容回填输入框。顺手修：排队消息一直显示“已排队”、回合开始前排队的首条消息缺 hist。
  - 测试：test_queue 20、queue.test、QueueTray.test、e2e_ws.py 加排队/撤回/退回检查。合并后 pytest 648 过 2 跳过、vitest 50、两份 tsc 与 build 通过。
  - 已知：送入后的排队消息不能编辑重发/分叉；运行中不能排队发图片；后端崩溃时未送入的排队消息会丢；未用真实 key 做体验对比。

- **D5-10 修剪旧工具输出 + fork 式子代理**（合并自 feat/d5-10，2026-10-26 23:30）
  - 参考：修剪取自 OpenCode prune（不调模型、保护最近 2 个用户回合和约 40k token 工具输出、一批不足 20k 不修剪、不越过压缩摘要、指定工具不修剪），针对其固定 token 触发的问题改为按窗口比例且低于压缩阈值；占位写工具名/参数摘要/原长度是 ee 自加；修剪后需新用量再涨一批才再修剪，为 DeepSeek 前缀缓存设计。fork 取自 Codex fork_context 思路（继承父历史、复用缓存、只回传结果）；工具表与父逐字节相同、嵌套与提问调用时报错是为保缓存。
  - 修剪 `core/prune.py`：`[prune] enabled=true, threshold=0.6, keep_turns=2, keep_tokens=40000, min_tokens=20000, min_output_tokens=300, tools=[read, shell, shell_output, grep, glob, web_fetch, web_search, repo_map, mcp__*]`，threshold 限制在 compact.threshold−0.05 以下（自动压缩 80% 不变）；在 `_maybe_auto_compact` 开头调用。保护最近回合、近 keep_tokens 输出、最近回合提到路径的文件最后一次 read、非名单工具、小输出。JSONL 原记录不动，另写 `prune` 记录按序重放（恢复/编辑重发/回退/分叉/压缩兼容）；修剪 read 结果会清掉 read 去重；`Pruned` 提示、totals 加 `pruned`，回合结束显示“修剪省 ~N tokens”。**对 DeepSeek 不省钱**（修剪那次后半段缓存未命中，约 50–75 步回本），价值是推迟有损压缩、照顾小窗口模型；DeepSeek 1M 窗口约 60 万 token 才触发。
  - fork（`core/subagent.py`）：task 新增 `fork: boolean`，冻结前缀与工具表用主会话的；历史截到含该 task 调用的 assistant 消息并补占位结果再追加分支任务，首个请求前缀与主会话下一次请求逐字节相同；只回传 `<task_result fork="true">`；规则同普通子代理；只支持 general/explore，模型固定为主会话当前模型；前端卡片显示“分叉”。
  - 测试：test_prune 10、test_fork_subagent 6、AgentItems.test 1；合并后 pytest 579 过 2 跳过、vitest 41。
  - 已知：无设置页开关（同压缩，改 config.toml）；“仍被引用”仅按最近回合提到路径；节省量为估算；plan 模型不同时分支可能提示前缀不同；分支不沿用主会话 read 去重；未用真实 key 实测缓存命中。

- **Pi A3 edit 改进**（合并自 feat/pi-edit，2026-10-26 23:30）
  - 参考：Pi 的 edit 取三点：`edits[]` 一次多处且都对照原文件匹配、查重叠；参数容错；Unicode 符号归一后模糊匹配。“找不到给最接近原文”“不唯一列行号”是 ee 自加。
  - 做法：`fileops.parse_edits` 容错（数组/JSON 字符串/单对象，旧顶层 old_string/new_string/replace_all 与 oldText 等仍认；schema 只列 path+edits）。`fileops.apply_edits` 每处走容错链：精确 → 去行尾空白 → 符号归一 → 逐行 trim → 缩进无关 → 首尾锚点；查重叠；任一失败整体不写并列出所有出错的 edits[i]。符号归一：弯引号、破折号、特殊空格、零宽、NFKC，映射回原文位置，未改行保留原符号。找不到时 difflib 给“最接近第 L1-L2 行（相似度、首个不一致行）”+ ≤15 行带行号原文（3 万行约 0.2 秒，放线程）；不唯一给各处行号。同一步写已由 `parallel.RWGate` 串行，另加按路径进程内锁 `fileops.path_lock`。编码：CRLF/BOM 保持；GBK 写回仍 GBK（edit 新内容 GBK 表示不了则报错，write/apply_patch 改存 UTF-8）；认不出编码 edit 拒绝。
  - 测试：`tests/test_edit_multi.py` 27 个；合并后 pytest 563 过 2 跳过。
  - 已知：组合字符不互相匹配；连续破折号不合并；最接近窗口行数固定等于 old 行数；路径锁仅进程内；edit.md 里“返回附带 diff”的不实描述由 feat/pi-lean 修；**DeepSeek 真实任务前后对比未做（沙箱无 key），打包时用 eval_profiles.py 验证，若 DeepSeek 对 edits 数组不适应再回退 schema**。

- [x] D5-9 回答风格 / 按用途分模型 / 推理强度 / 花费上限（2026-10-26 22:45）
  - 参考了谁、取了哪点：
    - 推理强度：档位 + `/model` 里选强度、会话可切、写回配置按模型生效取自 Codex 的 `model_reasoning_effort`；“模型声明接受哪些推理参数、不接受就不发并提示，可配置强制”取自 Aider 的 `accepts_settings` / `--reasoning-effort`；侧路请求（标题/提交信息/压缩）不带主会话的强度参数，取自 OpenCode 修过的坑（主模型 variant 漏到 small_model 导致 400）。DeepSeek 参数按官方文档（2026-10，Thinking Mode）：`reasoning_effort` low/high/max（medium→high），关闭思考 `extra_body={"thinking":{"type":"disabled"}}`。
    - 按用途分模型：用途表的形态取自 Continue 的 roles（chat/edit/apply/summarize）与审计建议；标题用小模型取自 OpenCode `small_model`；提交信息/摘要用弱模型取自 Aider `weak_model`；计划模式单独模型取自 Cline plan/act 分模型；子代理按角色取模型取自 OpenCode agent 级 model。
    - 回答风格：讲解 / 作业辅导的思路借 Claude Code output styles（Explanatory / Learning，闭源只借思路）与 Codex Personality；注入方式是 ee 自己为 DeepSeek 前缀缓存设计的。
    - 花费上限：“达到上限暂停并问是否继续”借 Cline 系的自动批准花费上限思路与 Cursor 的 spend limit（闭源只借思路）；80% 预警、跨会话日/月账本是 ee 自己的组合。
  - 做法：
    - 推理强度（`core/reasoning.py`、档案字段 `effort_param` / `effort_values` / `thinking_param`）：档位 默认 / 低 / 中 / 高 / 关闭思考；取值优先级 会话里选的 → 按模型记住（`~/.ee/state/model_prefs.json`，原子写）→ `[models.X] reasoning_effort` → 全局 `[reasoning] effort`。DeepSeek 低/中/高 → low/high/max、可关闭思考；GPT o 系 / gpt-5、Gemini 发 `reasoning_effort`；Qwen（`enable_thinking`）、GLM / 豆包（`thinking.type`）只能关闭思考；其它不支持 → 不发、界面置灰。`openai_chat`：400 能认出参数名（reasoning_effort / thinking / enable_thinking）就记住不再发；认不出且这次带了推理参数 → 去掉推理参数重试一次，成功才写进 quirks.json 记住，再失败照常报错。用量解析 `completion_tokens_details.reasoning_tokens`，回合结束行显示“推理 N tokens”。
    - 按用途分模型：`[roles] title/compact/explore/plan/review/commit`（旧写法 `[models.roles]` 读取时并入，不会被当成模型）；`Config.role_model` 没配或配了不存在的模型 → 跟随主模型。`Session.role_spec/role_client`（按别名缓存客户端）；主循环在计划模式用 plan、/review 回合用 review（同一冻结前缀）；压缩用 compact（历史放不进它的窗口就走侧路精简记录）；提交信息 commit；只读子代理 explore（调用时指定 model 优先）；标题 title（`[title] ai=true` 时第一回合结束后生成，exec/自动化不生成）。每次请求按实际模型单价计价，`UsageStats` 改为逐条记录 + `by_model` 细分，子代理并入主会话时按条复制不重算。
    - 回答风格（`core/styles.py`、`prompts/styles/concise.md`；2026-10-26 22:55 按用户要求删掉内置「讲解」「作业辅导」，只留默认/简洁，用户可放 `~/.ee/styles/<名>.md`）：会话首个请求时风格段附在 system 末尾随前缀冻结（默认风格时 system 与以前逐字节相同）；会话中途切换不动 system，而是在下一条用户消息的 `<context-update>` 里加 `<response-style>` 子块（只追加，缓存不失效），压缩后全量附录里仍带当前风格；子代理不套风格。设置默认 `[style] default`，会话级存在 thread meta。
    - 花费上限（`core/budget.py`）：`[budget] session/daily/monthly/action(warn|pause)/warn_ratio/currency`。每日/每月账本 `~/.ee/state/spend.json`：进程内锁 + O_EXCL 锁文件（残留锁 30 秒清掉）+ 临时文件 os.replace，Windows 上读写遇 PermissionError 重试；按本机日期、只保留 62 天 / 24 个月。主会话和子代理每次模型请求前检查：到比例提示一次；100% 时 warn 提示一次继续，pause 发 `tool="budget"` 的审批卡片（继续这一回合 / 本会话不再询问 / 停止，拒绝 → TurnDone reason="budget"）。子代理共用主会话的 BudgetGuard（并行子代理同时到线只问一次）。会话花费 = 主会话含子代理合计 + 重启前最后一次 turn_end 的花费。ee exec / 自动化（`unattended_configure` 标记无人值守）到 100% 一律停止：exec 退出码 5、turn_done 带 `stop_reason`；自动化运行记录 status="budget"、摘要写原因。
    - RPC：`thread/options`、`styles/list`、`budget/status`、`reasoning/prefs|forget`；`turn/start` 支持 effort/style；`models/list` 带 `effort_support/effort_values/roles`；settings 读写 reasoning/roles/title/style/budget（含校验）。
    - 前端：输入框工具栏「推理」「风格」两个下拉（不支持的档位置灰）、`/effort`、`/style`；设置页新分区「回答与花费」`AnswerSettings.tsx`（全局默认强度 + 按模型记住的列表可删、默认风格、六个用途选模型、AI 标题开关、三档上限 + 处理方式 + 比例 + 币种 + 今日/本月已花）；`BudgetCard`；回合结束行显示推理 token、多模型时悬停看分模型花费、reason=budget；自动化状态“超出花费上限”。
  - 测试：`tests/test_reasoning_roles.py` 30 个（各家族参数、自定义参数名、400 认出参数降级、认不出时去参重试一次并记住、两次失败不记住、无推理参数不重试、三层取值、换模型取该模型档位、不支持档位不发、推理 token、侧路不带强度、角色回退、旧写法并入、侧路/提交/压缩/计划/审查/explore 子代理按角色取模型并按其单价计价、压缩模型窗口太小走侧路、models/list、thread/options 持久化、AI 标题）；`tests/test_style_budget.py` 23 个（风格列表、默认风格 system 不变、风格冻结且切换只追加、切回默认与压缩后附录、子代理无风格、账本跨日跨月、裁剪、多线程原子累加、坏文件与残留锁、80%/100% 仅提醒、暂停三种选择、日/月上限计入别的会话、跨月重置、无人值守强制停止、重启后会话花费、3 个并行子代理花费只记一次、并行子代理超限只问一次且全部停止、exec 退出码 5、exec 记账、自动化记录原因、设置校验、budget/status）；前端 `AnswerSettings.test.tsx` 2 个 + slash.test 2 个。pytest 536 过 2 跳过，vitest 40 过，tsc 两份、vite build 通过。
  - Windows CI：54d1cb7（后端）run 40339235884 全绿；488233b（前端+文档）run 40340977819 全绿（含打包 exe selftest/e2e、安装包）。
  - 已知限制：只统计与 `[budget] currency` 同币种的单价（混用 ¥/$ 模型时另一种不计入上限）；没填单价的模型花费未知、不计入；侧路请求（压缩、标题、提交信息）不在请求前检查上限（花费照常记账）；会话花费只认重启前最后一次 turn_end 的累计；“本会话不再询问”只在进程内有效；日期按本机时区；推理强度只按档案判断支持与否，中转站对同名模型行为不同要靠 400 自动降级或在 `[models.X]` 里覆盖 `effort_param`；DeepSeek 的“中”和“默认”实际都是 high；新对话下拉显示的强度在发首条消息前是前端推算的（按模型记住 → 全局默认）；风格切换后 system 里的旧风格段仍在（靠追加说明覆盖），换会话才真正干净；AI 标题默认关闭。
  - 剩余待做（用户额度紧，未做）：真机 Windows 上未手测新下拉与设置分区；未用 DeepSeek 真实 key 验证 `reasoning_effort=max` / `thinking.disabled` 的实际效果和费用；可考虑让侧路请求也受上限约束、给 exec 加 `--max-cost` 命令行参数。

- [x] D5-2 ★子代理并行（2026-10-26 21:20）
  - 参考了谁、取了哪点：一步内所有工具调用同时派发、每个调用按“能否并行”拿共享读写锁的读锁或写锁、tokio RwLock 先到先得（写不会被后来的读饿死，读写读不会被重排）、结果按原顺序收集、中断时给每个未完成调用补“已中断”结果取自 Codex `tools/parallel.rs`；子代理并发上限（Codex `agents.max_threads` 默认 6，超出排队而不是报错）取自 Codex multi_agents，ee 默认 3（DeepSeek 并发和花费更敏感）；每个 task 调用就是一个独立子会话、审批按子会话区分、同一步多个 task 直接并发取自 OpenCode 的 task 工具；只读子代理放心并行、每个子代理单独统计 token/花费并实时汇总到任务总花费、界面逐个显示进度取自 Cline 的 use_subagents；写型并行“每个任务一个 worktree、结束后人决定合并”取自 Cline Kanban / Codex 桌面版的 worktree 线程。没取：OpenCode 让写型子代理直接在同一目录并发（会互相踩文件），Codex 的 spawn/wait 异步模型（留给 D6-1 指挥会话）。
  - 做法（后端）：
    - `core/parallel.py` `RWGate`：FIFO 公平异步读写锁，三类访问 read（可并发）/ write（独占、保序）/ free（不过闸门）；等待中被取消会让出队列。`Session._dispatch` 取代原来“连续只读分组 gather”：一步里所有调用 `ensure_future` 同时派发，闸门在 PreToolUse 之前获取（写类调用的 Hook、ToolStart 也按顺序）；单个调用抛意外异常只记它自己的错误结果；取消时 cancel 所有子任务再等它们收尾；结果仍按调用顺序回填历史。工具可定义 `access(args, session)` 动态决定访问类型（默认看 read_only）。
    - `core/subagent.py`：`access()` 判定 task 调用：explore / mode=read-only / 主会话只读或计划 / 只有只读工具的自定义子代理 → read（并发）；其余 → write。`plan_step()`：同一步有 ≥2 个写型 task（或显式 `isolation=worktree`）且 `[subagent] worktree=true`、是 git 仓库且有提交 → 这些 task 走 worktree 并改为 free；否则原地按顺序并出提示“N 个会改文件的子代理将按顺序执行（原因）”。单个写型 task 仍原地执行（与以前一致，/undo 能撤）。并发名额 `asyncio.Semaphore([subagent].max_parallel)`（默认 3，1～8），名额满时先发 `SubagentStart(status="queued")`，排队中被中断也会发 `SubagentEnd(interrupted)`。
    - 写型 worktree（`core/worktree.py` 新增 `agent_eligible` / `create_for_agent` / `finish_agent`）：基于当前 HEAD 建 `ee/agent-<时间>-<随机>` worktree（复用 D5-1 的 create，登记在 index.json，Worktree 对话框里能看到）；把主目录相对 HEAD 的未提交改动（`git diff HEAD --binary` 用 `apply --cached` + `checkout-index` 应用，绕开 Windows 换行转换；未跟踪文件直接复制）做成“基线提交”，子代理在它之上干活；子代理 cwd 对应主目录里的子目录；权限换成以 worktree 为工作区的新策略、会话内“以后都行”与主会话共享；不共享主会话快照（主会话 /undo 不碰 worktree）。结束时把改动提交到分支、按基线统计 numstat 和提交列表，一点没改就自动删 worktree 和分支；`<task_result>` 后追加 `[worktree]` 段（分支、目录、文件 +/-、查看/合并命令），提示主代理合并前先问用户。git 操作用进程内锁串行，避免并行建 worktree 时的 ref/config 锁冲突。
    - 只读/计划模式：`isolation=worktree` 的 task 直接拒绝（提示切模式或用 explore）；其它子代理照旧强制只读（写操作被权限层拒）。
    - 审批：`ApprovalRequest.source = {call_id, agent, description}`，call_id 仍是 `父/子`，并行时每个子代理一张审批卡、互不串台；threads 审批条目带 `source`，headless JSONL 的 approval 也带。
    - 用量：子代理每次 Usage 增量并入主会话统计（`_merge_usage` 可多次调用），发 `SubagentUpdate(kind="usage", usage=子代理, totals=主会话)`，threads 据此推 `thread/usage`，顶部花费实时涨；记录里有每个子代理自己的用量。loopguard 每个子会话各自一份，事件都带父 task 的 call_id（threads 挂到 `tool_<call_id>` 条目的 meta.subagent，记录新增 `id`、`worktree`）。
    - 配置/设置：`[subagent] max_parallel = 3`、`worktree = true`；settings/get|set 读写并校验（1～8）。task 工具新参数 `isolation: auto|worktree|none`，工具说明里写了并行规则。
  - 前端：`lib/reduce.ts` 连续的 task 条目合成 `agents` 块、`subagentStatus()`（记录停在运行/排队但条目已结束时以条目为准）；`SubagentGroup`（组头“N 个子代理并行 · 2 个进行中，1 个排队中”）+ `SubagentCard` 增加排队状态、worktree 分支与改动文件列表、模型与 token/花费；审批卡片右上角“来自子代理 X · 描述”；设置「工具与记忆 → 子代理」加“同时运行数”“写型子代理用 worktree 并行”。
  - 测试：`tests/test_subagent_parallel.py` 17 个（闸门 FIFO 顺序与取消、普通工具读并发/写独占/结果顺序、同一步读写读看到写入、3 个只读子代理确实重叠且结果按序、上限 2 时峰值 2 且 2 个排队、中断传到运行中和排队中的子代理、一个子代理请求失败不影响另一个、每个调用都跑 PreToolUse、两个写型子代理在 worktree 里并行且主目录不变、分支里带基线、无改动自动删、非 git 退回串行、单个写型原地、read-only/plan 拒绝 worktree 子代理、两个审批同时挂起可区分来源、服务端两张卡片同时 running 与 thread/usage、设置读写）；`AgentItems.test.tsx` 2 个 + reduce.test 1 个。pytest 483 过 2 跳过，vitest 36 过，tsc 两份、vite build 通过。
  - Windows CI：第 1 轮（c227a1f，run 40329553594）唯一失败是“两个写型子代理时间重叠”的断言——Windows 上建 worktree 约 2 秒且串行，第一个子代理的首个请求在第二个开始前就结束了；测试改为会合点（A 的首个请求要等 B 也开始请求，串行就会等满 30 秒记为失败）。第 2 轮（315d783，run 40331422411）后端测试在 74%～89% 那一段（test_secrets 末尾 → test_subagent_parallel）卡住 10 分钟以上，已取消；之后给 CI 的后端测试加了 20 分钟上限和 `-o faulthandler_timeout=150`（卡住会打印所有线程调用栈）。第 3 轮（37b3484，run 40333619262）全绿（pytest 476 过 9 跳过）。为复现那次卡住，在临时分支上用 Windows 跑了 6×4 + 4×4 轮相关测试文件（约 1800 次用例执行），一次都没复现，临时分支已删。**未解决风险**：那次卡住原因不明（可能是并行改动引入的偶发死锁，也可能是 test_server 等旧测试的偶发问题）；以后 CI 再卡住，看日志里 faulthandler 打出的调用栈定位。
  - 已知限制：worktree 子代理不加载项目 Hooks（信任按路径登记，worktree 路径不同）且 MCP 服务器仍以主目录为工作目录；分支里带的“基线提交”意味着主目录有未提交改动时直接 `git merge` 会要求先提交（Worktree 对话框合并要求主目录干净），只取子代理改动可用 `git cherry-pick --no-commit <基线>..<分支>`（报告里给了命令）；被中断的 worktree 子代理不自动提交、目录保留待用户处理；写型子代理之间看不到彼此改动，改同一文件需人工合并；普通只读工具没有并发上限（一步里模型给多少就并发多少，和以前一样）；并行子代理的 PreToolUse Hook 也是并发执行的，写全局状态的 Hook 脚本需自己加锁。

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
