# ee 当前路线与实施状态

更新：2026-10-28。产品定位：Windows 桌面 AI 编程 Agent；Python 内核 + Electron/React，只支持 OpenAI Chat Completions 协议，不恢复 TUI。

## 当前执行范围

本批已执行稳定化 Wave 0–2（T1–T6），并已完成 D6-1 指挥会话的 Wave A/B（U1–U7）。D6-1 的 Wave C（U8 桌面端接管、U9 端到端）与 D6-2（LSP）尚未开始。完整方案见 plans/2026-10-28-stabilization-and-d6.md 与 plans/2026-10-28-d6-1-orchestrator-tasks.md；本页是当前入口与验证记录。

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
| 后续 D6-1 Wave C / U8–U9 | 桌面端左栏树形与子会话卡片、假模型端到端 | 未开始，任务书见 plans | `desktop\src\components\Sidebar.tsx`、新增 `ChildThreadCard.tsx`；`ee\scripts\e2e_ws.py` |
| 后续 D6-2 | 版本化 LSP 诊断、可信启动与进程回收 | 未开始 | 先 Python 与 TypeScript/JavaScript，不自动安装服务器 |
| 后置 | best-of-n、文件交接、Goal、自动审批、发布 | 未开始，分别立项 | 自动记忆、真实模型费用、公开更新渠道需单独确认 |

## 已有能力，不重复建设

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
- `ee\scripts\e2e_ws.py` 尚未接入 CI，也没有「创建子会话 → wait → 校验终态」的编排场景（D6-1 U9 待做）。

延后项（未做，保留原因）：

- 删除“正在运行/等待压缩/等待审批”会话的完整时序测试（当前只覆盖删除后迟到写入）。
- settings 保存与在途请求的竞争场景。
- D6-1 编排关系模型（需与普通 fork 的 `parent_id` 分离）。

## 文档用途

- 本页：当前路线、依赖、状态和验证边界。
- `README.md`、`ee\README.md`：使用说明和能力摘要。
- `HANDOFF.md`：历史交付记录；归档段不再是开工指令。
- `research\plan.md`、`design.md`、`d3-plan.md`、`d6-plan.md`、`parity-audit.md`：历史设计/研究，冲突时以当前路线和实际代码为准。
