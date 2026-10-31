# ee agent 全面体检报告

> **这是一份审计记录，不是待办清单。** 基线是 commit `1ba804b`，行号以那版为准，容易漂移。
> 文中 P0/P1 大部分已修（见文末「已修复清单」），P2/P3 多为静态推断、**未经复现**。
> **读之前先看「复核修正」章节**：实测推翻了原文 7 条结论，其中 5 条是「看到现象就推断根因」。
> 想做事请读 `roadmap-current.md`；这份文件的价值是解释「当初为什么这么判断」以及「哪些判断是错的」。

体检对象：`E:\ee建设\ee-main`（ee/ Python 内核 + desktop/ Electron）
版本：commit `1ba804b`（2026-10-31）。行号以这个版本为准，改动后可能漂移。
方法：7 路并行静态阅读（约 500 个文件），关键结论已回查源码行号。

**复现脚本**（可直接跑）：
- `D:\ee日常\probe_perm.py`：权限绕过
- `D:\ee日常\probe_wire.py`：随机 tool_call_id
- `D:\ee日常\probe_orphan.py`：孤儿 tool 消息
- `D:\ee日常\probe_sandbox.py`：只读沙箱下 Git Bash 失败
运行：`cd E:\ee建设\ee-main && ./.venv/Scripts/python.exe D:\ee日常\probe_perm.py`

**怎么用这份报告**：P0/P1 的定位和影响可以直接开工；P2/P3 里少数条目是静态推断，动手前先按条目里的复现方式确认一次。桌面端未实机跑 UI，相关结论来自代码。

---

## 修复状态（2026-10-31 16:30，commit `d66f1e0`）

**已修并实测通过**：
- P0-1 孤儿 tool_calls：`load_messages` 补配对，`probe_orphan.py` 返回 3 条含 tool，True。
- P0-2/P0-3 权限连写绕过：`probe_perm.py` plan 模式 4 条 deny、default 4 条 ask。
- P1-15b 计价假红：conftest 钉折扣 1.0，7 failed → 54 passed（修复者自测）。
- P1-4/5/6/7/9/13/14/15、P2-23/24、P1-11（泄漏部分）、P1-12：对应提交已合入。
- P0-3b-A：helper 改为 `python -P -m ee`，工作区同名 `ee/` 不再劫持 import。

**仍未修/只绕过**：
- P0-3b-B：只读沙箱下 Git Bash 仍报 `CreateFileMapping ... Win32 error 5`（`probe_sandbox.py` 仍 exit 256）。修复方式是 `explore` 子代理不再带 `shell`，但 `plan` 模式主会话、自定义只读子代理仍会踩到。
- C1 随机 tool_call_id：`probe_wire.py` 仍 `two wires identical: False`。
- C2/C3/C4/C5/C6/C7/C8 六条缓存专项均未动。
- P2/P3 其余条目未动。

**待核实**：`fedf2e4` 称「子代理报告全量进主上下文不成立，80006 字 → 771 字」。我实测 `truncate_output_info` 是 5 万中文字 → 8249 字、8 万 ASCII → 24633 字；但两条路径可能不同（工具结果 vs task 结果），先不确认谁对，等实际复现后再定。

---

**实测补充（主代理在本机跑）**：
- 首轮（`1ba804b`）：`1910 passed, 7 failed, 16 skipped`，1058s。7 个失败同一根因：`config.py:80` 默认 `offpeak_discount = 0.5`，周末/夜间测试断言全价。
- 修复后（`d66f1e0`）：**`1934 passed, 16 skipped, 0 failed`，1023s**。
- 首轮已实测复现：`git -ccore.fsmonitor=... status` plan 模式 allow；`sort -no` plan 模式 allow；`wire_messages` 两次输出不同；`load_messages` 不补孤儿；只读沙箱下 Git Bash 报 `CreateFileMapping ... Win32 error 5`。修复后前者均已转 deny/ask/补齐，后两项仍未变。
- 未复现：孤儿 tool_calls 导致的服务端 400（需要真实 API 调用）。


---

## P0 — 阻断级，建议立刻修

### 1. 崩溃/关窗后历史留下孤儿 tool_calls，会话永久 400
- 位置：`ee/ee/core/session.py:1215`（assistant(tool_calls) 先落盘）、`session.py:1263`（tool 结果后落盘）、`ee/ee/core/store.py:109-121`（load_messages 原样恢复，无配对修复）
- 现象：两者之间是整段工具执行时间。关掉桌面端 / 进程被杀时正在跑工具，这条会话之后每次请求都被服务端拒绝（assistant tool_calls 必须跟 tool 消息），界面只有一条 400，用户只能删会话。
- 建议：`load_messages` 收尾扫描，给缺失的 tool_call_id 补一条「（回合被中断，此调用没有结果）」；同时把 `_turn` 的 `_fill_missing` 放进 `finally`（`session.py:1154-1162` 现在只覆盖 CancelledError/Exception，MemoryError 等路径漏掉）。
- 复现：跑一个长工具 → 直接杀后端 → 重开会话发消息。

### 2. 只读模式可被 `git -c` 连写绕过，静默执行任意程序
- 位置：`ee/ee/core/permissions.py:1258`（`any(a in ("-c","-O") for a in args)` 只认独立写法）、`permissions.py:904-909`（短选项回退 `-{body[0]}` 命中即放行）
- 现象：`git -ccore.fsmonitor=<程序> status` 被 `-c` 首字符回退判为合法，只读模式直接 allow；`core.fsmonitor` 可执行外部程序。
- 建议：短选项按 getopt 语义解析（`-cX` ≡ `-c X`）；`-c`/`-O` 改 `startswith`；给「带值选项」加簇级扫描。
- 复现：只读模式执行上面那条命令。

