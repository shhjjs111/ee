# D6 实施手册（给接手的人）

> 写于 2026-10-27 22:50（Asia/Shanghai）。基线：`main 59b11d1`，Windows CI run 40522096425 绿，pytest 1050 passed / 14 skipped，vitest 139。
> 本文件保留 D6 的原始提案，不再是当前操作手册。当前顺序、范围与验证边界见 roadmap-current.md：先完成 Wave 0–2 稳定化，再授权 D6。下方工期、接口和交付清单均需按新方案校准，不能直接恢复本地 WIP。

---

## 历史约定与提案（不作为本批执行指令）

当前实施采用定向 RED→GREEN 与分阶段验证，提交、测试和审查分开登记；不要求每个小改动跑全量，也不自动推送、发布或删除分支。后续编排关系须与普通 fork 的 parent_id 分离；status 不能修改 Git 索引，idle 不等于成功；LSP 需版本化诊断；迁移不能假定影子快照覆盖所有文件。

## 0. 原始开工说明（历史）

### 0.1 项目是什么
- **ee**：个人 AI 编程 Agent。Python 内核（`ee serve`，WebSocket JSON-RPC）+ Electron/React 桌面端。**只要 Windows 桌面端，不做终端版**。
- 只支持 OpenAI Chat Completions 协议，默认 DeepSeek。界面全中文，交流用中文。
- UI 参考 Codex 桌面版三栏布局。

### 0.2 用户定下的硬规则（违反任何一条都要返工）
1. **绝不向用户要 API key**，也不要在代码里写死任何 key。
2. 每完成一项：更新 `HANDOFF.md`（顶部“最后更新/接手第一步” + 对应项打勾写做法与已知限制）、`README.md` 状态、`research/design.md` 状态 → 测试 → 提交 → 推送 `main` → 看 Windows CI。
3. 每个功能先看 OpenCode / Codex / Cline / Aider / Goose / Continue 怎么做（Claude Code / Cursor 只借思路），取长适配；在 HANDOFF 该项写“参考了谁、取了哪点”。**代码和文档里其它地方不写出处，不加第三方声明文件。**
4. 质量优先。先写失败测试，再改代码。
5. `pytest` 不是只读命令，需要用户确认，不能加回只读白名单。
6. `SYSTEM.md` 只替换基础系统段，工具规则继续追加。不要改这个行为。
7. edit 的模糊匹配：宁可报错，也不能误改。
8. 项目层 `.ee/config.toml` 只能收紧安全设置，不能放宽（provider、base URL、key、代理、MCP 命令、hooks、权限放宽、沙箱关闭、联网开启都不允许由项目层设置）。新加的任何配置项都要先想清楚：项目层能不能改？只能往哪个方向改？
9. Windows 相关的行为（进程、路径、编码、沙箱）必须在 Windows CI 上真跑测试，不能在 Linux 上假装通过。
10. 安装包和 Windows 真机测试最后统一做，D6 期间不打包。
11. 没用的功能不要做；参考项目只作参考，不强行照搬。

### 0.3 环境与命令
```bash
git clone https://github.com/shhjjs111/ee.git && cd ee
# 后端
cd ee && pip install -e . pytest && python -m pytest -q        # 期望 ≥1050 passed
# 前端
cd ../desktop && npm ci --ignore-scripts
npx tsc --noEmit -p tsconfig.json
npx tsc --noEmit -p tsconfig.electron.json
npx vitest run           # 机器慢就分片：npx vitest run --shard=1/3（2/3、3/3）
npm run build
# 端到端（假模型，不需要 key）
cd .. && PYTHONPATH=ee python ee/scripts/e2e_ws.py     # 结果应为“通过”
```
- e2e 坑：脚本会临时覆盖 HOME，`openai` 包必须装在**系统** site-packages，否则报 `No module named 'openai'`。
- CI：推 `main` 自动跑 `.github/workflows/desktop-windows.yml`（windows-latest，cancel-in-progress）。连续推送前先等上一轮结果。查看：GitHub → Actions，或 `gh run list --branch main -L 3`。
- 提交身份：`shhjjs <shhjjs111@users.noreply.github.com>`。提交信息用中文，格式参考 `git log`。

