# ee 当前路线与实施状态

更新：2026-11-02（切会话卡顿：大会话分页 + 区块窗口化 + 窄订阅；补 e2e 隔离、CI 接入 e2e 与 lint、发布链路首跑；缓存命中率回归排查与零断点清推理；上下文成本：关掉消息级修剪、历史大字段占位、工具输出阈值 16KB。见「执行记录（2026-11-02）」）。产品定位：Windows 桌面 AI 编程 Agent；Python 内核 + Electron/React，只支持 OpenAI Chat Completions 协议，不恢复 TUI。

## 当前执行范围

本批已执行稳定化 Wave 0–2（T1–T6），已完成 D6-1 指挥会话的全部三个 Wave（U1–U9），并已完成 D6-2（LSP 诊断）的 U1–U6。完整方案见 plans/2026-10-28-stabilization-and-d6.md、plans/2026-10-28-d6-1-orchestrator-tasks.md、plans/2026-10-29-d6-2-lsp-tasks.md；本页是当前入口与验证记录。

| 阶段 | 工作 | 当前状态 | 生产入口 / 验证入口 |
|---|---|---|---|
| Wave 0 / T1 | 现行路线与历史设计分离 | 已完成（纯文档） | 本页、README、HANDOFF |
| Wave 0 / T2 | PR/main 验证与显式发布分离，补 Vitest | 已完成（恢复 `push`/`PR` 自动触发；`test_ci_offline_gate.py` 守着，远端已多次跑绿） | `.github\workflows\desktop-windows.yml`；`desktop\package.json` |
| Wave 1 / T3 | worktree 查询不改索引、读取失败不删成果、备份不完整不删、报告一致 | 已完成并验证 | `ee\ee\core\worktree.py`、`core\subagent.py`、`server\automations.py`；`ee\tests\test_worktree_contract.py` |
| Wave 1 / T4 | MCP 连接绑定工作区/配置、用户侧信任、总截止时间 | 已完成并验证 | `ee\ee\mcp\`、`server\mcp_rpc.py`；`ee\tests\test_mcp_isolation.py` |
| Wave 1 / T5 | 项目只能收紧预算；隔离失败不回退写主目录 | 已完成并验证 | `ee\ee\config.py`、`server\automations.py`；`ee\tests\test_budget_and_isolation.py` |
| Wave 2 / T6 | 会话加载单飞、删除不复活、设置重载释放客户端、前端清理幽灵数据 | 已完成并验证 | `ee\ee\server\app.py`、`threads.py`；`desktop\src\lib\store.ts`；`ee\tests\test_session_lifecycle.py`、`desktop\src\lib\store.test.ts` |
| D6-1 Wave A / U1–U4 | 关系模型（`orchestrator_parent_id` 与 fork 的 `parent_id` 分离）、run 终态、名额控制器、能力继承 | 已完成并验证 | `ee\ee\core\orchestrator_cap.py`、`orchestrator_slots.py`、`server\threads.py`；`ee\tests\test_orchestrator_model.py`、`test_orchestrator_slots.py`、`test_orchestrator_cap.py` |
| D6-1 Wave B / U5 | 窄接口 `OrchestratorAPI` 与三个读工具（list/status/wait） | 已完成并验证 | `ee\ee\core\orchestrator.py`、`server\orchestrator.py`、`tools\orchestrate.py`；`ee\tests\test_orchestrator_tools.py` |
| D6-1 Wave B / U6 | 写工具（create/send）与审批冒泡 | 已完成并验证 | 同上 + `server\threads.py` 的 `_bubble_to_parent`；`ee\tests\test_orchestrator_flow.py` |
| D6-1 Wave B / U7 | 重启把遗留 running 标为 interrupted、父删不级联删 worktree、重启不重放回合 | 已完成并验证 | `ee\ee\core\store.py` 的 `_close_stale_runs`、`server\app.py` 的 `_detach_children`；`ee\tests\test_orchestrator_flow.py` |
| D6-1 Wave C / U8 | 桌面端左栏树形与子会话卡片、审批冒泡落盘 | 已完成并验证 | `desktop\src\components\ChildThreadCard.tsx`、`Sidebar.tsx`、`lib\store.ts`；`ChildThreadCard.test.tsx`、`SidebarChildIndent.test.tsx`、`lib\childApproval.test.ts` |
| D6-1 Wave C / U9 | 假模型端到端脚本 + 编排闭环 | 已完成并验证 | `ee\scripts\e2e_ws.py`；`ee\tests\test_e2e_wait.py`；`server\app.py` 的 `thread/create_child\|children\|status\|wait` |
| D6-2 / T9 | 版本化 LSP 诊断、可信启动与进程回收 | **已完成并验证（U1–U6 + 生产接线 + 真实服务器）** | `ee/core/lsp/{client,servers,manager,diagnostics,notify}.py`、`ee/tools/lsp.py`、`ee/core/session.py` 的 `_attach_lsp`、`ee/tests/test_lsp.py`（30 例）+ `test_lsp_integration.py`（5 例，含真实 pyright）；设置页开关。只支持 python / typescript，不自动安装服务器 |
| T10-a 会话导出 | Markdown / JSON 导出（核心 + RPC + CLI + 桌面端入口） | **已完成并验证**（全链路；Electron 保存对话框未验） | `ee\ee\core\export.py`、`server\app.py` 的 `thread/export`、`cli.py` 的 `export` 子命令、`desktop\src\components\ExportDialog.tsx`（会话行菜单「导出…」）；`ee\tests\test_export.py`（11 例）、`test_export_rpc.py`（10 例）、`test_export_cli.py`（8 例）、`desktop\src\components\ExportDialog.test.tsx`（4 例） |
| T10-b / Goal 循环 | 条件驱动 + 多轮续跑 + 判据判定（零测试/超时不算成功） | **已完成并验证**（U1–U3 + 会话内 `turn/start` + 桌面端 `+` 菜单入口） | `ee\ee\core\goal.py`、`cli.py`+`headless.py` 的 `--goal`、`server\threads.py` 的 `start_turn(goal=)`／`_consume_goal`、`desktop\src\components\GoalMenu.tsx`；`test_goal.py`（12）、`test_goal_runner.py`（9）、`test_goal_cli.py`（6）、`test_goal_exec.py`（5）、`test_goal_acceptance.py`（2，真实 pytest 闭环）、`test_goal_turn.py`（12）、`GoalMenu.test.tsx`（7） |
| T10-c / 自动审批 | 建议态：只出建议、不碰决策 | **已完成并验证**（建议态 + 落库 + 审批卡片显示）；自动放行版本未做，需另行安全审查 | `ee\ee\core\autoapprove.py`、`events.py` 的 `ApprovalRequest.suggestion`、`session.py` 的 `_attach_suggestion`、`server\threads.py` 的 `_on_event` 落 `ApprovalItem`、`desktop\src\components\Items.tsx` 的建议区；`test_autoapprove.py`（10）、`test_autoapprove_wiring.py`（5）、`test_autoapprove_persist.py`（3）、`ApprovalSuggestion.test.tsx`（4） |
| T10-d / best-of-n | 候选评选，胜者只由判据决定 | **已完成并验证**（评选层 + RPC + 判据编排 + 桌面端） | `ee\ee\core\bestof.py`（复用 `goal.Verdict`）、`server\bestof_rpc.py` 的 `bestof/pick`、`server\bestof_verify.py` 的 `bestof/verify`（每候选单轮判据）、`desktop\src\components\BestofDialog.tsx`；`test_bestof.py`（14）、`test_bestof_rpc.py`（4）、`test_bestof_verify.py`（5）、`BestofDialog.test.tsx`（7） |
| T10-e / 文件交接 | 迁移清单 + 逐字节核对 | **已完成并验证**（清单层 + RPC + 桌面端入口）；复制/切换/清理需用户确认，未做 | `ee\ee\core\handoff.py`、`server\handoff_rpc.py` 的 `handoff/manifest\|verify`、`desktop\src\components\HandoffDialog.tsx`（工作区菜单「交接清单…」）；`test_handoff.py`（12）、`test_handoff_rpc.py`（8）、`HandoffDialog.test.tsx`（5） |
| 后置 | 发布 | **tag 发布链路已落地（2026-11-02）**：`release.yml` 推 `v*` tag 即校验版本号→打包→SHA256→建 Release；代码签名明确**不做**（理由见下），更新器接口已配好但客户端未接 electron-updater | 首次真实发版与自动更新需单独确认 |

## 执行记录（2026-11-02，改 .py 后删陈旧字节码）

CI 上 `test_goal_acceptance::test_goal_closes_loop_red_then_green` 偶发失败，日志是
`FAILED test_calc.py::test_add - assert -1 == 5` + `FakeClient 脚本用完了`，表面看像“模型没改对代码”，
实际是 **CPython 字节码缓存失效判据太弱**：判断 `.pyc` 是否过期只看「源码 mtime 的**整秒**」
和「**文件大小**」。`edit` 经常写出**等长**改动（`return a - b` → `return a + b`，34 字节不变），
若它和上一次 import 落在同一秒内，两项都不变，Python 就把旧字节码当有效——紧接着跑的测试
仍读到旧值。于是 agent 明明改对了、判据持续红、goal 循环跑满 3 轮把 FakeClient 的脚本耗光，
退出码 3。同一份代码上一轮是绿的，属于时序敏感的偶发（本地连跑 10 次全过，所以长期没被发现）。

这是**对所有用户都成立**的缺陷：用户让 ee 改任何 Python 文件都可能踩到，不只是 CI 测试。

修法：`fileops.write_text`（`edit`/`write`/`apply_patch` 三个工具共用的唯一写入点）写完后
删掉对应的 `.pyc`（含 `__pycache__` 里同 stem 的各版本）。不伪造 mtime、不指望“下次恰好
落在下一秒”，是唯一与时序无关的手段；删不掉（被杀软/索引占着）也不连累写入本身。

验证：探针把 mtime 强行拨回原值、大小不变（最坏情况），修复前 pytest 持续报旧结果，修复后
`1 passed`；端到端 goal 闭环 5/5 通过；新增 4 条测试（将 `_drop_bytecode` 打成空操作确认它们是红的）；
相关子集 184 passed。

## 执行记录（2026-11-02，切会话卡顿：全量渲染 + 全量订阅）

用户报「另一个会话跑了很久，切过去界面掉帧」。实测根因是两条独立的放大路径，**都跟“会话在跑”本身关系不大**：

**① 大会话一次全量搬进渲染进程 + 全量挂 DOM。** 真实样本 `20261101-201905-43ca2f.jsonl` 15MB / 5215 条（payload 7.2MB），
`thread/get` 一次全返回，`Conversation` 又用 `blocks.map` 把 1120 块全量 `ReactMarkdown` + Shiki 高亮。
结果是打开一次 4.4 秒、单个长任务 3.5 秒、打开后帧间隔 48.5/62.5ms（约 1fps 的观感）、DOM 节点 51559 个。

**② 全仓 30 处 `const s = useApp()` 全量订阅。** 后台会话每帧 delta flush 都会 `useApp.setState` 重建 `details`，
Composer、每个 `ThreadRow`、`Items`、`RightPanel`、`Timeline`、`FilesPanel` 等全部重渲——**跟当前看的是哪个会话无关**，
所以「后台在跑」会让前台一切操作都变卡。已收窄到只剩 11 处，且都在按需挂载的对话框/设置页里（`useAppFields` + 选择器）。
另外 `App.tsx` 托盘订阅每次 store 变化都两遍 `threads.filter`（上限 300 条），已加引用比较早退。

修法（四层）：

| 层 | 改动 |
|---|---|
| 传输 | `thread/get` 新增 `tail` / `before_seq` 分页（`threads.py:detail`、`app.py:thread_get`），不传参时行为不变，脚本/`ee exec`/导出不受影响 |
| 首屏 | `store.openThread` 只拉尾部 120 条（`FIRST_PAGE`）；重连快照按本地已有条数要 `max(FIRST_PAGE, localCount+40)`，避免把 7MB 再拉一遍 |
| 渲染 | `Conversation` 窗口化：默认只渲染尾部 60 块（`PAGE`），锚点用**块 id** 而非下标（delta 只往尾部长）；块级 `memo` + `sameItems` |
| 往上翻 | 本地窗口没展开完就纯前端展开；本地到底但 `items_has_more` 才 `loadEarlier` 补一页 120 条，按 id 去重前置 |

滚动补偿踩了两个坑，最后**不自己算**：`scrollHeight` 差量补偿与「DOM 锚点 + `overflow-anchor: none`」都会把视口甩到 `top: 0`，
因为新插入块的 Markdown / Shiki 高亮是异步完成的，量高度那一刻还没长完。改成「点按钮先 `scrollTop = 0` 归一化」+ 浏览器原生
scroll anchoring，点按钮与滚到顶两条路径行为一致，问题消失。

实测（合成样本 80 回合 = 1120 条 / 真实 15MB 会话）：

| 指标 | 优化前 | 优化后 |
|---|---|---|
| 冷启打开大会话 | 4390 ms | **507 ms** |
| 单个最长任务 | 3511 ms | **331 ms** |
| 打开后帧间隔 avg/p95 | 48.5 / 62.5 ms | **6.9 / 7.2 ms** |
| 后台流式时切大会话 | 4239 ms | **360–425 ms** |
| 后台流式时大会话帧间隔 | 51.5 / 83.4 ms | **7.1–7.7 / 7.1–7.3 ms** |
| 活动会话流式帧间隔 | 60.5 / 76.3 ms | **6.9–7.0 / 7.1–7.2 ms** |
| DOM 节点数 | 51559 | **2822** |
| 真实 15MB 会话打开 | 3013 ms | **1120 ms** |

验证：`vitest` 297 passed（58 文件，新增 3 条分页用例）；`playwright test` 全量 27 passed / 1 skipped（58.2s，`conversation-scroll.spec.ts` 4 条全绿）；
后端 `test_server.py` 14 passed（含新增的分页往返用例：`tail` 逐页往前翻能逐条拼回完整历史、不传参行为不变、`tail=0` 不退化成全量）、
`test_details.py` + `test_branch.py` 11 passed、`test_queue.py`/`test_plan.py`/`test_redo_data.py` 47 passed；
`ruff check ee/` 通过；`npm run typecheck` 通过。真实大会话探针实跑：打开时 61 块、在底部（`top: 1911`、`atBottom: true`、按钮「显示更早的 60 条（还有 60 条）」），
滚到顶自动补拉到 121 块（`top: 0`、按钮变「加载更早的历史」，`data-block` 锚点 120 个），滚动位置没有被甩飞，帧间隔 6.9/7.2ms。

保留边界：`Conversation` 的 `PAGE=60` 与 `store` 的 `FIRST_PAGE=120` 目前各自定义、靠注释对齐（一个是块、一个是条目，量纲本就不同）；
剩下 11 处 `useApp()` 都在按需挂载的对话框里，收益极低，未动。

## 执行记录（2026-11-02，上下文成本：关掉消息级修剪 + 历史大字段占位）

长会话越来越慢的根因不是「历史长」，而是**历史里永久重发的冗余**。按 DeepSeek 官方价
（未命中 2.0 / 命中 0.04 / 输出 8.0 元每百万）算，命中部分只占 1/50 价，所以「变长」本身几乎不涨钱，
真正的成本只有两处：**新写进来的内容（首次未命中）** 与 **打断缓存的事件**。据此推翻了三个旧结论：

**① 140k 断崖不存在。** 排除网络异常时段（慢请求占比 >20% 的 5 个小时）后，干净样本 3,930/4,600 条，
曲线平滑：首包 = **1317 + 0.0072 × prompt**（ms），分桶 20-30k→1085ms、100-110k→1984ms、
130-140k→2421ms、170-180k→2328ms、230-240k→3078ms。慢是真实现象（同时段大/小 prompt 中位倍数 1.76x，
10 个时段全部同向），但不随 prompt 断崖。

**② prune 回本要几十次请求，实际是纯亏损。** 修剪改的是**历史中段**，其后所有内容都要重算，
所以断点成本≈整个 prompt，而它只省下修剪量本身。实测三次：修剪 49,180 tok / prompt 50,965 → ¥0.0979；
修剪 23,906 / prompt 76,918 → ¥0.1096；修剪 34,898 / prompt 65,111 → ¥0.0955。
对比压缩请求只有 ¥0.040。5 个真实会话全量重放（含压缩请求成本、前缀命中、首包拟合）：

| 策略 | 总成本¥ | 总首包s | 压缩 | 修剪 |
|---|---|---|---|---|
| 只 compact@60k | 10.348 | 2,405 | 27 | 0 |
| 只 compact@80k | 10.445 | 2,490 | 17 | 0 |
| **只 compact@100k** | **10.826** | **2,595** | 12 | 0 |
| 只 compact@140k | 11.365 | 2,728 | 7 | 0 |
| prune@100k + compact@120k | 11.742 | 2,685 | 7 | 6 |
| **A 原现状 prune@100k+compact@140k** | **12.590** | **2,787** | 4 | 9 |
| 只 compact@180k | 12.778 | 2,999 | 5 | 0 |

prune 在每种组合下都更贵（8–12% 纯浪费）。**`[prune].max_tokens` 默认 100000 → 0**（留清推理）。
`[compact].max_tokens` 140000 → **100000**：60k 成本更低但压缩 27 次，而压缩的信息损失实测是
函数名 0%、报错 0% 覆盖，压得越频繁损失越大；100k 是成本与信息量的拐点。

**③ 真正的杠杆在「追加时就做减法」。** 首次压缩前完整历史（566 条 / 249,201 tok）的构成：
工具输出 41.1%、assistant 推理 29.7%（已不回传）、**工具调用参数 21.9%**、assistant 正文 6.0%。
参数里 edit 占 46.0%（61 次）、shell 34.5%、**write 16.8%（8 次，单次 912）**——
`edit` 的 `old_string`/`new_string` 和 `write` 的 `content` 里是整份文件，永久留在历史里每次重发。

### 做法：位置无关的确定性占位（`elide_args`）

`wire_messages()` 发送时把 edit/write 的大字段换成导航型占位（路径 + 行数 + 「需要时 read」）。
**规则只依赖调用本身，与位置无关** → 每条消息在第一次被发送前就是终态、字节此后永不变化，
**零缓存断点**。这比「按游标裁」好：游标每前进一步就制造一个断点，正是 prune 亏损的机制。

- 收益（43ca2f 全历史 2,467 条，按 `wire_messages` 真发出去的量算）：不裁 1,311,485 →
  `min_chars=150` 时 **1,171,278 token（-10.7%）**。阈值扫描：80→10.8% / **150→10.7%** /
  300→10.2% / 600→9.1% / 1000→8.4% / 2000→7.3%。80 与 150 已持平（占位变短后就如此），
  取 150 是因为调低会多裁 88 个净亏字段。
- **占位自身也要花 token**：中文约 1 字 1 token，第一版占位写了 68 字（“此处原有 N 行内容已省略……”），
  在短字段上吃掉大半收益。5 会话 508 个 150+ 字符字段按 token 实测：150–200 字符档 ROI 只有 **4%**，
  **67 个字段净亏**（裁了反而更贵 + 白白丢信息）。改成 44 字的短占位
  （`（N 行已省略，已写入 path；逐字原文请用 read）`）后：

| 占位 | 净省 token | 负收益字段 |
|---|---|---|
| 长占位（68 字） | 155,566 | 67 个 |
| **短占位（44 字，落地）** | **170,567（+9.6%）** | **0 个** |

  短占位在**每一档长度上都是正收益**（最差的 150–200 档也净省 8 token/字段）。5 会话共 927 个
  edit/write 字段、未裁总量 199,550 tok：阈值 120→省 86.8%（非正收益 0 个）/ **150→86.2%** /
  300→81.1% / 600→72.7%。阈值维持 150：120 与 150 只差 0.6%（噪声级），而 80/100 会多裁
  88/30 个非正收益字段。回归测试 `test_elided_placeholder_always_shorter_than_source` 把这个
  不变条件焊住。
- **净收益**（把省下的量 × 其后剩余请求数，对照「最坏情况每次裁都触发一次满额重读」）：
  5 个会话共裁 260 次，永久省下 35,406,730 tok，最坏补回 6,175,268 tok，**净 +29,231,462（82.6%）**。

### 安全性验证（真实网关，不是靠推断）

| 实验 | 内容 | 结果 |
|---|---|---|
| exp15 | 旧 edit 内容会不会被「回忆」 | assistant 正文里回忆 **0%**（0/292） |
| exp16 | 「被后续 edit 的 old_string 引用」里有多少无法从 read 补回 | 仅 20–33%，合计约 **6.3k token**（而裁剪省 72k） |
| exp20/22 | 裁剪后连续多步改同一文件 | 修正脚本 bug 后：orig 3/3、只裁 edit 3/3、**全裁 3/3** 文件全对 |
| exp24 | write 会不会覆盖已有文件 | 83 次 write 里 88% 是新文件；覆盖的 10 次**全是临时实验脚本**，无源码 |
| **exp25** | **配对测步数（各 6 轮）** | **裁剪组中位 6 步 / 均值 6.17，orig 组中位 7 / 均值 7.17；12/12 文件内容完全正确** |
| exp35/36 | 短占位（44 字）是否让模型改去“凭记忆重建” | 读到占位后 **6/6 先去 read**，与不裁组一致；步数 2–3、文件与答案 **6/6 全对** |

最后一项否掉了「裁剪会让模型多绕步」的担扰——它反而更少步（历史更短、上下文更干净）。
裁剪不改变模型能看到的信息（文件内容始终在磁盘上，read 随时可取），只去掉重复副本。

### 工具输出阈值

`truncate.py` 的 `MAX_LINES/MAX_BYTES` 2000/50KB → **800/16KB**（写入时截断，零断点）。
按真实截断语义扫描（5 会话，关 prune + compact@100k）：50KB→11.480 元 / 32KB→11.422 / **16KB→11.004** /
12KB→10.934 / 8KB→10.877 / 4KB→10.614。16KB 之后收益放缓，再往下 read 一个大文件就只剩开头。
实测工具输出总量 790,088 tok，read 占 48.8%、shell 37.7%，单条中位仅 129 tok，>1000 tok 的尾部贡献 30% 体积。

### 成本账（最终配置逐层叠加，5 个真实会话全量重放）

| 配置 | 成本¥ | vs 现状 | 总首包s | 压缩 | 修剪 | 命中 | 每次 miss | p50 prompt |
|---|---|---|---|---|---|---|---|---|
| A 现状 | 14.519 | — | 3,189 | 6 | 10 | 98.5% | 1,222 | 76,509 |
| B 关 prune + 压 100k（工具仍 50KB） | 12.262 | -14.6% | 2,930 | 14 | 0 | 98.6% | 872 | 57,228 |
| C B + 工具 16KB/800 | 11.771 | -18.0% | 2,889 | 12 | 0 | 98.6% | 827 | 50,670 |
| **D C + elide150（最终落地）** | **11.337** | **-21.1%** | **2,868** | 11 | 0 | 98.5% | **712** | **50,670** |

**成本**：不是变高，是降低 **21.1%**（14.519 → 11.337 元），总首包快 321 秒，每次请求平均 miss 少 42%。
修剪次数从 10 降到 0（该做的减法都改到「追加时」做了），命中率持平（98.5%）。

### 压缩水位：从 140k 降到 100k 的真实代价

| 配置 | 成本¥ | 总首包s | 压缩次数 |
|---|---|---|---|
| 压 100k（落地） | **11.399** | **2,886** | 11 |
| 压 120k | 12.141 | 3,042 | 8 |
| 压 140k | 12.681 | 3,145 | 7 |
| 压 160k | 13.369 | 3,290 | 5 |

100k 比 140k 便宜 **¥1.28（10%）**，代价是压缩多 4 次。把「压缩丢标识符 → 重摸索」折成 token
（保守估：每个标识符 3000 token，每次压缩涉及 20 个）后，「成本+损失」仍是 100k 最低（12.376 vs 13.303）。

### 会不会变傻：三个可测量的面

**① 已写内容会不会丢**——不丢。elide 只去掉历史里的**重复副本**，文件始终在磁盘上，read 随时可取。

**② 会不会多绕步**——exp25 配对实测（各 6 轮）：裁剪组中位 **6 步**、均值 6.17；orig 组中位 7、均值 7.17。
12/12 轮文件内容完全正确（长度均 2466）。**裁剪组反而少 1 步**：历史短、上下文干净。

**③ 压缩摘要会不会漏关键信息**——这是**唯一真实损失面**，且原来就很差（实测函数名 0%、报错 0% 覆盖）。
既然压缩次数不可避免地要从 6 增到 11，**必须把每次压缩本身变好**。所以强化了
`compact.md` / `compact_update.md`：把「函数名、报错原文、可执行命令、文件路径必须逐字抄写」
提到摘要指令的第一段，并明确「不许概括、不许改写、不许省略」。

实测（9adcf9 压缩前 240 条消息为输入，真实网关，各 6 轮，取中位数）：

| 提示词 | 函数名覆盖（中位 / 范围） | 路径覆盖（中位） | 摘要长度 |
|---|---|---|---|
| 原 `compact.md` | **15%**（14–18%） | 37% | 4.5–5.8k 字 |
| 强化后 | **19%**（0–27%） | 43% | 5.2–9.6k 字（1 次返回空） |

函数名覆盖从 15% 提到 19%（中位），**远低于早先 2 轮样本给出的 26%**（那两次是运气好的抽样）。
所以「压缩次数 6→11」的损失**不能被提示词完全抵消**，只能说略好一点。

### 工具输出阈值的真实代价（补测）

先前只量了 16KB 省多少钱，没量它会不会让模型多绕步。补测两个场景（真调 `truncate_output`，4–6 轮/组）：

- **定位型**（3000 行 / 159KB，grep 可用）：16/32/50KB 均 3/3 正确，步数中位 2/3/2，回传 10k/15k/15k。
- **通读型**（2000 行 / 50KB，必须数完）：**16KB 与 32KB 均 6/6 正确，步数中位均 3，回传均 14k**。
  （exp30 曾出现 32KB 组 1/3 正确，那是模型自身的算术错误，加大样本后不复现。）

结论：**16KB 在实测任务上没有观察到「多绕步」的代价**。模型实际很少把整个大文件一次读回来，而是用 grep 定位或分段 read。

### 三项叠加（之前完全没测，是本轮补的最大空白）

write 400 行文件 → 连改两处 → 问值（4 轮/组）：

| 配置 | 正确率 | 步数中位 |
|---|---|---|
| 三项全关 | **4/4** | **5** |
| 三项全开（落地） | **4/4** | **6** |

这是本轮唯一稳定的代价：**叠加后平均多 1 步**（模型多 read 一次拿旧 edit 的原文）。一次 read 约 1–2k token，
相比每请求省下的 700 token × 几十次请求，仍然划算，但不是零成本。

### 界面与未验

- `breakdown.py` 新增 `_prune_on()`：消息级修剪关着时 `prune_tokens` 上报 `null`，前端不再写「60% 时修剪旧工具输出」
（否则用户会以为它还会动手）。`compute()` 也按同一 `elide_min_chars` 算，界面与真正发出的请求口径一致。
- **已知代价（不是没代价）**：
  - 压缩次数 6 → 11，而强化后的摘要函数名覆盖只有 19%（不是 26%）：压缩损失增大 40%。
  - 三项叠加后平均多 1 步（read 一次）。
  - 强化提示词 6 轮里有 1 次返回空摘要（推理吃光预算）；实际压缩有重试和失败回退，但值得盯。
- **仍未验**：
  - 压缩次数 11 vs 6 对「跨压缩记忆连续性」的影响无法用离线指标衡量，只能靠实际使用观察。
  - 全部实验都是单会话、单模型（deepseek-v4.1-flash），没跑多模型对照。
- **退路（都是单开关）**：
  - `[elide_args].enabled = false` → prompt +15%，步数回到基线
  - `[elide_args].min_chars = 300` → 少裁 40% 字段，裁剪收益从 10.7% 降到 10.2%（仍正收益，只想更保守时用）
  - `[compact].max_tokens = 140000` → 压缩回 6 次，成本 12.681（仍比现状省 12.7%）
  - `truncate.MAX_BYTES = 32*1024` → 成本 +0.4%，实测无行为差异

## 执行记录（2026-11-02，缓存命中率：零断点清推理）

用户反馈：上下文减负上线后，长会话命中率从 99% 掉到 97%。查下来**不是改坏了，是分母变小了**：

| 时段 | 请求数 | 平均 prompt | 命中率 |
|---|---|---|---|
| 10-31 13:41 → 11-01 16:00（减负前） | 1659 | 100,970 | 97.08% |
| 11-01 20:19 → 现在（减负后） | 636 | 118,717 | **98.12%** |

四份日志（含轮转 `.1/.2/.3`，覆盖 10-28 起）给出同一条曲线：prompt <60k → 88–93%，60–120k → 96.4–97.6%，
120–180k → 97.6–98.7%，**180–300k → 99.1%**。每请求约 1.4–2.2k 固定未命中（新写出的内容 + 缓存断点），
不随 prompt 变大；以前长会话常在 200k+，1.4k/200k = 0.7% → 99%，被压到 100–130k 后同样的 1.4k/120k = 1.2% → 98%。

**原计划的「攒批推进游标」被数据否定**（用真实会话 1214 条消息 / 32 个用户回合离线复算，逐请求比对字节前缀）：

| 策略 | 命中率 | 每请求未命中 |
|---|---|---|
| 现状（水位 10 万，due 时推进游标） | 99.21% | 1715 |
| 攒批推进（门槛 5k / 12k / 20k） | 99.24–99.25% | 均摊不变，无效 |
| 每个用户回合边界推进 | 98.95% | 更差 |
| 清推理搭车工具修剪 | 99.24% | prompt 涨到 248k，不划算 |
| **零断点：每条推理首次进历史即清** | **99.42%** | **1163（-32%）** |

原理：命中率 = 1 − U/P，U 里有相当一部分是模型每步新产生的推理（必须回传，否则同一回合内 400）。
把清推理从水位触发改成「每条 assistant 在它第一次被发送之前就清好」，这条消息的字节从此永不变化——
断点归零，同时 prompt 少 26%，两者同向优化，不是取舍。

- **实现**：`prune.py` 新增 `sync_reasoning(session, cfg)`（幂等、与水位无关）；`keep_reasoning_turns` 默认 2 → **0**；
  `maybe_prune` 只管工具输出。`session.py` 在 `_sample()` 每次真实发送前同步（放这里而不是 `_maybe_auto_compact`：
  压缩重建、400 回退、overflow 重试都绕过后者），`_summarize()` 也同步一次。
- **真实网关验证**（opencode + `deepseek-v4.1-flash11`，带 tools）：四种回传变体全 OK、无 400——
  全带 / 同回合内部分置空 / 同回合内全置空 / 完全省略 `reasoning_content`。
- **取值依据**：`keep_reasoning_turns` 最终定为 **0**。三个真实长会话离线复算（20 / 22 / 35 回合）里
  `keep=0` 在命中率、平均未命中、prompt 三项上全部最优（平均未命中比 `keep=2` 低 30–45%）：

  | 会话（回合） | keep=0 | keep=1 | keep=2（原实现） |
  |---|---|---|---|
  | 20 | **98.67% / 1067** | 98.16% / 1561 | 97.68% / 2091 |
  | 22 | **98.16% / 1236** | 96.82% / 2138 | 96.00% / 2790 |
  | 35 | **99.51% / 1155** | 99.29% / 1703 | 99.13% / 2161 |

  （命中率 / 每请求平均未命中 token）
- **行为质量实测**（真实网关，4 个多步工具任务 × 2 档）：`keep=0` 与 `keep=2` 都是 **4/4 答对、总步数 7、无重复调用**。
  即同一回合内不回传上一步思考，在实测任务上没有观察到退化。
- **测试**：`test_every_request_is_prefix_of_next` 把每次真实请求序列化后逐字节比对，断言它是下一次的前缀，
  同时断言推理确实为空。双向验证过：临时移除 `_sample` 里的同步，用例立刻红（第一版只比对前缀、
  「从不清推理」也能过，是假绿，已补断言）。
- **界面**：`savings` 把「旧推理不回传」与「修剪工具输出」拆成两个字段（原来共用 `pruned`，会把工具修剪的数字说大）。
- **未验**：`keep_reasoning_turns=0` 意味着同一用户回合内模型看不到自己的上一步思考。真实网关 4 个多步任务上
  没有观察到退化（4/4 答对、步数持平），但任务集偏小、判定偏粗，不能当成对长任务的保证。
  出现退化时把 `[prune].keep_reasoning_turns` 调回 2 即可回到旧行为（代价是命中率与 prompt 变差）。

## 执行记录（2026-11-02，验证链路补齐）

这一批没有动产品功能，补的是「怎么知道自己没坏」（之前几个真事故就是因为没人跑）。

- **e2e 隔离（`eb3edf1`）**：7 个 Playwright spec 里只有1个隔离了 `EE_HOME`、0 个隔离 `userData`。两个后果都实测到了：日常开着 ee 时 `test:ui` 必然报 `Target page closed`（单实例锁 + disk cache 被锁）；`interaction.spec.ts` 的 `settings/set` 直接写用户真实的 `~/.ee/config.toml`（把 `default_model` 改成了指向本地假服务的 fake-gpt），`workspace/remove` 还会隐藏真实项目。现在统一走 `desktop/e2e/launch.ts`（临时 home + 临时 userData），跑完即删。结果：27 passed / 1 skipped、57 秒（改前 8 分钟 4 failed），跑前跑后配置 sha256 一致。
- **e2e 进 CI（`8457ad5`）**：这个套件长期只靠本地手动跑，所以上面的缺陷能静默存活。现已接在 `npm run build` 之后，远端实测 27 passed / 1 skipped、47 秒。
- **后端静态检查（`b7968c6`、`a8903dc`）**：`ruff` 只启用「代码有误」那组（`E9/F63/F7/F82/F401/F811/F841`），不做风格强制；清掉产品代码 4 处未用导入、测试 51 处未用导入与 10 处未用变量（其中 1 处补了漏掉的断言）。依赖补了主版本上界，避免上游发大版本把 CI 打红。
- **发布链路首跑（run #152，手动触发 `package=true`）**：验证与打包全绿——PyInstaller onedir 后端、打包后端 `selftest --secrets`、假模型 E2E、黑洞超时 E2E、NSIS 安装包、node-pty 在 asar 外、两份 artifact 上传。（当时记的「仍未做：代码签名 / 版本号策略 / 更新渠道」已于 2026-11-02 逐项给出结论，见下条。）
- **tag 发布与签名结论（2026-11-02）**：新增 `.github/workflows/release.yml`，推 `v*` tag 自动发版；`desktop-windows.yml` 加 `workflow_call` 让发布复用同一份 verify+package（不写第二套打包逻辑），产物补上 `latest.yml`/`.blockmap`。版本号由 `check` job 硬校验 tag 与 `desktop/package.json`、`ee/pyproject.toml` 三处一致——不一致会导致更新器永远重复下载同一版。
  - **签名：明确不做**。查微软文档后推翻了原先「自签名能缓解」的假设：自签名证书与不签名在 SmartScreen 上**表现完全相同**，而且 **EV 证书也已不再绕过** SmartScreen。花钱买房只买到「显示发布者名字」而不是「消除警告」。剩下真正能消警告的只有 Azure Artifact Signing（$9.99/月，首年仍要攒信誉）与 Microsoft Store（免费零警告，要过审）。现在采用「不签名 + 公开 SHA256 + 安装说明讲清原因」。
    来源：https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation
  - **未验**：本地 `electron-builder` 在 `packaging` 阶段卡住（本机拉不到 Electron 45 二进制），所以 `latest.yml` 生成与 Release 上传脚本**只有代码审查，没有实跑证据**；待推一个 tag 验证。客户端尚未接 `electron-updater`（`build.publish` 已配好，随时可接）。
  - **→ 2026-11-02 已实跑打通（`v0.1.0-rc.1`，run #4 起连挂四轮，第 5 轮全绿）**。发布链路现在有完整实证：`check`→`verify`（Backend tests 7min、E2E 真实 Electron 48s）→`package`→`publish` 全绿，Release 页上有 `ee-Setup-0.1.0.exe`（152,091,121 B）、`latest.yml`、`.blockmap`、`SHA256SUMS.txt`、`builder-debug.yml`。`latest.yml` 内容已核对：`version: 0.1.0`、`path: ee-Setup-0.1.0.exe`、sha512 与 size 均与实际上传的安装包一致。
    - 四次失败各是一个独立的坑，都只在实跑时暴露，记下来免得重犯：
      1. `--target` 对已存在的 tag 无意义（只在自动建 tag 时用），已移除。
      2. 预发布 tag（`-rc.1`）不能拿去比 `package.json` 的完整版本号，只能比基础版本（`${VER%%-*}`）。
      3. **publish job 不 checkout 就没有 `.git`**，`gh` 找不到 `-R`/`GH_REPO` 时会去读 git remote，死在 `failed to run git: fatal: not a git repository`。修法：加 `actions/checkout` + 显式 `GH_REPO` + `--repo`，并在 job 上显式声明 `permissions: contents: write`。
      4. **PowerShell 的 `@pre` / `@assets` splat 在 `-File` 模式下被拆成单字符**，gh 报 ``no matches found for `-` ``（那个 `-` 就是 `--prerelease` 被拆出的第一段）。修法：先拼 `@argv` 数组再 `& gh @argv`。
    - 附带修的一个真问题：`playwright.config.ts` 只有 per-test `timeout`（靠 JS 定时器，进程挂死时自己也停），实测 CI 上卡过 44 分钟不退、把 verify 拖过 45 分钟 job 上限而 cancelled。已加 `globalTimeout: 12 分钟` 兜底。
    - **失败现场要自己留**：GitHub 工作流日志要仓库管理权限（API 403、页面要登录），所以 publish 失败时把 gh 输出 Tee 到 `dist-out/gh-release.log` 并上传 `release-publish-log` artifact。
    - 仍未做：客户端未接 `electron-updater`（`build.publish` 已配好，随时可接）。
- 仍成立的历史边界：`test_goal_acceptance` 与 `test_bgshell_guidance` 各失败过一次，但都在对应的针对性修复之前，此后连跑全绿（后者本地连跑 3 次）。

## 修订（2026-11-01）

清掉 2026-10 各节里「桌面端从未启动过」「远端触发未验证」这类已经不成立的结论。只改状态描述，不动当时的验证数字；仍成立的边界保留，但把「未验」收窄到具体控件和视觉项。

- **桌面端已在日常使用**：启动、三栏、会话与审批主流程都有实证。仍没有核对记录的是间距/字号/动画/`+` 菜单 `side="top"` 的弹层方向，以及 best-of-n 的判据输入框与「验证 N 个候选」按钮。
- **CI**：`push main` / `pull_request` 自动触发已恢复（`.github/workflows/desktop-windows.yml`），`ee/tests/test_ci_offline_gate.py` 守着这条；~~`verify→package` 的远端依赖关系仍没走过~~ → 2026-11-02 已走过（run #152 全绿，见下一节）。
- **T2、导出与 Goal 桌面端入口、D6-1 编排关系模型**：状态从「未做 / 未验」更新为已完成（行内用 `~~旧结论~~ →` 标注）。
- **视觉验收闭环**：内核原先没有截图通路，UI 改动只能由用户肉眼验收。→ 2026-11-01 已补上（见下一节），现在 agent 能自己截一张当前窗口的图并看。

## 执行记录（2026-11-01，回合内看图 + 桌面端截图）

起因：想给「改完 UI 自己看一眼」搭一条路，读代码时发现两个前置问题。

| 问题 | 实测现象 | 修法 |
|---|---|---|
| `pending_images` 只在 `_add_user`（回合开始 / 下一条用户消息）注入 | `read(as_image=true)` 本回合里看到的是「下一轮可见」——模型得先把回合交还给用户，等于不可用 | `Session._flush_pending_images`：工具返回后、下一步采样前补一条 `_kind="note"` 的 meta 用户消息（不污染用户原话/界面）；`test_midturn_images` 3 例 |
| 没有截图通路 | `grep capturePage desktop/electron` 零命中 | 新增 `core/screen.py`（等待表 + PNG 校验）、`tools/screenshot.py`、notify `ui/capture`、RPC `ui/capture_result`、Electron `win.webContents.capturePage()` |

端到端探针（真后端 + 假模型 + 假桌面，走真 WebSocket）当场抓到两个只靠单测发现不了的缺陷：

| 缺陷 | 后果 | 修法 |
|---|---|---|
| `screenshot` 没进 `permissions.READ_TOOLS` | 走「未知工具」分支：默认模式每次弹审批，只读模式直接拒 | 加进白名单 + `test_screenshot_is_a_read_tool` |
| 工具把 `CaptureRequest` 发给了 `ctx.emit` | `ctx.emit` 是往工具输出追加文字的字符串通道，直接 `TypeError` | 改走 `ctx.extra["emit_event"]`，并在 `screen.request` 的注释里写明 |

取舍：截图只在 `EE_DESKTOP=1`（桌面端下发的后端）注册——CLI / headless 没有窗口，不该背 schema 开销；`FULL_LIMIT` 4500 → 4650，抬高部分正好等于这个工具（实测 +113 近似）。

验证：后端 `test_server / test_permissions / test_screenshot / test_midturn_images / test_prefix_budget / test_tool_output_gc / test_rpc_contract / test_loop` 123 passed；`scripts/verify.sh --quick` 全部通过（含前端 293 例 + tsc）；真模型验图（同回合读出随机码 `JOMT-9163`）；端到端探针 PASS（`ui/capture` 1 次、截图 8×8、模型下一步收到 1 张图）。

未验证：真机 Electron 窗口的 `capturePage` 结果（需要重启桌面端才加载新代码）；截图的可读性（Retina 缩放后字太小 / 暗色主题对比度）需要看真实渲染才知道。

## 执行记录（2026-10-31，全仓体检修复 + 子代理优化）

来源：`2026-10-31-agent-audit.md`（体检报告，79 条发现）。**修复前先做了实测复现**，
推翻了原文 7 条结论（详见该文件「复核修正」章节）。共 31 个提交，`69fcdd8..4ad62ab`。

**安全与数据正确性（P0 级）**

| 问题 | 修复 | 验证 |
|---|---|---|
| 孤儿 tool_calls 导致会话永久 400 | `load_messages` 与**生产路径 `SessionStore.load`** 都补缺口；`_turn` 加 `finally` 兜底 BaseException | 真红灯：补齐全 `False → True` |
| 危险短选项连写绕过只读模式（`git -cX`、`sort -no`） | `_any_short_opt` 按簇逐字母扫 | 探针实测：plan 4 条 deny、default 4 条 ask |
| `.eeignore` 不挡 shell（`cat .env` 免问可读） | `_eeignored` 加 shell 分支；长选项值不误判 | 真红灯 + 实测对比表 |
| 同 cwd 有会话在跑时 undo/restore 会覆盖对方成果 | 两处入口都加同 cwd 互斥检查 | 两个真红灯 |
| 排队消息中途换模型（缓存全失效） | `_pending_*` 延迟到回合真正开始 | 真红灯：`flash → pro` |
| 审批等待无超时 → 会话永久 running | `APPROVAL_TIMEOUT=300s`，超时按拒绝 | 回滚后 25s 被掐死 |
| undo 无结束快照时静默覆盖用户手改 | 退到 `before` 做基线（代价：多弹一次确认，已加测试锁定） | 真红灯 |

**稳定性**

- `hooks.fire` 的 `gather` 加 `return_exceptions=True`：一个 hook 崩了不再打断整回合
- 流无 `finish_reason` 时抛错（区分「端点不发该字段」的兼容端点，不误报）；`truncated` 真正可重试
- 流中途失败：已显示的回答落库 + 记账（`usage_missing`）
- `thread/children`、`thread/status` 改 `async + to_thread`（原本阻塞事件循环）
- LSP manager 在 `aclose` 里 release/shutdown（原本全仓无调用，每会话泄漏一个子进程）
- MCP 连接失败后下回合重试；`_mcp_done` 只在流程走完才置位
- `store.append` 写盘失败不再静默吞掉

**子代理（本轮重点优化）**

| 改动 | 依据 | 实测效果 |
|---|---|---|
| 并发闸门跨步共享 | 原来每步 new 一个 Semaphore，「最多 N 个并发」只在单步内成立 | 上限真正生效 |
| 步数按类型分档 | 写型 30 步常在收尾前用完，只能重派一次 | `general` 60 / `explore` 30 |
| `explore` 用回 shell | 上一轮把它删了，等于砍掉「跑测试看错误」的能力 | 沙箱下自动换 powershell（Git Bash 在受限令牌里起不来） |
| 只读子代理轻量上下文 | `research/claude-code.md:24`：Explore/Plan 跳过 CLAUDE.md | explore 首请求消息字符 **6486 → 2391（-63%）** |
| `<task_digest>` 结构化摘要 | 从实际工具调用推（改动文件/执行命令/出错工具），不让模型自述 | 主代理不必读文本猜「改完没」 |

**测试基础设施**

- `BgManager.wait_exit`：原来的固定轮询（上限 5 秒）在负载下会读到空输出，是 `test_bgproc_gbk_output` 偶发红的真因
- `test_rpc_contract` 抓到 `approval/respond` 参数名写错（`decision` → `choice`），组件测试补了同类断言

**验证状态**：后端全量 `1946 passed, 16 skipped`（1101 秒）、前端 `288 passed`（57 文件）、`tsc --noEmit` 0 error，GitHub Actions 全绿。

**未做**：P1-10（Electron 重启不等旧后端退出）、P2/P3 里未经复现的条目。P1-8（子会话审批在父界面不可见）本轮已做：`Composer` 顶部加横幅 + `store.clearChildApproval`，前端测试 5 passed。

## 执行记录（2026-10-29，T10 桌面端入口与 RPC）

把 T10 的五项从「只有后端」推到「桌面端可达」，并补上界面缺的后端入口。提交：

```
684500a feat(ui): 交接清单与候选比较的界面入口，四个 T10 能力全部可达
1c55d81 feat(rpc): 文件交接与 best-of-n 的 RPC，补上界面缺的后端入口
9eb4010 feat(ui): Codex 式「目标」入口，两级菜单接进输入区
031dd19 feat(goal): Goal 接进 turn/start，会话内可用
31488cd feat(ui): T10-c 审批卡片显示建议，并补上后端缺失的落库
441354f feat(ui): T10-a U3 会话导出入口，接到会话菜单
```

界面形态按用户要求做成 Codex 式：输入框 `+` → 分组菜单 → 每项带图标 / 标题 / 灰色描述；
`Menu`（`desktop\src\components\ui.tsx`）扩展出 `sections` 与 `desc` 支持双行与分组。
视觉全部沿用 `desktop\src\index.css` 的 CSS 变量，未引入第二套体系。

本批顺带修掉的既有缺陷（「生产线从没通过电」，与 LSP 那次同一形态）：

| 缺陷 | 锚点 | 后果 |
|---|---|---|
| 建议态没落进 `ApprovalItem` | `ee\ee\server\threads.py` 的 `_on_event` | 后端有建议、前端永远拿不到 |

（`_consume` 的 goal 分支从一开始就在 `try` 内，`threads.py:444-449` 的注释写明了「提前 `return` 会跳过 `finally` / `_finish()`」。这是 031dd19 一并做对的，不是修出来的缺陷。）

验证（本轮实测）：

| 单元 | 命令 | 结果 |
|---|---|---|
| 后端 11 个 T10 测试文件 | `pytest test_export{,_rpc,_cli}.py test_goal_turn.py test_autoapprove{,_wiring,_persist}.py test_handoff{,_rpc}.py test_bestof{,_rpc}.py` | 97 passed（23.15s） |
| 前端 5 个 T10 套件 | `vitest ExportDialog ApprovalSuggestion GoalMenu HandoffDialog BestofDialog` | 25 passed（5 files） |
| 类型 | `tsc --noEmit -p tsconfig.json` | exit 0 |
| CI | `684500a` `desktop-windows` | success |

未执行 / 未验证：

- ~~**桌面端从未启动过**~~ → 2026-11-01 纠正：桌面端已在日常使用，启动与主流程可用已有实证。当时未验的具体项（间距、字号、弹出方向、动画、`+` 菜单的 `side="top"` 实际弹出位置）仍没有逐条记录，jsdom 断言也仍然证明不了这些。
- **`GoalMenu` 键盘导航未测**：↑↓ 选择、Esc 关闭只有实现没有断言。
- ~~best-of-n 端到端不可用~~ → 已在 `5e33775` 补上 `bestof/verify`（见上节「判据编排」）。**仍未在真实桌面端点过**：判据输入框与「验证 N 个候选」按钮只有 jsdom 断言。

### best-of-n 判据编排（`5e33775`）

交接文档里唯一的实质卡点：界面把 worktree 列表当候选提交给 `bestof/pick`，
但**不带 verdict**，后端一律判未验证、结果永远「没有候选通过验证，不选」。

取舍：**每候选只跑一轮 `goal.judge`，不跑 `run_goal` 的续跑循环**。
候选代码已经写完了，再让 agent 改它就把「观测」变成了「修改」——证据不再属于同一个候选。
代价是 N 个候选 = N 次判据，按 `goal.timeout`（默认 300s）封顶，界面预算 600s。

| 不变量 | 实现 | 测试 |
|---|---|---|
| 只观测不修复 | 只调 `judge`，不进 `run_goal` | `test_verify_picks_the_green_worktree` |
| 路径限定 | 只接受 `worktree.list_for()` 登记过的目录 | `test_verify_refuses_unregistered_path` |
| 判据校验不复制 | 复用 `threads._norm_goal`（命令非空、轮次上限、超时正数） | `test_verify_rejects_empty_command` |
| 自述不算证据 | `self_reported_ok` 只记录不参与判定 | `test_verify_ignores_self_reported_pass` |
| 全红不硬挑 | `bestof.pick` 返回 `None` | `test_verify_none_when_all_red` |

验证（本轮实测）：

| 单元 | 命令 | 结果 |
|---|---|---|
| 判据编排 | `pytest test_bestof_verify.py` | 5 passed（真实 pytest 判据，无 mock 中间层） |
| RED 确认 | 把实现改回「不跑判据、verdict 留空」 | 1 failed（`assert False is True`，"没有判据结果" —— 正是原始缺陷的症状） |
| bestof 回归 | `test_bestof.py` + `test_bestof_rpc.py` + `test_bestof_verify.py` | 23 passed |
| 前端 | `vitest BestofDialog.test.tsx` | 7 passed（2 个新增：填判据走 verify、留空退回 pick） |
| 前端 RED 确认 | 把 `if (command.trim())` 改成恒假 | 1 failed（断言 `bestof/verify` 被调用） |
| 类型 | `tsc --noEmit -p tsconfig.json` | exit 0 |
| RPC 合约 | `pytest test_rpc_contract.py` | 1 passed（1.02s） |

未验证 / 保留边界：

- **判据输入框与「验证 N 个候选」按钮**：只有 jsdom 断言，没有在真实 Electron 窗口里操作过的记录（桌面端本身可用，未见的是这两个控件的实际操作）。
- **`test_rpc_contract.py` 只跑了 1 个用例、1.02s**——交接文档说它会扫描整个 `desktop\src`、约 3 分钟。这里 1 秒跑完说明本次没有触发那一步扫描，**不能当作前端 RPC 名一致性已验证**。
- 每个候选串行还是并行：实现里用 `asyncio.gather` 并行跑，但 `ee\tools\shell.run_command` 是否在同一事件循环里争用未实测。

## 执行记录（2026-10-29，T10-c/d/e）

| 单元 | 验证（真实执行） | 结果 |
|---|---|---|
| T10-c 建议态纯函数 | `pytest test_autoapprove.py` | 10 passed（先 RED 10 failed） |
| T10-c 接线 | `pytest test_autoapprove_wiring.py` | 5 passed（先 RED 5 failed） |
| T10-c 权限回归 | `test_permissions` + `test_rules_ask_ignore` + `test_session_lifecycle` + `test_loop` | 89 passed |
| T10-d 评选 | `pytest test_bestof.py` | 14 passed（先 RED 14 failed） |
| T10-d 真实衔接 | 两 worktree 各一候选，真实 pytest 判据：A 失败→未验证、B 通过→已验证，pick 选 B | 通过 |
| T10-e 清单与核对 | `pytest test_handoff.py` | 12 passed（先 RED 12 failed） |
| T10-e 真实端到端 | 本仓库 236 条清单 → 复制 → 逐字节核对 0 问题；故意篡改后精确报「哈希不一致」 | 通过 |
| CI | `7f1dccf` / `ca78136` / `a11697f` | verify 全绿 |

三项的共同取舍：**都只做不可逆动作之前的那一半**。
- 建议态不碰决策（没有能力把 ask 变 allow）；
- best-of-n 只评选（worktree 创建与合并留给调用方与用户确认）；
- 交接只出清单与核对（复制/切换/删除是用户确认后的分步动作）。

未做 / 保留边界（截至 `ef9ca1e`；后续界面/RPC 见下一节）：
- ~~三项均**无桌面端界面**~~ → 已在 `684500a`/`9eb4010`/`1c55d81` 补齐入口，见「执行记录（2026-10-29，T10 桌面端入口与 RPC）」。
- 自动审批的**自动放行版本**未做：方案要求另行安全审查，不擅自实现。
- best-of-n 的 run_id/费用采集未接（`Candidate` 有字段，调用方负责填）。
- ~~best-of-n 的判据编排未做~~ → 已在 `5e33775` 补上 `bestof/verify`：每候选**单轮** `goal.judge`（不跑续跑循环，只观测不改代码），路径限定在 ee 登记的 worktree 内，判据校验复用 `threads._norm_goal`。代价：N 个候选 = N 次判据，并按 `goal.timeout`（默认 300s）封顶；界面把 `bestof/verify` 的超时预算设为 600s。
- 文件交接与 `worktree.create/remove` 的衔接未接（复用现有接口，不在本单元）。



| 单元 | 验证（真实执行） | 结果 |
|---|---|---|
| T10-a 导出核心 | `pytest test_export.py` | 11 passed |
| T10-a 导出 RPC | `pytest test_export_rpc.py` | 10 passed |
| T10-a 导出 CLI | `pytest test_export_cli.py` | 8 passed |
| T10-a 真实会话导出 | 5 个真实会话，items 数无损、工具参数 3/3 结构化、Markdown 零缺失 | 通过 |
| T10-b U1 判据 | `pytest test_goal.py` | 12 passed |
| T10-b U2 续跑 | `pytest test_goal_runner.py` | 9 passed |
| T10-b U3 CLI | `pytest test_goal_cli.py` + `test_goal_exec.py` | 11 passed |
| T10-b 闭环验收 | `pytest test_goal_acceptance.py`：真实 pytest 判红 → agent read+edit 修复 → 判绿 → 退出 0 报 met | 2 passed |
| goal 全量回归 | 六个测试文件（含 test_exec） | 46 passed |
| e2e 修复前 | `python scripts/e2e_ws.py` | 68s 失败（60s 内没有 turn/completed） |
| e2e 修复后 | 同上 | rc=0，19.36s 通过；`--blackhole` 18.26s 通过 |
| CI | `9f138fa` / `520d29d` / `48a8172` / `4328907` / `cbc5328` | verify 全绿 |

e2e 卡住的根因（非后端）：`e2e_ws.py` 的 `reader()` 把每条通知 `json.dumps` 后截 400 字符同步 print，
`item/delta` 密集时堵住事件循环 → reader 停止消费 ws → 后端 `ws.send` 背压 → 后续通知（含 approval）
全排在队列里出不来。顺带修掉两处隐蔽缺陷：审批响应边遍历 `notes` 边 `remove`（同批两条审批会抛
`ValueError` 被外层吞掉）、超时诊断只报一句「没有 turn/completed」。

未验证 / 保留边界：

- ~~导出与 Goal 的**桌面端入口**未做~~ → 已在 `684500a`（会话行菜单「导出…」）、`9eb4010`（`+` 菜单「目标」）补齐，`turn/start` 接 goal 见 `031dd19`。
- Goal 判据的测试计数解析只保证 pytest；vitest / go test 解析不到会按 0 处理（保守方向：
  表现为跑满轮次后 exhausted，不会假成功）。
- `ee export` 的 Electron 保存对话框路径未验（当时本机没启动过桌面端；桌面端现在已日用，但这个对话框仍没有走通的记录）。
- 附件不参与导出：实测 items 里没有 image/attachment 字段（`threads.py:289` 接收但未落 items）。



发现方式：按计划原文去补「真实 pyright 集成用例」，探针一跑就暴露了生产线从没通过电——
假服务器用例 30 例全绿，但它们手工注入 `LspTool(manager=..., store=...)`，绕过了接线。

| 缺陷（实测，非推断） | 锚点 | 修法 |
|---|---|---|
| `lsp_manager`/`lsp_store`/`lsp_notifier` 全仓无赋值点，只在 `__init__` 置 None | `ee\ee\core\session.py:160-162` | 新增 `_attach_lsp`，照 `_attach_mcp` 的惰性一次接线 |
| `_notify_lsp` 首行 `if self.lsp_notifier is None: return`，写后通知永不执行 | `ee\ee\core\session.py:1254` | 同上；`_attach_lsp` 在回合开始处调用 |
| 工具表里 `LspTool()` 无参构造，`_store_from_ctx` 取到 None，永远报「没有启用」 | `ee\ee\tools\__init__.py:89`、`ee\ee\tools\lsp.py:92` | `_attach_lsp` 把 manager/store/root 补给已注册的工具 |
| 全仓无人注册 `publishDiagnostics` 回调，`DiagStore.on_publish` 无生产调用点 | grep `on_notification\|on_publish` 只命中定义 | `_attach_lsp` 里 `client.on_notification[...] = store.on_publish` |
| pyright 把盘符小写化（回 `file:///e%3A/`，我们发 `E%3A`），按字符串做键导致诊断整条丢弃 | 探针实测：`uri_match=False`、`on_publish -> False` | `diagnostics.norm_uri()`，DiagStore 读写路径统一归一（只归一 scheme 与 Windows 盘符，POSIX 大小写保持敏感） |