### 3. `sort -no` 静默写文件（含只读模式）
- 位置：`permissions.py:1251-1253`（只查 `a.startswith("-o")`）+ `permissions.py:904-909`
- 现象：`sort -no secret.txt data` = `-n -o secret.txt`，被整簇放行；Windows 沙箱只挡工作区外，工作区内文件照样被覆盖。
- 建议：同上；`-o` 类选项出现即拒（簇级扫描），补 `sort -o` 连写的回归测试。

### 3b. 只读子代理在 Windows 上无法使用 shell（task 默认只读）
- 位置：`ee/ee/tools/shell.py:68-80`（`sandbox_plan` 对只读模式默认开沙箱）、`ee/ee/core/sandbox.py:201-232`（readonly 时即使 `enabled=false` 也 active）、`ee/ee/core/sandbox.py:182-189`（包装成 helper + 受限令牌）。
- 现象：受限令牌下启动 Git Bash，MSYS2 创建共享内存映射失败：`CreateFileMapping S-1-5-21-...-1001.1, Win32 error 5`。所有 `task` 子代理的 `shell` 工具报错，用户表现为“子代理坏了”。主会话 default 模式不走沙箱，所以正常。
- 复现：`D:\ee日常\probe_sandbox.py`（`sandbox active = True`，exit 256）。
- 建议：给 MSYS2 的共享内存/命名对象授权；或只读沙箱对 `bash.exe` 降级为“非受限令牌 + 文件 ACL 限制”并明确提示；至少检测到 Git Bash 时告知“只读子代理的 shell 不可用”，不要只报 Win32 错误。

---

## P1 — 本迭代内修

### 4. 回合运行中收到排队消息会中途换模型/权限
- `ee/ee/server/threads.py:379-384`：`set_model`/`set_mode` 在 `if self.running`（:396）之前执行；UI 每条消息都带 model。
- 影响：下一步冻结新前缀（整段缓存失效、双倍计费），已发出的 tool_call 可能变「未知工具」。
- 建议：running 时把 model/mode 挂到队列项，回合结束再生效。

### 5. 审批/预算询问无超时，一直占着 `_history_lock`
- `session.py:1466`（`await fut`）、`session.py:392`（预算 ask）都没有超时。
- 影响：会话永久 running，`/compact` 和下一回合全堵在锁后；无人值守会一直挂。
- 建议：`asyncio.wait_for`，超时按 deny 并提示。

### 6. 中断回合的 `/undo` 不做冲突检测，静默覆盖用户手改
- `session.py:617-630`（`if after and nxt`）、`:642-643`（`after = e.get("after") or cur`）。
- 影响：`tree_after` 为空时把 `before → 当前` 全部回滚，含用户崩溃后手改，且不弹确认。
- 建议：`after is None` 走强制确认路径，至少提示「本回合没有结束快照」。

### 7. 快照恢复与并行写没有互斥
- `ee/ee/server/checkpoint_rpc.py:242-250`、`ee/ee/server/app.py:546`、`ee/ee/core/snapshot.py:278-333`。
- 影响：同 cwd 两个会话、或子代理正在写文件时恢复，会把对方成果盖掉。
- 建议：按 worktree 加进程级写锁；恢复前要求同 cwd 无 running 会话。

### 8. 子会话审批在父界面完全不可见
- `threads.py:720-734` 推 `thread/child_approval`，`desktop/src/lib/store.ts:874-883` 写入 `pendingChildApprovals`，但 `desktop/src/components/` 没有任何组件读它。
- 影响：子会话卡审批时父界面只有侧栏灰字，注释承诺的「不能静默卡到超时」没兑现。
- 建议：父界面渲染横幅/浮层，直接调 `approval/respond`。

### 9. `thread/status`、`thread/children` 同步阻塞事件循环
- `ee/ee/server/app.py:402`、`:417` 是普通 `def`；`orchestrator.py:109` 遍历所有会话文件逐个完整解析，`:290-299` 起 `git status` 子进程。
- 影响：期间所有窗口的流式 token 停摆；前端对每个子会话并发调用 status。
- 建议：改 `async def` + `to_thread`；`list_children` 只读 meta 尾部。

### 10. 内核重启不等旧后端退出，两个后端并存
- `desktop/electron/backend.ts:88-92`（`restart()` 先 `stop()` 再立刻 `start()`）、`:219-233`（`stop()` 只关 stdin，强杀等 5 秒）。
- 影响：旧后端仍在写 `sessions/*.jsonl`，新后端同时写同一会话，交错追加。
- 建议：`restart()` 改 async，等 `stopAndWait` 完成再 spawn。

### 11. LSP 进程与管理器泄漏，且诊断几乎拿不到
- 泄漏：`session.py:748-768` 每会话 new manager + acquire；`core/lsp/manager.py:84-92/152-168` 的 release/shutdown 全仓无调用。
- 不可用：`tools/lsp.py:45-57` 只读 store，不发 didOpen、不等待；`Deadline`/`set_status` 是死代码。
- 建议：会话销毁/退出时 release+shutdown（或按工作区全局单例）；`lsp` 工具按 Deadline 触发 didOpen 并等待。

### 12. MCP 取消/启动失败后不回收、不重试，整会话没有 MCP 工具
- `session.py:778-780` 先置 `_mcp_done=True` 再 connect；`ee/ee/mcp/manager.py:96-117` 只 catch `(McpError, TimeoutError, OSError)`，取消时 client 不 close。
- 建议：`except BaseException` 关闭并清状态；把 `_mcp_done` 拆成「已尝试/已成功」，允许后续回合重试。

### 13. 流被截断但无 finish_reason 时被当成成功
- `ee/ee/core/providers/openai_chat.py:280-346`：循环正常结束就 `yield done`；`session.py:1120-1127` 按 completed 收尾。`_complete()`（`openai_chat.py:419`）只有测试在用。
- 影响：半截回答入历史、不重试、不报错。
- 建议：done 前若 `finish_reason is None and (content or tool_calls)` 抛可重试错误；`length` 触发一次压缩重试或明确提示。