### 0.4 代码地图（D6 会碰到的）
| 位置 | 作用 |
|---|---|
| `ee/ee/server/app.py` | 所有 JSON-RPC 方法（`thread_start`、`turn_start`、`approval_respond`、`settings_set`…）。方法名 `thread_list` 对应 RPC `thread/list`。 |
| `ee/ee/server/threads.py` | 单个会话（Thread）：`meta`（含 `parent_id`、`source`）、`summary()`、`reload_config()`、`notify("item/started"…)`、`notify("thread/updated"…)`。 |
| `ee/ee/core/session.py` | 内核回合循环；工具调用时 `extra={"session":…, "emit_event":…, "call_id":…, "config":…}` 传给工具。 |
| `ee/ee/core/events.py` | 内核事件：`ApprovalRequest`（审批，future 等待结果）、`SubagentStart` 等。 |
| `ee/ee/core/subagent.py` + `ee/ee/tools/agent.py` | 现有 `task` 子代理工具（同进程、可开 worktree、进度挂在 task 卡片上）。**指挥会话要复用这里的思路，但子会话是真正的独立 Thread。** |
| `ee/ee/core/worktree.py` | worktree 创建 / 列表 / diff / merge / remove（锁定拒删、删前备份忽略文件）。 |
| `ee/ee/tools/__init__.py` | `build_tools(profile, config, agent)` 决定工具列表。新工具在这里注册。 |
| `ee/ee/tools/base.py` | `Tool` 基类：`name`、`read_only`、`parameters`、`summary()`、`async run(args, ctx)`，出错抛 `ToolError`。 |
| `ee/ee/core/permissions.py` | 权限判定；新工具必须定义它是否需要审批。 |
| `ee/ee/server/settings.py` | 设置读写；`_xxx_out()` 输出、`set_settings` 合并写回（只写允许的键）。 |
| `ee/ee/config.py` | 配置加载与项目层收紧规则。 |
| `desktop/src/lib/store.ts` | Zustand 全局状态（threads、details、rpc）。 |
| `desktop/src/lib/reduce.ts` | 把 `item/*`、`thread/*` 通知归约进状态。 |
| `desktop/src/components/Sidebar.tsx` | 左栏会话列表。 |
| `desktop/src/components/AgentItems.tsx` | 子代理卡片渲染（可借鉴给指挥会话的子会话卡片）。 |
| `desktop/src/components/AgentSettings.tsx` | 「工具与记忆」设置页，新开关加在这里（参考“命令沙箱”Card）。 |
| `desktop/src/types.ts` | 前后端共享类型（手写，改后端输出必须同步改这里）。 |
| `desktop/src/**/*.test.tsx` | vitest + jsdom；参考 `AgentSettings.test.tsx` 的 `mount()` / `switchAfter()` 写法。 |
| 前端 RPC 契约扫描测试 | 前端调用的每个 RPC 方法后端必须存在，新加 RPC 时这个测试会提醒你。 |

### 0.5 每一项的标准流程（照做）
1. 新建分支 `feat/d6-N-名字`（多人并行时每人一个分支，各自 clone）。
2. 读 HANDOFF 对应条目 + 本手册对应章节。
3. 看参考实现（第 0.2 条第 3 点），写 3–5 行“取了哪点”的草稿。
4. **先写失败测试**（后端 pytest / 前端 vitest），跑一遍确认失败。
5. 实现，直到测试通过；再跑**全量**后端 + 前端 + 两份 tsc + build + e2e。
6. 改 HANDOFF / README / research/design.md。
7. 推分支，确认 CI 绿后合并 `main`（或交给维护者合并），删远端分支。

---

## D6 总览与顺序

已完成、**不要重做**：GBK 解码回退（Electron 流式 UTF-8 失败回退 GBK，已在 main）、Ctrl/⌘+K 搜索、Windows 命令沙箱（D5-8）。

