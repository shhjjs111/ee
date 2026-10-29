# ee 当前路线与实施状态

更新：2026-10-29。产品定位：Windows 桌面 AI 编程 Agent；Python 内核 + Electron/React，只支持 OpenAI Chat Completions 协议，不恢复 TUI。

## 当前执行范围

本批已执行稳定化 Wave 0–2（T1–T6），已完成 D6-1 指挥会话的全部三个 Wave（U1–U9），并已完成 D6-2（LSP 诊断）的 U1–U6。完整方案见 plans/2026-10-28-stabilization-and-d6.md、plans/2026-10-28-d6-1-orchestrator-tasks.md、plans/2026-10-29-d6-2-lsp-tasks.md；本页是当前入口与验证记录。

| 阶段 | 工作 | 当前状态 | 生产入口 / 验证入口 |
|---|---|---|---|
| Wave 0 / T1 | 现行路线与历史设计分离 | 已完成（纯文档） | 本页、README、HANDOFF |
| Wave 0 / T2 | PR/main 验证与显式发布分离，补 Vitest | 已完成（本地配置；远端触发未验证） | `.github\workflows\desktop-windows.yml`；`desktop\package.json` |
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
| T10-d / best-of-n | 候选评选，胜者只由判据决定 | **已完成并验证**（评选层 + RPC + 桌面端展示）；**界面跑不出真实结论**：候选不带 verdict，判据编排未做 | `ee\ee\core\bestof.py`（复用 `goal.Verdict`）、`server\bestof_rpc.py` 的 `bestof/pick`、`desktop\src\components\BestofDialog.tsx`；`test_bestof.py`（14）、`test_bestof_rpc.py`（4）、`BestofDialog.test.tsx`（5） |
| T10-e / 文件交接 | 迁移清单 + 逐字节核对 | **已完成并验证**（清单层 + RPC + 桌面端入口）；复制/切换/清理需用户确认，未做 | `ee\ee\core\handoff.py`、`server\handoff_rpc.py` 的 `handoff/manifest\|verify`、`desktop\src\components\HandoffDialog.tsx`（工作区菜单「交接清单…」）；`test_handoff.py`（12）、`test_handoff_rpc.py`（8）、`HandoffDialog.test.tsx`（5） |
| 后置 | 发布 | 未开始，分别立项 | 自动记忆、真实模型费用、公开更新渠道需单独确认 |

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

- **桌面端从未启动过**（`cd desktop && npm run dev` 没跑过）：间距、字号、弹出方向、动画、`+` 菜单的 `side="top"` 实际弹出位置全部未验。所有前端断言都停留在 jsdom 的 DOM 结构层。
- **`GoalMenu` 键盘导航未测**：↑↓ 选择、Esc 关闭只有实现没有断言。
- **best-of-n 端到端不可用**：见上节「判据编排未做」。

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
- best-of-n 的**判据编排未做**：界面把 worktree 列表当候选提交，不带 `Verdict`，所以一律判未验证、永远「不选」。要端到端可用需先定判据命令来源与时间预算。
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

- 导出与 Goal 的**桌面端入口**未做（导出只有 CLI，Goal 只有 `ee exec --goal`）。用户明确要求先不做桌面端。
- Goal 判据的测试计数解析只保证 pytest；vitest / go test 解析不到会按 0 处理（保守方向：
  表现为跑满轮次后 exhausted，不会假成功）。
- `ee export` 的 Electron 保存对话框路径未验（本机未启动过桌面端）。
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
- **编辑器里的实际观感未验**：桌面端没启动过，工具输出的可见性只验到字符串层面。
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

- 远端 GitHub Actions：新 workflow 的 PR 触发、verify→package 依赖关系未在真实远端跑过；本地无法验证。
- 全量 pytest 已在项目 `.venv` 下执行过一次（1578 passed / 15 skipped / 0 failed）；日常仍按波次跑相关子集。
- Electron 桌面端实际渲染与交互。
- 真实 MCP 服务器跨项目行为、真实模型评测。
- `ee\scripts\e2e_ws.py` 已接入 CI 并实跑通过（普通 / `--blackhole` / 编排三种场景）。CI 的源码后端那一步原先缺 `working-directory: ee`，从仓库根跑会因命名空间包崩溃，已修。编排闭环已验通：`thread/create_child` → `thread/wait` → 终态 `completed` → `thread/children` 能列出，exit=0。
- 桌面端已接 `thread/status`：`refreshChildStatus` 在列表加载后补拉子会话的 `changed_count` 与终态，U8 卡片的「N 处改动」不再是空的。逐个子会话查、单个失败静默跳过。

延后项（未做，保留原因）：

- 删除“正在运行/等待压缩/等待审批”会话的完整时序测试（当前只覆盖删除后迟到写入）。
- settings 保存与在途请求的竞争场景。
- D6-1 编排关系模型（需与普通 fork 的 `parent_id` 分离）。

## 文档用途

- 本页：当前路线、依赖、状态和验证边界。
- `README.md`、`ee\README.md`：使用说明和能力摘要。
- `HANDOFF.md`：历史交付记录；归档段不再是开工指令。
- `research\plan.md`、`design.md`、`d3-plan.md`、`d6-plan.md`、`parity-audit.md`：历史设计/研究，冲突时以当前路线和实际代码为准。