### 14. 流式中途失败时已显示的回答不进历史，也不入账
- `session.py:1187-1207`：只有 CancelledError 分支落库，普通异常直接抛，`:1210-1224` 整段跳过。
- 影响：界面有半段回答、历史没有；该请求 usage 也不记（`usage_missing` 不增）。
- 建议：合并成 finally 落库（加 `_partial` 标记），并按 `usage is None` 补一条缺失记录。

### 15. hooks 的「绝不抛异常」是假的
- `ee/ee/core/hooks.py:465-516` 只 catch 启动期异常；`_communicate_capped`（:391-408）的 `ConnectionResetError` 会冒泡；`fire`（:657）用无 `return_exceptions` 的 gather。
- 影响：一个崩溃的 hook 会把整个回合打成 error。
- 建议：`run_hook` 外层 `except Exception`；`fire` 用 `return_exceptions=True`。

### 15b. 7 个测试依赖真实时间，周末/夜间必红
- 位置：`ee/ee/config.py:80`（默认 `offpeak_discount = 0.5`）、`ee/ee/core/providers/pricing.py:45-52`、`ee/tests/test_style_budget.py`、`ee/tests/test_reasoning_roles.py`。
- 现象：测试断言全价，但折扣按真实 `datetime.now()` 算；周六、节假日、工作日 18:00 后跑都会失败（实测 7 failed）。
- 建议：测试里把 `pricing.offpeak_discount` 置 1.0，或注入固定 clock；同时给 `pricing` 加一个可注入的 `now` 参数。

---

## P2 — 下一迭代

### 16. 400 参数降级永久写盘，无重置入口
- `openai_chat.py:238-249` 命中即写 `state/quirks.json`；`:158` 不再发 `stream_options`；`:86-93` 只有写没有清。
- 风险：把「参数值非法」误判成「不支持」，以后永远拿不到 usage；换 key/中转后旧怪癖仍生效。
- 建议：区分「不存在」与「值非法」；设置页加「重置模型怪癖」；`no_stream_options` 单独提示。

### 17. 预算只按单一币种判定
- `ee/ee/core/budget.py:217-219`（默认 ¥）、`session.py:368`（按 price.currency 记账）。
- 影响：某模型配成 `$` 后，花费进 `$` 桶，上限永不触发。
- 建议：遍历所有币种，或混币时明确报「上限不可用」。

### 18. 计价口径缺「缓存写入」，`cache_read` 缺省退化成原价
- `ee/ee/core/providers/profiles.py:24-33`：没配 `cache_read` 时命中按未命中价；无 `cache_write`；`hit > prompt` 时差额被吞。
- 建议：`cache_read is None` 时按 0 计费并标记「缓存价未知」；`Price` 增加 `cache_write`。

### 19. 错峰折扣把本地时间当北京时间，节假日只写死 2026
- `session.py:350-353`（`datetime.now()` 无 tz）、`pricing.py:45-52`、`:15-23`。
- 影响：非 +8 时区判反；2027 年起节假日全当工作日；跨边界长请求按结束时刻计价。
- 建议：`ZoneInfo("Asia/Shanghai")`；节假日表外置并标年份。

### 20. 溢出触发的压缩必定先白撞一次
- `session.py:1164-1178` → `_compact` → `_summarize`（:1545-1569）用同一份历史同模型再发，必然再 overflow，才退侧路。
- 建议：`reason == "overflow"` 直接走侧路，或发请求前按窗口预检。

### 21. 侧路请求污染 `last_prompt/last_completion`
- `session.py:1469-1485` 只 `_charge`；`context.py:185-213` 无条件写 last_*。
- 影响：上下文占用、修剪判断、压缩触发全部失真；`subagent.py:525/534` 专门保存恢复过这两个字段，说明作者知道坑。
- 建议：`side=True` 时只累加不写 last_*。

### 22. 修剪直接改服务端的 `last_prompt`，并把估算标成「实际」
- `ee/ee/core/prune.py:227`、`breakdown.py:102-104`。
- 建议：另存 `prune_estimate`，展示层再扣。

### 23. `store.append` 静默吞掉写盘失败
- `ee/ee/core/store.py:59-62`（`except OSError: pass`）。
- 影响：磁盘满/杀软占用时对话看起来正常但没落盘，重启即丢。
- 建议：记日志 + 每会话 emit 一次 warn。

### 24. `.eeignore` 只挡 file 工具，shell 完全不管
- `permissions.py:1363-1366`；`:1390` 注释自认。
- 影响：`cat .env`、`rg secret` 免问直接读到被排除内容。
- 建议：对 shell 段参数里的路径跑 `eeignore.ignored`，命中即 ask/deny。

### 25. DENY 用整段原文正则，正常只读工作被硬拒
- `permissions.py:1527-1537`：`grep -rn "shutdown" .`、`echo "format C:"` 都被判危险命令。
- 建议：DENY 只在归一后的命令起点匹配，原文匹配只留给 FORCE_ASK。

### 26. DENY 不检查脚本文件内容
- `permissions.py:564-565`（`pwsh -File` 直接 break）、`:620-651`。
- 影响：`bash x.sh` / `python x.py` 里的危险命令绕过；write 也不扫内容。
- 建议：解析到脚本参数时读文件（限大小）跑一遍 deny。

### 27. `shell_write` 无限信任 stdin
- `permissions.py:1402-1408` 直接 allow；`ee/ee/tools/bgshell.py:108-127` 不检查 input。
- 影响：批准一次解释器后，可输入任意命令，DENY/白名单全失效。
- 建议：对 input 跑一次 `_check_shell`。