| 顺序 | 项 | 优先级 | 估计 |
|---|---|---|---|
| 1 | D6-1 指挥会话 ★ | 最高 | 3–5 天 |
| 2 | D6-2 LSP 诊断回灌 + LSP 工具 ★ | 最高 | 3–4 天 |
| 3 | D6-3 best-of-n、本地 ↔ worktree 交接 | 中 | 2 天 |
| 4 | D6-6 命令面板/快捷键、活动视图、归档置顶、导出 | 中 | 2–3 天 |
| 5 | D6-4 Goal 模式、/side 侧聊 | 中 | 1–2 天 |
| 6 | D6-5 自动审批审查员 | 中 | 1–2 天 |
| 7 | D6-8 PAC 代理、PR 工作流、lint/test 循环、自动更新 | 低 | 3 天 |
| 8 | D6-7 自动记忆提取 | **必须先问用户同意** | 1 天 |

后置不做：插件包、浏览器、语音、自定义工具、WSL、云任务、向量索引。

依赖关系：D6-3 best-of-n 依赖 D6-1 的子会话机制；D6-5 审查员可独立做；D6-1 和 D6-2 互不依赖，可两人并行。

---

## D6-1 ★ 指挥会话（Orchestrator）

### 目标
一个“指挥”会话可以创建、驱动、监控多个**真正的子会话**（每个都是左栏里能点开的完整 Thread），子会话可以各自在独立 worktree/分支上写代码。用户在指挥会话里能看到每个子会话的进度卡片，子会话需要审批时冒泡到指挥会话和系统通知。

和现有 `task` 子代理的区别：`task` 是父回合内同步跑完的一次性子代理，结果作为工具输出返回；指挥会话的子会话是**持久、异步、可被用户直接接管**的独立会话。

### 参考
- Codex：多任务并行，每个任务独立 worktree，任务卡片显示状态与 diff。
- Goose：子会话/子 agent 的派发与结果汇总。
- OpenCode：会话父子关系（parentID）与 session 树。
- Claude Code：只借“后台任务 + 等待”思路。
写进 HANDOFF 时：取了“独立 worktree + 状态卡片”“父子关系树”“wait 阻塞直到完成”。

### 1. 数据模型（后端）
`threads.py` 的 `meta` 已有 `parent_id`、`source`。补充：
- `meta["parent_id"]`：指挥会话的 thread id。
- `meta["source"] = "orchestrator"`。
- `meta["role"] = "orchestrator" | "child" | None`（指挥会话本身第一次调用 thread_create 时标记为 orchestrator）。
- `meta["worktree"]`：若自动建了 worktree，记录 `{path, branch, base}`。
- `summary()` 把 `role`、`parent_id`、`worktree` 输出给前端（同步改 `desktop/src/types.ts` 的 thread 摘要类型）。
- 持久化：meta 已经随会话存盘的话直接沿用；重启后父子关系必须还原（写测试：建子会话 → 模拟重启加载 → `parent_id` 仍在）。

### 2. 五个工具（新文件 `ee/ee/tools/orchestrate.py`）
工具需要访问“会话管理器”（app 里管理所有 Thread 的对象）。做法：在 `app.py` 创建 session 时，把一个**窄接口**对象放进工具上下文 `extra["orchestrator"]`，不要把整个 app 传进去。接口定义在 `ee/ee/core/orchestrator.py`：

```python
class OrchestratorAPI(Protocol):
    def list_children(self, parent_id: str) -> list[dict]: ...
    async def create_child(self, parent_id: str, *, title: str, prompt: str | None,
                           worktree: bool, branch: str | None, base: str | None,
                           model: str | None, mode: str | None) -> dict: ...
    async def send(self, parent_id: str, child_id: str, text: str) -> dict: ...
    def status(self, parent_id: str, child_id: str) -> dict: ...
    async def wait(self, parent_id: str, child_ids: list[str], timeout: float) -> dict: ...
```
由 `app.py` 实现（内部调用已有的 `thread_start`、`turn_start`、`thread_get`）。

