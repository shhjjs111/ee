# LSP：从「每个会话常驻一个进程」改成按需取用

状态：**设计，未实现**。
日期：2026-11-12
相关：`roadmap-current.md`（2026-11-12 执行记录）、`2026-11-11-type-cleanup-and-lifecycle.md` 的 backlog 第 3 条、`2026-10-29-d6-2-lsp-tasks.md`、`ee/ee/core/lsp/{manager,diagnostics,notify}.py`、`ee/ee/tools/lsp.py`

---

## 0. 一句话

backlog 原写的是「跨会话共享 LSP 进程」，审计后结论要改：**先别共享，先别常驻**。
现在每个会话在第一个回合就启动一个语言服务器，并把它按引用计数一直握到会话释放；
桌面端最多缓存 40 个会话，等于最多 40 个进程。改成「用到才取、用完就还」，同一份收益，
没有跨会话共享带来的文档状态冲突。

## 1. 现状（审计事实，都可在代码里对上）

| 事实 | 位置 |
|---|---|
| 首次回合开始时**主动** `acquire`，与本次回合是否要用诊断无关 | `core/session.py:929` `_attach_lsp`，`turn_runner` 在 `:1371` 调它 |
| 拿到的句柄存进 `self._lsp_handle`，直到 `Session.aclose` 才 release | `core/session.py:981`；`:395` 的 `aclose` 分支 |
| manager 的空闲回收只收 `refs == 0` 的条目 | `core/lsp/manager.py` `_reap_loop`（`DEFAULT_IDLE_TTL=120`） |
| 所以主句柄在会话存活期间让 refs 恒为 1 → **120 秒 TTL 实际上永不生效** | 同上两条合起来 |
| 会话缓存上限 40，淘汰才 `aclose` | `server/app.py` `MAX_LOADED_THREADS` |
| `lsp` 工具只读 `DiagStore`，自己**不** acquire | `tools/lsp.py:53` `run()` |
| 诊断靠 `publishDiagnostics` 回调 + `Notifier` 发 `didOpen`/`didChange` | `session.py:973` 附近、`core/lsp/notify.py` |

推论：只要用户开了 `[lsp].enabled`，**即使模型一个诊断都没要**，每个打开过的会话也会替它养一个
语言服务器进程。这是当前真正的成本项，也是「每会话一个 manager」这个设计的副作用。

> 待实测（实施时第一步）：单个 `pyright-langserver` 的常驻内存，以及「打开 10 个会话、各跑一个
> 回合」时的进程数与总内存。没有这组数字之前，本方案只按结构论证，不写具体百分比。

## 2. 为什么先不做跨会话共享

共享一个进程要同时解决两件事，第二件没有安全的默认答案：

1. **诊断多订阅者**：`LspClient.on_notification["textDocument/publishDiagnostics"]` 现在是单槽
   （`session.py:973` 直接赋值），两个会话共享会互相覆盖——这一条好改（fan-out 列表）。
2. **文档所有权**：语言服务器的文档状态是**按 URI 唯一**的。两个会话各自 `didOpen` 同一文件、
   内容不同（一个会话改了、另一个还没读到）时，服务器只保留最后一份；另一边收到的诊断属于
   它没见过的版本。而 `DiagStore` 的 `version`/`fingerprint` 是按会话算的
   （`core/lsp/diagnostics.py` 的 `change()`），冲突时要么丢诊断、要么把别人的诊断当自己的。
   这不是「多写一点代码」的问题：**给模型错的类型信息比不给更危险**（它会照着改）。

结论：共享进程要么限定「同一时刻只允许一个会话持有某 URI」（等于把并发诊断排成串行，
模型拿不到就等），要么就得引入文档所有权仲裁——那是另一个设计。先做 §3，把 40 个常驻
进程降到「正在用诊断的那一两个」，收益的大头已经拿到。

## 3. 推荐方案：按需 acquire，回合结束就还

### 3.1 接线改动

1. **`_attach_lsp` 不再 acquire**：只建 manager/store/notifier 与 spec 元信息，
   把「怎么取」交给工具。保存 `_lsp_spec` / `_lsp_identity`，供按需 acquire 使用。
