# 交接：稳定性修复 + 类型清理（2026-11-11）

状态：**已完成并验证**。代码提交 `9138a3e`（58 files, +1157 −152）。
这份文档代替"上一轮对话的记忆"：新会话先读它，再 `git show 9138a3e` 看细节。

## 本轮做了什么

| # | 问题 | 修法 | 入口 |
|---|---|---|---|
| 1 | 自动化每次运行往 `backend.threads` 塞一个会话，永不释放 | `_execute` 的 `finally` 里摘掉 + `aclose`；取消/超时路径等 task 结束再关 | `server/automations.py` |
| 2 | 删除正在跑的会话后，`_finish`/迟到事件广播 `thread/updated`，左栏复活 | 后端 `deleted` 门控（`_finish`/`_on_event`/`_ai_title`）+ 前端删除墓碑 | `server/threads.py`、`desktop/src/lib/store.ts` |
| 3 | handler 内部 TypeError 被伪装成"参数错误"；慢客户端响应被静默丢弃 | `inspect.signature().bind()` 区分签名错误；`_put_response` 满了断连；`_spawn` 存强引用 | `server/app.py` |
| 4 | `thread/list` 对每个会话 `load()` 全量解析 | `SessionStore.summary` 轻量扫描（只扫 meta/时间戳/用户条目） | `core/store.py` |
| 5 | LSP 并发 acquire 会重复启动进程（旧锁随 Entry 被替换） | 锁改 per-key 并在锁内重读池；文档改成"每会话一个 manager" | `core/lsp/manager.py` |
| 6 | `restart()` 只关 stdin 就起新后端，短暂双后端 | `await stopAndWait(...)` 后再 `start()` | `desktop/electron/backend.ts` |
| 7 | `backend.threads` 无上限（大会话 history/items 常驻） | 上限 40 + 空闲 60s 淘汰 + `aclose`；`_athread`/`thread_start`/5 分钟 GC 触发 | `server/app.py`、`threads.py`、`loops.py` |
| 8 | pyright 117 条存量诊断 + 配置缺失 | `pyrightconfig.json` + 全量清零（0 error / 0 warning） | 仓库根、`ee/docs/pyright.md` |
| 9 | formatter 把整仓排版打散（diff +4762） | 恢复手写排版，只保留语义 diff；`.pi-lens.json` 禁用 format/autofix | 仓库根 |

## 关键决策（不要无意推翻）

- **`.pi-lens.json` 是有意加的**：本仓库的排版是手写的（`ee/pyproject.toml` 写明不做风格强制）。
  删掉它，pi-lens 会在每个 turn 结束时重新格式化你改过的文件。
- **`pyrightconfig.json` 只查 `ee/ee`**：`ee/tests`/`ee/scripts` 是测试与排障脚本，噪声大。
  口径与理由见 `ee/docs/pyright.md`；当前没有 severity override，0/0 是基线。
- **会话缓存淘汰**：`MAX_LOADED_THREADS=40`、`THREAD_EVICT_MIN_IDLE=60s`，
  按 `Thread.last_access` 判空闲（不是 `meta.updated`，否则刚打开的老会话会被立刻踢）。
  淘汰会 `aclose`；`/loop`、运行中、压缩中、等审批的会话不淘汰。
- **删除会话是双保险**：后端门控 + 前端墓碑。只改一边，另一边仍可能复活。
- **已知并行 flake**：`test_robustness::test_http_status_error_message` 在 `-n 8` 下
  偶发失败（本地假服务器争用），单独跑通过。不是本轮引入。

## 验证命令与结果

```bash
./.venv/Scripts/python.exe -m ruff check ee/                       # All checks passed
pyright                                                            # 0 error / 0 warning
PYTHONUTF8=1 ./.venv/Scripts/python.exe -m pytest -c ee/pyproject.toml -q -n 8
# 2516 passed / 16 skipped / 0 failed（134s）
cd desktop && npx vitest run                                       # 65 files / 394 passed
cd desktop && npm run typecheck                                    # exit 0
```

> Windows 上路径带中文时 `PYTHONUTF8=1` 必须加，否则 xdist worker 启动即崩。

## 未完成 backlog（按优先级）