### 28. `apply_patch` 不要求先 read，`Delete File` 无审批直接删
- `ee/ee/tools/apply_patch.py:255-292` 对照 `write.py:29-30`、`edit.py:41-42`。
- 建议：删除/更新前要求 was_read，删除额外 ask。

### 29. 原子写会替换符号链接本身
- `ee/ee/core/atomic.py:28-47`（tmp + `os.replace`）。
- 影响：编辑软链接指向的文件会变成普通文件（静默分叉）；硬链接断裂；Windows 丢 ACL。
- 建议：写入前 `path.resolve()` 或识别 symlink 后写真实目标。

### 30. `write`/`apply_patch` 无路径锁且全同步 IO
- `write.py:21-39`、`apply_patch.py:280-292`，对照 `edit.py` 有锁 + `to_thread`。
- 影响：并发写交错；大文件读写阻塞整个后端。
- 建议：复用 `path_lock`，重活进 `to_thread`。

### 31. `grep`/`glob` 没有超时
- `ee/ee/tools/search.py:22-42`（`communicate()` 无 timeout）、`:52-68`（Python 回退线程不可取消）。
- 建议：`wait_for` + kill；回退路径加文件数与时限。

### 32. 沙箱只限写不限读、禁网只是环境变量、非 Windows 直接失效
- `ee/ee/core/sandbox.py:19-21`、`:81-94`、`:215-216`。
- 影响：macOS/Linux 只读模式实际只剩正则白名单；不读环境变量的程序照样联网。
- 建议：至少把「非 Windows 无隔离」在 UI 上明示；长期看需要 landlock/seatbelt。

### 33. 缺「写前版本绑定」
- `edit.py:51-54,68-69` 外部改动只提示仍照写；`apply_patch.py` 无校验；`base.py:44,59-86` 的 `content_identity` 只有 read 用。
- 建议：edit/apply_patch 用 identity 硬校验，不一致要求重读。

### 34. worktree 基线包含用户未提交改动，合并会一起带进来
- `ee/ee/core/worktree.py:474-524`（`_sync_dirty` 把主目录 dirty 内容 commit 成 agent 分支基线）、`:565-619`；`subagent.py:507-509` 建议 `git merge --no-ff`。
- 建议：基线放独立 `refs/ee/baseline/*`，或在合并说明里明确「含你未提交的改动」。

### 35. worktree 建了但启动失败会残留
- `ee/ee/server/orchestrator.py:205-240`、`ee/ee/core/subagent.py:338-344/398-401/476-477`。
- 影响：残留目录+分支；子代理卡片永远转圈（没有 SubagentEnd）。
- 建议：失败分支 `remove(force=True)` 并发结束事件。

### 36. `thread_wait` 不看审批状态，子会话等审批时白等 600s
- `orchestrator.py:311-327` 只判 `k.running`；`threads.py:273-279` 已经算出 `waiting_approval`。
- 建议：返回 `waiting_approval` 列表，让协调者转述用户。

### 37. 协调者没有停止/合并/结构化结果工具
- `ee/ee/tools/orchestrate.py:30-170` 只有 list/status/wait/create/send。
- 影响：「派发→验收→合并」闭环缺一半。
- 建议：补 `thread_stop`、`worktree_diff/merge`、带 `files/verdict` 的结构化回报。

### 38. 子代理报告全量进主上下文
- `ee/ee/core/subagent.py:483` 把 final 原文塞进 `<task_result>`，无截断；对照 `tools/truncate.py:8-12`。
- 建议：同款截断（头尾 + 全文路径），或单独设 `max_report_chars`。

### 39. 大输出预览仍是 24KB 无结构原文
- `ee/ee/tools/truncate.py:39-44`。
- 建议：加统计型摘要（行数/错误行/最后失败信息），预览降到 4-8KB。

### 40. 显式技能调用没有总预算
- `ee/ee/core/skills.py:42`（最多 5 个）、`:386-398`（单个 60k）、`session.py:1004-1022`。
- 影响：一条消息可膨胀 30 万字符，直接 overflow。
- 建议：总字节预算 30k，超出只给路径。

### 41. 项目提示通道的信任门不一致
- `instructions.py:81-104`（AGENTS.md 无条件）、`memory.py:203-208`（项目 `.ee/memory.md` 默认注入）、`skills.py:221-233`，而 SYSTEM.md/hooks/MCP 都要显式信任。
- 建议：至少把项目 memory/skills 纳入同一张信任表。

### 42. MCP 断线重试会重复执行非幂等工具
- `ee/ee/mcp/manager.py:168-197`：`attempt == 0 and client.closed` 时重连后原样再发。
- 建议：只在「请求根本没写出去」时重试；`readOnlyHint=false` 禁止自动重试。

### 43. MCP 的 instructions 被读了就丢
- `ee/ee/mcp/client.py:142` 赋值，全仓无消费者。
- 建议：以固定消息注入（注意前缀冻结，放首条固定 user 消息）。

### 44. MCP 工具清单变化被忽略
- `client.py:87-96` 忽略通知；`session.py:702-704/776-781` 只装一次。
- 建议：listChanged 置脏，回合边界重建并提示「新会话生效」。

### 45. 恢复会话复用旧 tools schema，可能产生幽灵工具
- `session.py:861-884` 只看 sha/compat，`886-906` 只 warn，`1236-1238` 未知工具报错。
- 建议：比对当前 `tool_map`，缺的工具从 schema 剔除或重建前缀。

### 46. `files/*` 没有根目录白名单
- `ee/ee/server/app.py:786-813` 接受任意 cwd；`server/files.py:40-48` 只保证在 cwd 内。
- 影响：token 泄露即可读 `~/.ssh`、`~/.ee/config.toml`。
- 建议：按已知 workspace 列表校验 cwd。