验证（真实执行）：

| 单元 | 命令 | 结果 |
|---|---|---|
| 真实服务器端到端 | 探针：中文目录 + 中文文件名，真实 pyright | `status=fresh n=2`，工具输出 `1:10 错误 Type "Literal['hello']"...` / `2:5 错误 "undefined_name" is not defined` |
| RED 确认 | 回滚 `on_publish` 的 `norm_uri` 调用后跑 `test_publish_with_lowercase_drive_still_accepted` | 1 failed（`assert False is True`，红灯落在被测层） |
| LSP 单元 | `pytest ee/tests/test_lsp.py` | 30 passed |
| LSP 接线 + 集成 | `pytest ee/tests/test_lsp_integration.py` | 5 passed（含真实 pyright 1 例，不跳过） |
| D6-2 停点回归 | `pytest test_lsp.py test_lsp_integration.py test_details.py test_redo_security_config.py test_rpc_contract.py` | 63 passed |
| 会话链路回归 | `pytest test_loop.py test_server.py test_session_lifecycle.py` | 33 passed |
| 前端 | `npm run test -- AgentSettings.test.tsx` | 3 passed |

未验证 / 保留边界：

- **typescript 语言服务器未验**：本机没有 `typescript-language-server`，只有 python 路径跑过真实服务器。
- **进程退出/崩溃恢复未用真实服务器验**：`test_crash_restarts_on_next_acquire` 仍是 Fake client。
- **编辑器里的实际观感未验**：工具输出的可见性只验到字符串层面，没做视觉核对（桌面端已在日用，但这一项不是启动就能证明的）。
- 集成用例用 `npx --package pyright` 启动，首次拉包约 60 秒；已给它 120 秒预算。机器上直接有 `pyright-langserver` 时走 PATH，不经过 npx。
- 会话内只起一种语言的服务器（按根目录特征文件判定），多语言项目需要后续扩。