| 工具 | 参数 | 行为 | read_only | 需审批 |
|---|---|---|---|---|
| `threads_list` | 无 | 列出本指挥会话的子会话：id、标题、状态（idle/running/waiting_approval/error）、worktree 分支、最后一条助手消息摘要（≤300 字） | 是 | 否 |
| `thread_create` | `title`（必填）、`prompt`（可选，给了就立即开第一回合）、`worktree`（bool，默认 true）、`branch`（可选，不给自动生成 `ee/<slug>`）、`base`（可选，默认当前 HEAD）、`model`、`mode` | 调 `worktree.create()` → `thread_start(cwd=worktree路径)` → 设 parent_id → 若有 prompt 调 `turn_start`。返回子会话 id 和 worktree 路径 | 否 | 是（首次；“以后都行”记住工具名） |
| `thread_send` | `thread_id`、`text` | 子会话空闲：开新回合；正在跑：进入其排队队列（复用现有队列机制 `core/queue.py`）。返回“已发送/已排队” | 否 | 否（创建时已批准） |
| `thread_status` | `thread_id` | 状态、当前 todo、改动文件数（`worktree.diff()` 统计）、最后助手消息、累计 token/费用 | 是 | 否 |
| `thread_wait` | `thread_ids`（数组）、`timeout`（秒，默认 600，上限 3600） | 阻塞直到全部子会话进入 idle 或 error，或超时；返回每个的最终状态和最后助手消息。**必须可被用户中断**（指挥会话 interrupt 时立即返回，不能卡死） | 是 | 否 |

安全与限制（每条都要有测试）：
- 只能操作 `parent_id == 当前会话` 的子会话，否则 `ToolError("不是本会话创建的子会话")`。
- 子会话**不能再创建子会话**（禁止递归）：子会话的工具列表里不出现 `thread_create`。
- 并发上限：设置 `[orchestrator] max_children = 4`（同时 running 的数量），超出时 `thread_create` 报错并说明上限。项目层只能调小。
- 子会话权限模式不能比父会话宽：父是“需审批”，子不能是“全自动”。复用 `subagent.access()` 的思路。
- 沙箱：父会话开了沙箱，子会话继承（子会话 cwd 是 worktree，沙箱可写范围自然就是 worktree）。
- 工具描述（中文）要写清何时用：“需要并行做多件独立的写代码任务时用；简单查询用 task 子代理”。注意 full/lean 提示词预算：lean 模式**不提供**这五个工具；full 模式默认也不提供，只有开关 `orchestrator.enabled=true` 或用户在会话里切到“指挥模式”时才加入（避免每个会话都多出五个工具的固定 token 开销，也避免破坏前缀缓存——只对新对话生效）。

### 3. 审批冒泡
- 子会话产生 `ApprovalRequest` 时，`app.py` 除了照常发给子会话，还要：
  1. 给父会话发通知 `thread/child_approval`：`{parent_id, child_id, call_id, title, detail}`；
  2. 若用户启用了通知，走现有系统通知（`desktop/src/lib/notify.ts`），标题“子会话「xx」等待审批”。
- 前端：父会话里该子会话的进度卡片出现“允许 / 拒绝 / 打开子会话”按钮。点“允许/拒绝”直接调用已有 `approval/respond`，参数里的 `thread_id` 是**子会话 id**。
- 测试：子会话审批 → 父会话收到通知 → 从父会话响应 → 子会话继续；响应后父卡片上的按钮消失；重复响应返回明确错误不崩溃。