### 47. WebSocket 关掉心跳，半死连接发现不了
- `app.py:1047-1048` `ping_interval=None`；前端无看门狗。
- 建议：恢复 ping 20/20，或前端 N 秒无消息主动探活。

### 48. UI 断连会重启整个内核并打断回合
- `desktop/src/App.tsx:101-103`、`desktop/src/lib/rpc.ts:9`。
- 建议：failed 先探活，确认后端死了才重启。

### 49. 打开正在流式的会话，文本会回退或重复
- `desktop/src/lib/store.ts:303-327`（快照整体替换）对照 `:211-233`（重连有合并逻辑）。
- 建议：复用合并逻辑，调用前 flush delta 缓冲。

### 50. 长会话无虚拟化
- `Conversation.tsx:242-266` 全量渲染；`Markdown.tsx:77-83` 每条同步解析。
- 建议：只渲染最近 N 条 + `content-visibility: auto`。

### 51. 子代理并发上限是「单步」的，不是全局写者上限
- `session.py:1304-1305` 每步新建 Semaphore；`subagent.py:326`；与 orchestrator 的 max_children 互不相干。
- 建议：提升到进程级共享计数器。

### 52. 长阻塞的只读工具占 READ 名额，堵住同一步的写调用
- `ee/ee/core/parallel.py:3-8` + `tools/orchestrate.py:70-72`（wait 最多 3600s）+ `bgshell.py`。
- 建议：工具加 `blocking` 标记，不占 READ 名额。

---

## P3 — backlog

53. `handoff.py:33/157-167` sha256 只算前 64MB，两个不同大文件可能判一致 → 加 size 比对或标记 partial。
54. `handoff.py:80-86` 裸 `subprocess.run(["git"])`，无 timeout、无 stdin=DEVNULL，GUI 线程里跑 → 改用 `ee.proc.run_capture`。
55. `bgproc.py:259-274` 先置 killed 再杀，kill 失败也不重试 → 杀完校验 `poll()`。
56. `session.py:1298` 并发类型判定在 hook 改参数之前 → hooks 先跑再算 access。
57. `permissions.py:1489` auto 模式不看 `.git`，可直接写 `.git/hooks` → 保留 force ask。
58. `permissions.py:1483` vs `:1487`：deny 按 tool+write 匹配，ask 只按 tool → 统一。
59. `shellenv.py:51-56` PowerShell 脚本落在共享 TEMP → 改私有目录。
60. `autoapprove.py:129-132` 生成的规则与真正落盘的 `rule_from_decision` 不一致 → 改用后者。
61. `permissions.py:1301-1312` 永久允许一个解释器 = 永久放开 `node *`/`bash *` → 解释器类禁止 forever。
62. `read.py:8-9/137-146` 首尾截断吃掉中间；`~/.ee/tool-output` 无清理、明文落盘 → 自适应分段 + 保留期。
63. `session.py:1109-1115` LAST_STEP_NOTE 可能配 budget/异常路径留下孤立指令 → 检查提前。
64. `session.py:471-482/493-504` 回退不重置 `last_prompt/_fresh_usage`，误触发压缩 → 清零。
65. `session.py:903` 每步重算完整前缀（同步读提示词文件）→ 缓存 sha，变化才失效。
66. `session.py:1350` 工具截断在事件循环里同步写盘 → `to_thread`。
67. `session.py:1033-1035` 等锁期间 `interrupt()` 返回 False → 提前置 pending 标记。
68. `context.py:185-205` 的 `turn` 字段无消费者，「未命中归因」没实现 → 补或删。
69. `subagent.py:478` `max_steps` 被当成功；`:226-228` 丢弃计数只记 1；`:329-344` 异常不发 SubagentEnd → 修正。
70. `worktree.py:60-72` index.json 跨进程无锁 → 文件锁。
71. `snapshot.py:25/226-252` 大文件排除被根 mtime 缓存绕过 → 缓存 key 加 `git ls-files --others` 探测。
72. `checkpoints.py:65-81` gc 每次 load 所有会话 → 流式只读 meta。
73. `orchestrator.py:43-48/106/307` 协调者只拿到 300 字符末条消息 → 放宽到 4-8KB。
74. `tools/todo.py:50-52` 多 in_progress 直接报错 → 自动降级保留第一项。
75. `Items.tsx:132-138` `Approved` 不认 `forever`；审批无键盘快捷键 → 补。
76. `app.py:934` 每连接无界队列；`:974` 无并发上限 → 设上限。
77. `headless.py:454-477` 异常路径缺 turn_done；`:540-551` 可能发两条 → 统一终态。
78. `research/README` 基线数字四个版本（1578/1480/1822/1846）→ 只留一处。
79. `desktop/src/lib/schedule.ts:116-129` 与后端 `schedule.py:218-224` 对「周末」描述不一致 → 统一。

---

## 缓存机制专项（按收益排序）

### C1. tool 消息缺 id 时每次生成随机 UUID，整段缓存失效
`ee/ee/core/providers/transforms.py:133-135`。历史里只要有一条缺 `tool_call_id` 的 tool 消息（迁移/改写过的旧会话），每一步请求字节都不同。改：确定性 id（`call_fixup_<下标>`）或在 `_append` 时回填并持久化。

### C2. 工具循环中间触发压缩，整段历史重建
`ee/ee/core/session.py:1108` 在 `step > 1` 每一步都调 `_maybe_auto_compact`，压缩后 `self.history = new`，从第一条起全部 miss。改：循环内只在接近硬顶（如 0.92×window）才压，0.8 阈值留到回合结束/下条用户消息前。

### C3. 压缩不保留首条用户原话
`ee/ee/core/compaction.py:145-153` 只保留 agents + 最近 N 条 user + 摘要。改：保留首条真实 user（去重），顺序 agents → first_user → recent_user → summary。