- 核心会话、流式工具循环、审批、队列、压缩/缓存、模型档案：`ee\ee\core\session.py`、`providers\`；测试 `ee\tests\test_loop.py`、`test_queue.py`、`test_prefix_cache.py`。
- 桌面三栏、本地 RPC、设置、终端、文件预览：`desktop\src\`、`desktop\electron\`、`ee\ee\server\`；前端 Vitest 与 `ee\tests\test_rpc_contract.py`。
- worktree、磁盘快照、时间线、按块审阅：`ee\ee\core\worktree.py`、`snapshot.py`、`hunks.py`；对应 worktree/checkpoint/hunk 测试。
- task 子代理、自动化、会话内 /loop、预算：`ee\ee\core\subagent.py`、`budget.py`、`ee\ee\server\automations.py`、`loops.py`。
- MCP、Hooks、Skills、记忆、凭据存储均有生产实现；API key 优先级为凭据存储 > 旧明文 > 环境变量。项目配置不得提升用户授权（MCP 信任、预算、权限、沙箱都是这个方向）。
- 最近编辑安全修复：唯一匹配、原子写、混合换行、同 index 工具分块隔离。

## 执行记录（2026-10-28，D6-1 Wave A/B）

D6-1 的 U1–U7 已全部提交并推送（`9d867f6`、`d68af00`、`58e1e67`、`7f2db43`）。回归实测：

| 单元 | 验证（真实执行） | 结果 |
|---|---|---|
| U1–U4 编排模型/名额/能力 | `pytest test_orchestrator_model.py test_orchestrator_slots.py test_orchestrator_cap.py` | 与下面五文件合计 62 passed |
| U5–U7 工具/流程/重启语义 | `pytest test_orchestrator_flow.py test_orchestrator_tools.py test_orchestrator_model.py test_orchestrator_cap.py test_orchestrator_slots.py` | 62 passed |
| 跨模块回归 | `pytest test_server.py test_session_lifecycle.py test_automations.py test_automations_v2.py test_queue.py test_exec.py test_redo_data.py test_checkpoints.py test_worktree.py test_worktree_contract.py` | 122 passed |
| 全量（项目 .venv） | `./.venv/Scripts/python.exe -m pytest -c ee/pyproject.toml -q` | 1578 passed / 15 skipped / 0 failed（17 分 15 秒） |

## 执行记录（2026-10-28，稳定化 Wave 0–2）

| 单元 | 验证（真实执行） | 结果 |
|---|---|---|
| T3 修复前 | `pytest ... test_worktree_contract.py` | 7 failed / 4 passed（红灯落在被测逻辑） |
| T3 修复后 | 同上 | 11 passed |
| T3 回归 | `test_worktree.py` + `test_worktree_finish_safety.py` + `test_review_worktree.py` | 30 passed |
| T3 回归 | `test_automations.py` + `test_automations_v2.py` | 32 passed |
| T3 前端 | `vitest AgentItems.test.tsx WorktreeDialog.test.tsx` | 4 passed |
| T4 修复前 | `pytest ... test_mcp_isolation.py` | 7 failed / 2 passed |
| T4 修复后 | `test_mcp.py` + `test_mcp_isolation.py` | 18 passed |
| T5 修复前 | `pytest ... test_budget_and_isolation.py` | 7 failed / 3 passed |
| T5 修复后 | 同上 | 10 passed |
| T5 回归 | `test_redo_security_config.py` + `test_style_budget.py` + `test_config_scope.py` | 46 passed / 1 skipped |
| T6 修复前 | `pytest ... test_session_lifecycle.py` | 6 failed |
| T6 修复后 | 同上 | 6 passed |
| T6 回归 | `test_server.py`、`test_server_audit.py`、`test_server_compact.py`、`test_queue.py`、`test_loops.py`、`test_client_lifecycle.py` | 74 passed |
| T6 前端 | `vitest store.test.ts` | 7 passed |
| 类型 | `tsc -p tsconfig.json --noEmit` | exit 0 |

未执行 / 未验证：

- ~~远端 GitHub Actions：新 workflow 的 PR 触发、verify→package 依赖关系未在真实远端跑过~~ → 2026-10-30 仓库公开后恢复 `push`/`PR` 自动触发，后续提交的 verify 均在远端跑绿（见上两节记录）。仍未走过的是 `verify→package` 这一段依赖关系。
- 全量 pytest 已在项目 `.venv` 下执行过一次（1578 passed / 15 skipped / 0 failed）；日常仍按波次跑相关子集。
- ~~Electron 桌面端实际渲染与交互~~ → 桌面端已日常使用（启动、三栏、会话、审批主流程可用）。仍未核对的是逐项视觉细节：间距、字号、动画、弹层方向。
- 真实 MCP 服务器跨项目行为、真实模型评测。
- `ee\scripts\e2e_ws.py` 已接入 CI 并实跑通过（普通 / `--blackhole` / 编排三种场景）。CI 的源码后端那一步原先缺 `working-directory: ee`，从仓库根跑会因命名空间包崩溃，已修。编排闭环已验通：`thread/create_child` → `thread/wait` → 终态 `completed` → `thread/children` 能列出，exit=0。
- 桌面端已接 `thread/status`：`refreshChildStatus` 在列表加载后补拉子会话的 `changed_count` 与终态，U8 卡片的「N 处改动」不再是空的。逐个子会话查、单个失败静默跳过。

延后项（未做，保留原因）：

- 删除“正在运行/等待压缩/等待审批”会话的完整时序测试（当前只覆盖删除后迟到写入）。
- settings 保存与在途请求的竞争场景。
- ~~D6-1 编排关系模型（需与普通 fork 的 `parent_id` 分离）~~ → 已在 D6-1 Wave A / U1–U4 完成（`orchestrator_cap.py`，见上方状态表）。

## 文档用途

- 本页：当前路线、依赖、状态和验证边界。
- `README.md`、`ee\README.md`：使用说明和能力摘要。
- `HANDOFF.md`：历史交付记录；归档段不再是开工指令。
- `research\plan.md`、`design.md`、`d3-plan.md`、`d6-plan.md`、`parity-audit.md`：历史设计/研究，冲突时以当前路线和实际代码为准。