2. **`LspTool.run` 取用**：目标文件在 store 里没有可用结果时，`await manager.acquire(...)`，
   `notifier.attach(uri, client)`，发 `didOpen`，等诊断（沿用现有 `wait` 超时），
   然后 `finally: await manager.release(handle)`。
   - 判据：`store.get(uri)` 是 `FRESH` 就不取进程（诊断已经在内存里）。
3. **回合内复用**：同一回合里连续问两个文件时，第二次命中 §3.1.2 的 FRESH 判定就不用再取；
   需要重新取时 manager 的池按键复用，不会真起第二个进程。
4. **写后同步（`_notify_lsp`）**：没有活着的客户端就跳过（现在也是这样，`client_for` 返回 None 就返回），
   不为了推送诊断去起进程——下一次 `lsp` 调用会拿最新内容重新打开。
5. **删除 `lsp_manager`/`_lsp_handle` 的常驻语义**：`aclose` 里的 release 变成「如果恰好持有」，
   语义保持不变（幂等）。

### 3.2 行为变化（要写进用户可见的说明）

- **首包**：第一次 `lsp` 调用要多等 1–2 秒（启动 + initialize），换取「不查诊断的会话零成本」。
  这比现在「回合开始先卡 1–2 秒、哪怕不用」更符合实际使用（大多数回合不查诊断）。
- **诊断时效**：会话空闲 120 秒后进程被回收；下次问诊断时重新启动并重新 `didOpen`。
  `DiagStore` 里的旧结果会因重新 `open()` 而作废（`open()` 本来就清 `_results`），不会出现
  「进程换了、旧诊断还当 fresh」。
- **并发**：同一会话内 `manager.acquire` 已有 per-key 锁与锁内重读（2026-11-11 修过），
  两个工具调用并发也只会起一个进程。

### 3.3 测试

| 层 | 用例 | 断言 |
|---|---|---|
| 单元（假服务器） | 回合开始不再启动进程 | 跑一个不调 `lsp` 的回合，`manager.stats()` 为空 |
| 单元 | 首次 `lsp` 调用才启动 | 调用后 `stats()` 有一条、`refs` 回到 0 |
| 单元 | FRESH 命中不重复取 | 第二次调用 `acquire` 次数不增加（spy） |
| 单元 | 异常路径也 release | 工具抛错后 `refs == 0`（否则 TTL 永远不收） |
| 集成（真 pyright） | 启动 → 取诊断 → release → `shutdown()` 后进程消失 | 现有 `test_lsp_integration.py` 加一条 |
| 回归 | 现有 30 例 `test_lsp.py` 全绿（含并发 acquire 只起一个进程） | — |

### 3.4 风险与回滚

- **风险**：工具里多一次 `await acquire` 会拉长工具调用的超时预算。缓解：沿用现有
  `wait_for` 的 120 秒等待上限与 `init_timeout`（20 秒），失败按 `D.UNAVAILABLE` 如实报，不重试。
- **回滚**：改动集中在 `_attach_lsp` + `LspTool.run` 两处；回滚 = 恢复 `_attach_lsp` 里的 acquire，
  工具不改也能工作（store 里已有诊断）。不涉及协议与配置。

## 4. 后续（本次不做）

- **共享进程**：等 §2 的两件事都有答案再谈。真要做的第一步是把 `on_notification` 改成
  多订阅者（fan-out 列表 + 按订阅者过滤 URI），第二步才是文档所有权仲裁（「同一 URI 同一时刻
  只有一个 owner，其他会话只读最近一次结果」）。
- **UI**：设置页显示「正在运行的语言服务器」（`LspManager.stats()` 已经有数据，没有消费方）。
- **每会话的诊断缓存上限**：`DiagStore` 现在只增不减（长会话会攒下所有打开过的文件的
  文本与诊断）。做懒加载时顺带加 LRU（按最近使用，上限几十个文件）。

## 5. 为什么现在只写方案不直接改

这条改动会改变「第一次要诊断时的等待位置」（从回合开始挪到工具调用里），属于用户能感知的
时序变化；同时 `test_lsp_integration.py` 依赖真实 `pyright-langserver`，本机有、CI 靠
`npm i -g pyright`，两边行为要分别确认。先落方案，下一轮按 §3.3 的表逐条做，避免把
「LSP 不再常驻」和「诊断语义变了」混在一次改动里。