### C4. 换模型时 tools 重建但 prefix 保留，可能发过期工具
`session.py:213-226`：`same_prefix` 只比 `prompt_patch`/`edit_style`。改：比对新旧 tools 的 sha256，不一致时警告「新工具本会话不生效」。

### C5. miss 没有归因
`context.py:188` 注释承诺了归因，实际只记 turn。改：`UsageStats.add` 加 `reason`（first_turn/after_compact/after_prune/model_switch/normal），`breakdown` 按 reason 分组。

### C6. 延迟压缩没接上
`context_policy.py` 是影子模式，`observe()` 只记录。改：`hit_rate ≥ 0.9 && pressure < 0.85` 时把压缩推迟到用户边界。

### C7. 恢复会话不提示提示词文件已变更
`session.py:861-884` 复用旧前缀（正确），但只有首建时 emit notices。改：恢复路径也检查文件指纹，提示「新对话生效」。

### C8. 修剪把服务端 `last_prompt` 改成估算值
`prune.py:227`，影响 `context_left()`/`needs_compaction()`/界面「实际占用」。改：另存 `prune_estimate`。

---

## 与 codex / 天枢对照的结构性缺口

| 能力 | codex | 天枢 | ee 现状 |
|---|---|---|---|
| OS 级沙箱 | seatbelt/landlock/bwrap + Windows 受限令牌 | 审批闸 + 路径校验（无 OS 沙箱） | 仅 Windows 写限制，非 Windows 失效 |
| 指令级权限判定 | Starlark execpolicy（可前缀匹配、可扩展） | destructive gate + 版本绑定 | 正则 + 自写命令行拆分器，有误报/漏报 |
| 写前版本绑定 | 无 | `content_identity` 硬校验 | 有 identity 但只有 read 用 |
| 流式截断检测 | 要求 `response.completed` | `IncompleteStreamError` | `finish_reason` 不读 |
| 压缩时机 | 接受压缩后重建 | 只在用户边界重写 | 工具循环内也压 |
| 缓存归因 | 只有 cached_tokens | 完整账本 + miss 分类 | 有命中率，无归因 |
| 多 agent 结果流转 | InterAgentCommunication + mailbox | 结构化 work order + 聚合策略 | 自由文本 `task_result`，无停止/合并 |
| 会话锁 | 单进程内 | sidecar 单进程 | 桌面 + `ee exec` 可并发写同一会话 |

---

## 建议修复顺序

1. P0-1（孤儿 tool_calls）→ 2. P0-2/P0-3（权限绕过）→ 3. P1-4/5/7（并发与锁）→ 4. C1/C2（缓存两条）→ 5. P1-9/10（事件循环与重启）→ 6. 其余 P2。

每一项改完至少补一条回归用例；P0-1、P0-2、P1-10 建议先写最小复现再动手。


---

# 复核修正（2026-10-31，主代理实测复核）

按条目逐条复现后，下面 7 处结论需要更正。**原文不删，改在这里**——保留错误记录，
才知道哪些是推断、哪些是实测。基线仍是 `1ba804b`；修复已提交到 `b8d7755`。

## 修正 1：P0-3b 的影响面说大了

原文：「所有 `task` 子代理的 `shell` 工具报错，用户表现为"子代理坏了"。」

实测（`ee/ee/core/subagent.py:137-147` 的 `is_read_only`）：

- 默认 `agent=general` **不是**只读 → 不受影响
- 真正命中的是：`agent=explore`、显式 `mode=read-only`、**主会话处于 read-only/plan 模式**
- 后者最要紧：「plan 模式下主会话自己的 shell 也会挂」，不只是子代理

## 修正 2：P0-3b 的根因不是「权限不足」

原文：「MSYS2 创建共享内存映射失败：`CreateFileMapping ... Win32 error 5`」，暗示是受限令牌没给写权限。

沙箱内实测（ctypes，受限令牌下）：

| 操作 | 结果 |
|---|---|
| `CreateFileMappingW` 同名对象 PAGE_READWRITE | handle=616, **err=0** |
| `OpenFileMappingW(FILE_MAP_WRITE)` | handle=368, **err=0** |
| `OpenFileMappingW(FILE_MAP_READ)` | handle=348, **err=0** |
| `cmd.exe /c echo hi` | **exit=0 输出 hi** |
| `powershell.exe -Command echo hi` | **exit=0 输出 hi** |
| `bash.exe -c echo hi` | exit=256，Win32 error 5 |

结论：**不是权限问题，也不是命名对象问题**。只有 cygwin/MSYS2 这一族失败——
cygwin1.dll 启动时要给自己的共享内存对象设 DACL，而它不知道自己在受限令牌下、
无法把 ee 的能力 SID 写进去，于是它自己写的 DACL 不满足 `WRITE_RESTRICTED` 检查。

顺带排除两个假设：TEMP 改成真实目录照样失败；沙箱内外 `OpenFileMapping` 都返回
err=2（对象本来就不存在）。

## 修正 3：`probe_sandbox.py` 按原文命令跑不起来

原文给的运行方式：

```
cd E:\ee建设\ee-main && ./.venv/Scripts/python.exe D:\ee日常\probe_sandbox.py
```

实测直接 `ImportError: cannot import name '__version__' from 'ee' (unknown location)`，
**到不了沙箱那一步**。仓库根下的 `ee/` 没有 `__init__.py`，Python 把它当 namespace
package（`ee.__path__=['E:\ee建设\ee-main\ee']`），取不到真包里的 `__version__`。

必须：`cd E:\ee建设\ee-main\ee && ../.venv/Scripts/python.exe D:\ee日常\probe_sandbox.py`
这样才复现出报告里那句 `CreateFileMapping ... Win32 error 5`。