### 4. 前端
1. **左栏树形**（`Sidebar.tsx`）：有 `parent_id` 的会话缩进显示在父会话下，可折叠；父会话折叠时显示“3 个子会话，1 个运行中”。子会话的父会话被删除时，子会话提升为顶层（不要级联删除，worktree 有未合并改动的情况很常见）。
2. **进度卡片**（新组件 `ChildThreadCard.tsx`，参考 `AgentItems.tsx`）：在父会话里，每个 `thread_create` 工具调用渲染为卡片：标题、状态点（运行中动画/空闲/等待审批/出错）、分支名、改动文件数、最后一条助手消息前两行、按钮“打开”“查看 diff”（复用 `WorktreeDialog`）“合并到当前分支”（调用已有 worktree merge RPC，需二次确认）。
3. 卡片状态来源：订阅 `thread/updated`（子会话的）+ `thread/child_approval`，在 `reduce.ts` 里按 `parent_id` 归档。
4. 类型同步 `types.ts`；RPC 契约扫描测试会检查新 RPC 是否在后端存在。

### 5. 测试清单
后端（`ee/tests/test_orchestrator.py`，用假模型/假 provider，参考现有 subagent 测试）：
- 创建子会话：建了 worktree、parent_id 正确、prompt 触发了第一回合。
- worktree=false：cwd 为父会话 cwd。
- 越权操作他人子会话报错；子会话中没有 thread_create。
- 超并发上限报错。
- thread_send 空闲/忙时两种分支。
- thread_wait：全部完成返回；超时返回部分结果并标注超时；父会话 interrupt 时立刻返回。
- 审批冒泡与从父会话响应。
- 重启后父子关系还原。
- 项目层不能调大 max_children、不能开启 orchestrator（只能关）。
前端：
- Sidebar 树形缩进与折叠；父删除后子提升。
- ChildThreadCard 各状态渲染；审批按钮调用 `approval/respond` 且 thread_id 为子会话。
- e2e：在 `e2e_ws.py` 加一段：假模型调用 thread_create → thread_wait → 校验返回。

### 6. 已知限制（写进 HANDOFF）
- 子会话之间不共享上下文，只能通过指挥会话转述。
- 合并冲突不自动解决，只提示。
- 子会话费用单独计，父会话卡片显示汇总。

---

## D6-2 ★ LSP 诊断回灌 + LSP 工具

### 目标
模型改完文件后自动拿到语言服务器诊断（类型错误、未定义名字），作为工具结果的附加信息回灌，模型下一步就能修。另提供一个 `lsp` 工具查定义、引用、悬停信息。先支持 Python（pyright）和 TypeScript/JavaScript（typescript-language-server）。

### 参考
- OpenCode：编辑后触发 LSP 诊断并把错误追加到工具输出（重点参考）。
- Cline / Continue：只借诊断展示思路。
- Aider：lint 后自动修的循环（与 D6-8 lint/test 循环呼应）。

### 1. 架构
新目录 `ee/ee/core/lsp/`：
- `client.py`：最小 LSP JSON-RPC 客户端（stdio，`Content-Length` 头分帧）。用 `asyncio.create_subprocess_exec`；**Windows 下用 `.cmd` 启动器要走 `shutil.which` 解析完整路径**，不要 `shell=True`。编码一律 UTF-8 字节读写（不要用文本模式，Windows 默认 GBK 会坏）。
- `manager.py`：按 (工作区根, 语言) 懒启动、复用服务器；空闲 10 分钟关闭；会话结束/服务退出时关闭全部（注意 Windows 进程树：参考沙箱/bgproc 里的 Job Object 或进程树清理，确保 node 子进程不残留）。
- `servers.py`：服务器定义表：
  ```python
  SERVERS = {
    "python": {"exts": [".py", ".pyi"], "cmd": ["pyright-langserver", "--stdio"], "roots": ["pyproject.toml", "setup.py", "pyrightconfig.json", ".git"]},
    "typescript": {"exts": [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"], "cmd": ["typescript-language-server", "--stdio"], "roots": ["tsconfig.json", "jsconfig.json", "package.json", ".git"]},
  }
  ```
  **不自动安装**服务器。找不到命令时：设置页显示“未安装”，并给出安装命令（`npm i -g pyright typescript-language-server typescript`）；工具层静默跳过诊断（不报错、不阻塞编辑）。