1. ~~`store.append` 写盘失败仍只写日志，界面无感知（磁盘满时对话看似正常）。~~
   → **2026-11-12 已完成**：`SessionStore` 记账 + `on_fail`/`on_write` 回调，`Thread` 变成
   `summary()["save_error"]` + 对话内警告，桌面端横幅 + 左栏图标，恢复后自动撤掉。
   入口：`ee/core/store.py`、`ee/core/session.py`、`ee/server/threads.py`、
   `desktop/src/components/{Conversation,Sidebar}.tsx`；测试 `ee/tests/test_store_failure.py`（4）、
   `desktop/src/components/SaveErrorBanner.test.tsx`（4）。详见 `roadmap-current.md`「执行记录（2026-11-12）」。
2. ~~`BLOCKING_RPCS` 是空集合，`Backend.call` 里的 `to_thread` 分支是死代码。~~
   → **2026-11-12 已完成**：先审计了一遍（94 个 RPC 处理函数**全部**是 async，没有一个需要进表），
   所以拆掉整张表与死分支，并在注释里留下两个反例（`thread/export` 双重 offload、
   `thread/delete` 需事件循环）。新增不变量：
   `test_audit_rpc_dispatch.py::test_all_handlers_are_coroutine_functions` —— 同步处理函数
   会直接在事件循环上跑，一次 git/磁盘 IO 就卡住所有连接；忘了写 `async` 会被这条报出来。
3. 跨会话共享 LSP 进程：需要先把 `publishDiagnostics` 改成多订阅者，否则诊断互相覆盖。
4. pi-lens 的提示（2026-11-12 部分处理）：~~部分 `int()` 无 try~~、~~Protocol 方法的 `...` 体（有意为之）~~。
   `int()`/`float()` 那类已集中到 `ee/core/coerce.py` 的 `as_int`/`as_float`（`threads.py` + `session.py` 共 44 处），
   并顺手修好一个真缺陷（会话文件里 `seq: "abc"` 会让 `thread/get` 500，`test_coerce.py` 有 RED 验证）。
   剩下**不再动**的几类（全量扫描才会出，且都是存量、有意为之）：
   - `python-empty-except`（threads 8 / session 6）：`except ...: pass` 全是尽力而为的收尾，
     旁边已写明理由（如「影子记录失败绝不能影响真实回合」），改成 log 只会制造噪声；
   - `python-assert-production`（6 处）：`assert self.session is not None` 这类内部不变式，
     本产品不跑 `-O`，改成 raise 反而丢信息；
   - `jscpd:duplicate`（3 对 6–9 行）：如 `_run_allout` 与 `_consume_goal` 的 GoalSpec 构造，
     抽函数可以做，但不属于本轮；
   - 2 条 `unreachable-except` 是**假阳性**（分析器认为 `except asyncio.CancelledError` 能吃掉
     后面的 `except Exception`/`BaseException`，实际 3.8+ 的 `CancelledError` 属 `BaseException`），
     已用 `lens_diagnostic_mark` 标为 false-positive。
   - 剩两处 `inline-styles` 是 hint（标题栏高度 / Logo path），依赖运行时值，不动。
   - `generic-api-key`（`ee/tests/*` 的 11 条）全是合成夹具，已新增根目录 `.gitleaks.toml`：
     `[extend] useDefault = true` + 按目录放行 `ee/tests/`。双向验过（夹具 17→0；
     在产品代码里放一个像真 key 的串仍然报），所以不是把扫描关了。
   注：以上都是**局部处理**（真修 / 原那么注或不注），没有在 `.pi-lens.json` 里
   全局降级/禁用任何规则——新出现的同一个规则照样会报。
5. 如果觉得 40 太低，可把 `MAX_LOADED_THREADS` 做成配置项；目前是常量。

## 新会话开场建议

> 先读 `AGENTS.md`、`research/roadmap-current.md`、`research/2026-11-11-type-cleanup-and-lifecycle.md`，
> 再看 `git show 9138a3e` 与 `git status`。已完成的项不要重做，从 backlog 里选。

## 本轮主要入口文件

- 后端：`ee/ee/server/app.py`、`automations.py`、`threads.py`、`loops.py`
- 内核：`ee/ee/core/store.py`、`session.py`、`subagent.py`、`hooks.py`、`lsp/manager.py`
- 桌面：`desktop/electron/backend.ts`、`desktop/src/lib/store.ts`
- 配置/文档：`pyrightconfig.json`、`.pi-lens.json`、`ee/docs/pyright.md`
- 新增测试：`ee/tests/test_store_summary.py`，以及 `test_automations.py`、
  `test_session_lifecycle.py`、`test_audit_rpc_dispatch.py`、
  `test_audit_ws_backpressure.py`、`test_lsp.py`、`test_loops.py` 里的新用例