「让对方先跑 probe_sandbox.py 确认环境一致」这句因此失效——对方会卡在 ImportError
上得出"报告不准"的结论。

## 修正 4：P2-38「子代理报告全量进主上下文」不成立

原文：「`ee/ee/core/subagent.py:483` 把 final 原文塞进 `<task_result>`，无截断。」

`subagent.py:483` 确实不截断，但**外层统一截断**：`ee/ee/core/session.py:1356`
对所有工具结果（含 `task` 的 `<task_result>`）调用 `truncate_output_info`。实测：

```
原长度: 80006 -> 截断后: 771
```

该条应撤销。

## 修正 5：P1-15 的根因有一半不成立

原文：「`_communicate_capped`（:391-408）的 `ConnectionResetError` 会冒泡。」

`ee/ee/core/hooks.py:396` 已经 `except (BrokenPipeError, ConnectionResetError): pass`。
真问题只有一处：`fire()`（:657）的 `asyncio.gather` 没有 `return_exceptions=True`。

## 修正 6：P0-1 里「MemoryError 漏掉」的举例是错的

原文：「`session.py:1154-1162` 只覆盖 CancelledError/Exception，MemoryError 等路径漏掉。」

`MemoryError` 是 `Exception` 的子类（实测 `issubclass(MemoryError, Exception)` → True），
**已被覆盖**。真正漏的是 `KeyboardInterrupt` / `SystemExit`（`BaseException` 的子类，
不是 `Exception`）。结论（会留孤儿）成立，但举例要换。

## 修正 7：P0-3b 还有第二层缺陷原文没提到

沙箱 helper 用 `python -m ee` 启动（`sandbox.py:192` `helper_argv`）。`-m` 会把 cwd
放进 `sys.path[0]`，而 helper 是在**用户工作区**里跑的——工作区只要有名为 `ee/` 的目录
（本仓库正是如此），helper 自己 import 就失败：

```
ImportError: cannot import name '__version__' from 'ee' (unknown location)
```

这层失败把真正的沙箱错误完全盖住，用户只看到"沙箱起不来"。

---

# 已修复清单（截至 `b8d7755`）

| 提交 | 对应条目 | 修复内容 | 验证 |
|---|---|---|---|
| `5933f75` | 15b | 测试计价与真实时钟脱钩（默认折扣钉 1.0） | 7 failed → 54 passed |
| `5933f75` | P0-2/P0-3 | 短选项簇扫描：`-cX`≡`-c X`、`-oX`≡`-o X` | plan 4 条 deny、default 4 条 ask |
| `5933f75` | P0-1 | `load_messages` 补孤儿 tool 消息 + `_turn` 加 `finally` | 补齐 False → True |
| `5933f75` | P0-3b（第 1 层） | helper 加 `-P` + `helper_env`，不被工作区同名目录劫持 | RED→GREEN |
| `fedf2e4` | 效率 | `explore` 移除 `shell`，不再进沙箱 | 37 passed |
| `cce51a4` | P1-15 | `gather` 加 `return_exceptions=True` | RED→GREEN |
| `517e90e` | P1-13 | 无 `finish_reason` 抛 `ModelError(kind="truncated")` | RED→GREEN |
| `5939bbd` | P1-14 | 流中途失败保住已显示内容并入账 | RED→GREEN |
| `9c3f36b` | P1-9 | `thread/children`/`status` 改 async + `to_thread` | **未取得红灯**，见下 |
| `2df6f9e` | P1-5 | 审批等待 300s 超时，超时按拒绝 | 回滚后 25s 被掐死 |
| `b8d7755` | P1-6 | 无结束快照时退到 before 做基线，仍弹确认 | RED→GREEN |

## 未取得红灯的一项（如实披露）

P1-9（`9c3f36b`）：想用心跳 ticker 测「0.5s 内事件循环跳了几次」，构造不出稳定红灯：
调度 `thread/children` 时没有子会话，`gitinfo.status` 根本不被调用，实测耗时仅 1.9ms；
ticker 的 `create_task` 与紧跟的 `await` 落在同一事件循环轮次，测到的是启动延迟。
该测试已删除（不留空转的假绿），改动按静态路径核对：两处都在 `to_thread` 内执行。

## 尚未处理

- **P0-3b 第 2 层（cygwin 共享内存）**：修法只有两条，都有实质代价——给令牌放开命名对象
  写权限（削弱隔离），或对 Git Bash 退化成非受限令牌 + ACL（只读模式从「内核强制」降级为
  「静态分析」）。需要产品决策，未动。
- **P1-4**（回合中改 model/mode）：涉及队列语义，未做。
- **P1-7**（快照恢复与并行写无互斥）：需进程级写锁，横跨三处，未做。
- **P1-8**（子会话审批父界面不可见）：前端改动，无法验证渲染，按项目约定不做。
- **P1-11/12**（LSP/MCP 泄漏）：需真跑 LSP/MCP 才能验证，成本高，未做。
- **P2/P3 其余条目**：多为静态阅读推断，未经复现，不建议直接动手。

## 一条方法论提醒

本报告口径是「7 路并行静态阅读 + 关键结论回查行号」，这个组合对**定位**有效，
但对**因果判断**不足。上面 7 处修正里有 5 处是"看到现象就推断根因"导致的：

- 看到 `subagent.py:483` 不截断 → 断定报告全量进上下文（实际外层有统一截断）
- 看到 `CreateFileMapping` 失败 → 断定权限不足（实际只有 cygwin 失败）
- 看到 `_fill_missing` 覆盖两种异常 → 举 `MemoryError` 为例（实际它已被覆盖）
- 看到 helper 报 ImportError → 以为沙箱坏了（实际是两层缺陷叠加）
- 看到 `except Exception` 之外 → 以为漏了 MemoryError（实际漏的是 BaseException 分支）

动手前跑一次最小复现，成本通常低于事后修正。