- 协议流程：`initialize`（rootUri、capabilities 里声明 `textDocument.publishDiagnostics`）→ `initialized` → 文件变更时 `didOpen`（第一次）/`didChange`（全量文本同步即可）→ 等 `textDocument/publishDiagnostics` 推送。

### 2. 诊断回灌
- 挂点：`write`、`edit`、`apply_patch` 三个工具成功之后（在工具结果生成处，或在 session 工具调用后统一处理——推荐统一处理，避免三处重复）。
- 等待策略：发出 didChange 后最多等 **1.5 秒**（可配 `lsp.wait_ms`）拿该文件的诊断；超时就不附加（不能拖慢每次编辑太多）。首次启动服务器可能要几秒，首轮超时是正常的，后续会快。
- 只附加 **error** 级（可配包含 warning），每个文件最多 **20 条**，超出写“另有 N 条”。格式：
  ```
  
  [诊断] src/app.ts
  - 12:5 错误 TS2304: 找不到名称“foo”
  ```
- **只报告本次编辑引入或仍存在的、属于被编辑文件的诊断**；其它文件的诊断不附加（避免噪声）。可选：对比编辑前后，只标“新增”。
- 脱敏/截断：附加内容计入工具输出截断预算（`tools/truncate.py`）。
- 前缀缓存：诊断是工具结果的一部分，不影响系统提示词，安全。

### 3. `lsp` 工具
- 参数：`action`（`definition` | `references` | `hover` | `diagnostics` | `symbols`）、`path`、`line`、`column`（1 起始，内部转 0 起始）。
- 返回：位置列表（相对路径:行:列 + 该行源码），最多 50 条。
- read_only = True，无需审批。
- 仅 full 模式且 `lsp.enabled` 时提供；lean 模式不提供。

### 4. 配置与设置页
- `[lsp] enabled = true`（服务器装了才会实际启动）、`diagnostics = true`、`wait_ms = 1500`、`include_warnings = false`、`servers = {}`（允许用户覆盖命令路径）。
- **项目层**：只能关闭（enabled/diagnostics=false），**不能**设置 `servers` 命令（否则等于允许仓库执行任意命令，和 MCP 命令同等级风险）。写测试。
- 设置页「工具与记忆」加 “语言服务器（LSP）” Card：两个开关 + 每种语言一行状态（已找到路径 / 未安装 + 安装命令）。后端加 RPC `lsp/status` 返回 `{python: {found, path}, typescript: {...}}`。

### 5. 测试
- 单元：分帧编解码（含中文、多字节、半包）；manager 懒启动与复用；找不到命令静默跳过。
- 集成：用一个 **假 LSP 服务器脚本**（Python 写，读 stdin 回固定诊断）测完整流程，CI 不依赖真实 pyright。
- Windows CI：在 workflow 里 `npm i -g pyright` 后跑一个真实 pyright 的集成测试（标记 `@pytest.mark.skipif(not shutil.which("pyright-langserver"))`），验证 Windows 路径、`.cmd` 启动、进程清理。
- 前端：设置页 Card 渲染与开关写回。

### 6. 已知限制
- 只做两种语言；其它语言用户可在用户层 `servers` 自行配置。
- 大仓库首次索引慢，前几次编辑可能拿不到诊断。

---

## D6-3 best-of-n、本地 ↔ worktree 交接

### best-of-n
- 用户在输入框选“并行尝试 N 份”（N=2..4），同一提示词在 N 个 worktree 子会话里各跑一份（复用 D6-1 的 `create_child`，parent 为当前会话）。
- 全部完成后，在当前会话显示对比面板：每份的改动文件数、diff 预览、测试结果（若跑过）、费用；用户选一份“采用” → merge 到当前分支，其余 worktree 询问是否删除（默认保留，删前走现有备份逻辑）。
- 参考 Codex 的多次尝试选择。
- 测试：N 个子会话创建、全部完成后面板数据正确、采用一份后合并且其余状态正确。

### 本地 ↔ worktree 交接
- “移到 worktree”：当前会话在本地有未提交改动 → 新建 worktree（基于 HEAD）→ 把改动用 `git stash` 或 `git diff | git apply` 迁移过去 → 会话 cwd 切换到 worktree（会话 meta 更新，后续回合在新 cwd）→ 本地还原干净。迁移失败必须完整回滚，本地改动不能丢（先建快照，复用 `core/snapshot.py`）。
- “移回本地”：worktree 的改动 merge/apply 回本地分支 → cwd 切回本地。冲突时停止并报告，不强制。
- 前端：会话标题栏菜单两项；需二次确认。
- 测试：有未跟踪文件、二进制文件、冲突三种情况。

---

## D6-4 Goal 模式、/side 侧聊

### Goal 模式
- 用户给一个目标 + 完成判据（例如“`pytest` 全过”或一条命令退出码为 0），Agent 自主多回合推进，每回合结束自动检查判据，不满足就继续，直到满足 / 达到最大回合数（默认 10）/ 费用上限 / 用户中断。
- 已有 `/loop`（`core/loopspec.py`、`server/loops.py`、`LoopTray.tsx`）——**先读它，Goal 模式应在其上扩展，不要另起一套**。区别：判据是可执行命令，失败输出回灌给下一回合。
- 判据命令执行同样走权限审批和沙箱。
- 测试：判据第 3 回合满足后停止；达到上限停止并报告；中断立即停。

### /side 侧聊
- 在当前会话旁开一个临时侧聊：继承当前会话历史（只读副本）问问题，**不写回主会话历史、不影响主会话前缀缓存**。
- 实现：复用分支会话（`server/branch.py`）创建一个 `source="side"` 的临时会话，工具限制为只读；右侧面板显示；关闭即丢弃（可选“保存为新会话”）。
- 测试：侧聊不改主会话 history 文件字节；侧聊无写工具。

---

## D6-5 自动审批审查员

- 在“需审批”的命令/编辑到达用户之前，先让一个便宜模型（`[models.roles]` 里配置的角色模型，默认同 DeepSeek 低档位/关闭思考）判断风险：返回 `{"verdict": "allow"|"ask"|"deny", "reason": "..."}`。
- 规则：只有 `allow` 且命令不在危险清单内才自动放行；`deny` 不直接拒绝，而是**升级为询问用户**并显示理由（审查员不能替用户拒绝，只能帮用户少点几次）；任何异常/超时（默认 8 秒）都回退为询问用户。
- 危险命令清单（`permissions.py` 已有归一化逻辑）永远不交给审查员放行。
- 开关 `[permissions] reviewer = false`（默认关）；项目层只能关。设置页加开关并说明会产生额外费用。
- 审查员的判定和理由记入该工具卡片（“已由审查员放行：理由…”），可审计。
- 参考 Codex 的 auto-approve 思路、Goose 的权限模式。
- 测试：allow 放行、deny 升级询问、超时询问、危险命令不送审、项目层不能开启。

---

## D6-6 命令面板 / 快捷键自定义、活动视图、会话归档置顶、导出

1. **命令面板**（Ctrl/⌘+Shift+P）：模糊搜索所有动作（新会话、切换模型、切换模式、打开设置各页、切换主题、压缩上下文、撤销…）。动作注册表放 `desktop/src/lib/commands.ts`，每个动作 `{id, title, keywords, run, shortcut?}`；现有快捷键迁移到注册表。
2. **快捷键自定义**：设置页列出所有动作与快捷键，点击录制新组合；冲突检测；存桌面设置（已有原子写入）。
3. **活动视图**：一个页面列出所有会话正在运行/等待审批/最近完成的任务（跨工作区），点击跳转。数据来自 `thread/list` + 状态。
4. **归档 / 置顶**：thread meta 加 `pinned`、`archived`；左栏置顶在上、归档默认隐藏（底部“已归档 N”展开）；RPC `thread/set_flags`。搜索结果包含归档会话并标注。
5. **导出**：会话导出 Markdown（用户消息、助手消息、工具调用摘要，折叠工具输出）和 JSON（原始 items）。走 Electron 保存对话框；导出内容复用保存时的脱敏逻辑，**不能导出 API key**。
- 测试：命令注册表搜索、快捷键冲突、置顶排序、归档过滤、导出脱敏。