---

# 独立审查后的补充（2026-10-31 晚）

对 `5933f75..0cb6135` 跑了 `review_changes` 独立只读审查（单个 reviewer，13 项发现）。
下面记录核实结果与处置——**审查的发现同样经过实测，不是照单全收**。

## 审查发现的处理结果

### 已修（6 条）

| 发现 | 级别 | 实测确认 | 修复提交 |
|---|---|---|---|
| `_fill_orphan_tool_calls` 只挂在 `load_messages`，生产路径 `SessionStore.load()` 没挂 | HIGH | ✅ `load()` 返回 `['user','assistant']` | `379ebde` |
| `_pending_model/_pending_mode` 在撤回/退回时残留 | HIGH | ✅ 撤回后 `flash → pro` | `0cb6135` |
| `helper_env` 的 import 根算错一层（`parents[1]` 是包目录） | HIGH | ✅ 改为 `parents[2]` 后能解析真包 | `0cb6135` |
| `truncated` 一次都不会重试（`isinstance(ModelError)` 覆盖成 other） | MEDIUM | ✅ 代码路径核对 | `8476148` |
| 不发 `finish_reason` 的端点被全量误报为截断 | MEDIUM | ✅ RED：兼容端点被判截断 | `8476148` |
| `_shell_path_args` 先 `replace("="," ")` 导致长选项值被误当路径 | LOW | ✅ `--exclude-dir=.env` 被误拦 | `505fb1c` |

### 实测后保留现状（2 条，已加测试锁定）

| 发现 | 判断依据 |
|---|---|
| `_conflicts` 在无结束快照时恒定要求确认 | 实测属实（不手改也 `needs_confirm=True`）。但快照层分不开「ee 写的」和「用户手改的」；多问一次 vs 静默覆盖用户改动，选前者。已在注释写明，并用 `test_undo_without_end_snapshot_always_confirms` 锁定 |
| `git -p` 分页器短选项不在黑名单 | 实测 `GIT_PAGER=more` 也不会启分页器（`child_env` 钉成 cat）。不可利用。用 `test_git_pager_is_neutralized_by_child_env` 锁定前提 |

### 未处理（2 条，需更大改动或产品决策）

| 发现 | 原因 |
|---|---|
| 同 cwd 互斥是 TOCTOU，非真锁 | 做真锁要跨 `app.py`/`checkpoint_rpc.py`/`snapshot.py` 引入进程级写锁，风险面大；当前是尽力而为 |
| `_eeignored` 对所有 shell 命令都 `for_dir()` 读盘 | **尝试过加进程级缓存，实测引入安全回归**（改 `.eeignore` 后规则不生效，`cat secret.txt` 变 allow），已完全回滚。性能优化不能拿正确性换 |

## 审查自身的价值

这次审查最有价值的一条是 **`_fill_orphan_tool_calls` 挂错路径**——我改了函数、写了 RED、测试全绿，但生产路径根本没走它。这是「修了但没修到点上」，比修不出来更危险，因为绿灯会让人以为问题解决了。

三条 HIGH 有共同特征：**验证的是测试路径，不是生产路径**。教训是：改任何函数后，先 grep 它的真实调用方，再决定测试怎么写。

---

# 全部变更一览（`1ba804b..732d9e7`，19 个提交）

| 提交 | 内容 |
|---|---|
| `5933f75` | P0 三项（孤儿 tool_calls / 短选项绕过 / 沙箱 helper）+ 计价假红 |
| `fedf2e4` | `explore` 移除 shell |
| `cce51a4` | hook 抛异常不再打断回合 |
| `517e90e` | 流无 `finish_reason` 报错 |
| `5939bbd` | 流中途失败保住已显示内容并入账 |
| `9c3f36b` | `thread/children`/`status` 移出事件循环（**未取得红灯**） |
| `2df6f9e` | 审批等待 300s 超时 |
| `b8d7755` | undo 无结束快照时检测手改 |
| `8764e42` | 排队消息不中途换模型 |
| `fe70605` | 同 cwd 有会话在跑时拒绝 undo/restore |
| `6a0119e` | 会话关闭释放 LSP |
| `9e0a967` | MCP 失败后重试 |
| `f598e03` | 写盘失败不再静默 |
| `d66f1e0` | `.eeignore` 也挡 shell |
| `379ebde` | 孤儿补全挂到生产路径（审查） |
| `0cb6135` | 排队 model 残留 + helper import 根（审查） |
| `8476148` | 截断判定 + truncated 可重试（审查） |
| `505fb1c` | `.eeignore` 不误拦长选项值（审查） |
| `732d9e7` | 锁定两个有意接受的权衡 |

## 验证状态

- 全量 pytest：`1931 passed, 1 failed, 16 skipped`（1058s）。那 1 条是 `test_http_status_error_message`，本机假服务器在并发下的 `WinError 10053`，单独跑该文件 `9 passed`，与改动无关。
- 19 个提交中，**18 个有真红灯**；`9c3f36b` 未能构造红灯，已在该 commit message 里如实标注。
- 前端消费方已核对：`thread/undo` 与 `checkpoint/restore` 的新增 `RpcError` 会被 `store.ts:602` 和 `Timeline.tsx:86` 的 catch 显示成 toast，无需改前端。

## 仍存在的已知问题（不建议直接动手）

1. **`explore` 之外，plan 模式下主会话自己的 shell 也会因 Git Bash + 受限令牌失败**（见前文 3b 第 2 层）。修法只有「削弱隔离」或「退化到 powershell」，需产品决策。
2. **同 cwd 互斥是尽力而为**，不是真锁。
3. **P2/P3 里未经复现的条目**：本报告的错误率测量结果是 7 处推断错误（其中 5 处源于「看现象就推根因」）。**动手前请先跑最小复现**。