---

## D6-7 自动记忆提取（**先问用户**）

- 开工前必须得到用户明确同意（HANDOFF 写着“待用户确认”）。
- 方案：会话结束（或压缩时）用便宜模型从对话里提取“用户偏好/项目约定”候选，**不直接写入**，在界面以“建议记住”列表展示，用户勾选后写入现有记忆（`core/memory.py`，user 或 workspace 范围）。
- 默认关闭。不提取任何密钥、路径中的用户名以外的个人信息。

---

## D6-8 PAC 代理、PR 工作流、lint/test 循环、自动更新

（GBK 解码回退已完成，跳过。）

1. **PAC 代理**：网络设置支持 PAC URL；用 `pypac` 或自写解析（注意 PAC 是 JS，需要 JS 引擎——优先评估：Windows 上可直接用 WinHTTP 的系统 PAC 解析 `WinHttpGetProxyForUrl`，通过 ctypes 调用，避免引入 JS 引擎）。“跟随系统”模式下自动使用系统 PAC。Windows CI 测试用本地 PAC 文件。
2. **PR 工作流**：在 worktree/分支会话里一键“创建 PR”：生成标题/描述（模型）→ `git push` → 用 `gh pr create`（若安装且已登录），否则给出网页链接。**不替用户存 GitHub token**；检测不到 gh 就提示安装。
3. **lint/test 循环**：配置 `[checks] lint = "ruff check ."`, `test = "pytest -q"`；编辑回合结束后可选自动运行（默认关），失败输出回灌，最多 N 轮。命令执行走审批（pytest 需确认的规则不变）和沙箱。与 Goal 模式共享判据执行代码。
4. **自动更新**：`electron-updater` + GitHub Releases（仓库是私有的，私有仓库更新需要 token，**这一点要先问用户**是否改用公开发布渠道）。安装包阶段才真正启用，现在只做代码与开关，默认关。

---

## 交付检查表（每一项合并前逐条打勾）
- [ ] 先写了失败测试，现在全部通过
- [ ] 后端全量 pytest 通过（数量 ≥ 之前）
- [ ] 两份 tsc、vitest 全量、`npm run build` 通过
- [ ] `PYTHONPATH=ee python ee/scripts/e2e_ws.py` 输出“通过”
- [ ] Windows CI 绿（涉及进程/路径/编码的必须有 Windows 实测用例）
- [ ] 新配置项：已确认项目层只能收紧，并有测试
- [ ] 新工具：已决定 read_only / 是否审批 / lean 模式是否提供 / 是否只对新对话生效
- [ ] 没有向用户要 key，没有写死 key，导出/日志不泄露 key
- [ ] HANDOFF（顶部 + 条目：做法、参考了谁取了哪点、已知限制）、README、research/design.md 已更新
- [ ] 代码里没有写参考项目出处，没有加第三方声明文件
- [ ] 远端临时分支已删除

## 遗留问题（D6 期间顺手评估）
- `apply_patch` 的危险 strip 匹配已经在 `3efbbe3` 修复，已有 `ee\tests\test_apply_patch_safety.py`；不再列为未查。其它编辑安全改动仍须按各自回归用例验证。
- 分支会话（branched sessions）的冻结前缀继承。
- 保存时脱敏的历史消息，重启后无法完全字节一致（影响缓存命中，不影响正确性）。
- `~/.claude/skills` 加载开关目前只有配置项，设置页没开关。
- 真实 DeepSeek 对比评测由用户用自己的 key 运行：`cd ee && python scripts/eval_profiles.py --preset a2 --runs 3`、`--preset a3 --runs 3`、`--runs 3`。
