# ee 优化验收矩阵

每项给出可观察行为、证据类型、当前状态。状态取值：**未开始 / RED（已复现缺陷）/ GREEN（已修复并有证据）/ 待验证假设 / 无法自动验证**。

证据必须是本轮工具输出（测试回执、探针 stdout）或人工操作记录，不接受"看起来对了"。

## 1. 费用与预算

| ID | 验收面 | 操作 | 期望观察 | 证据类型 | 状态 |
|---|---|---|---|---|---|
| ACC-01 | 混币不合并 | 同一会话先后产生 CNY 与 USD 计费 | 两币种分别列出，不出现把 USD 累加到 CNY 的单一总额 | `test_usage_accounting.py`（5 passed） | **GREEN**（提交 607ec92） |
| ACC-02 | 缺用量不假完整 | 一次请求无 usage | 总额显示"已知部分 + 未知条数"，不写成确定金额 | `test_usage_accounting.py` | **GREEN**（提交 607ec92） |
| ACC-03 | 重启不翻倍 | 花费后重启两次 | 会话/每日金额稳定 | `test_usage_restore.py`（5 passed） | **GREEN**（提交 0e94068） |
| ACC-04 | 子代理只入账一次 | 子代理产生费用 | 父会话视图含该费用，日/月账只加一次 | 单测 | 未开始 |
| ACC-05 | 未知单价可见 | 模型未配价格 | 显示"价格未知"，不显示 0 | `test_usage_accounting.py` | **GREEN**（原本已正确） |
| ACC-06 | 预算不假装硬限 | 缺 usage + 无人值守 + 设上限 | 停止继续发起付费请求并说明原因 | `test_budget_incomplete.py`（4 passed） | **GREEN**（提交 d7feea2） |

## 2. Goal 与验证

| ID | 验收面 | 操作 | 期望观察 | 证据类型 | 状态 |
|---|---|---|---|---|---|
| ACC-10 | 判据进模型输入 | 桌面端带判据发起 Goal | 下一轮模型输入含判据命令与失败详情 | `test_goal_feedback.py`（4 passed） | **GREEN**（提交 87883a0） |
| ACC-11 | 失败证据驱动修复 | 真实临时仓库，判据先红 | 修复后转绿，不需要人工重复描述错误 | 真实临时仓库运行 | 未开始 |
| ACC-12 | 不假绿（零测试） | 判据只 collected / 全 skipped | 判为未通过 | `test_test_results.py`（10 passed） | **GREEN**（提交 39ec155） |
| ACC-13 | 轮次语义不变 | max_rounds=3 | 3 次判定、最多 2 个修复回合 | 单测 | 未开始 |
| ACC-14 | 首轮通过不跑模型 | 判据本来就成立 | 0 次模型调用 | 单测 | 未开始 |
| ACC-15 | 停止真能停 | 模型/工具/判据三阶段各停一次 | 请求中→真实中断，判据进程不残留 | `test_goal_interrupt.py`、`test_goal_cancel.py` | **GREEN**（判据阶段；桌面反馈属 Wave 3） |
| ACC-16 | 候选证据绑定版本 | 候选验证后改动候选 | 显示 stale，不再声称已验证 | 单测 | 未开始 |

## 3. 交互可靠性

| ID | 验收面 | 操作 | 期望观察 | 证据类型 | 状态 |
|---|---|---|---|---|---|
| ACC-20 | 跨会话退回不丢 | 活跃 C，A/B 先后退回 | A/B 均能找回，顺序稳定 | `drafts.test.ts`（14 passed）、`interaction.test.ts` | **GREEN**（提交 60b706b） |
| ACC-21 | 同毫秒事件不覆盖 | 两条退回同毫秒 | 两条都在 | `drafts.test.ts` 按 id 去重用例 | **GREEN**（提交 60b706b） |
| ACC-22 | 停止返回 false 可见 | RPC 返回 `{interrupted:false}` | 有明确提示，不假装已停 | `interaction.test.ts`（3 passed） | **GREEN**（提交 60b706b） |
| ACC-23 | 切回计时正确 | 切回运行中会话 | 显示真实累计时长，不每次归零 | `interaction.test.ts` T13 四条 | **GREEN**（提交 60b706b） |
| ACC-24 | 发送失败保文字 | 发送失败 | 文字退回输入框，不丢 | 已有 + 扩展 | 未开始 |
| ACC-25 | 断线重连不串状态 | 断线期间切换会话后重连 | 不覆盖已确认的新条目 | 单测 | 未开始 |

## 4. 可观测与评测

| ID | 验收面 | 操作 | 期望观察 | 证据类型 | 状态 |
|---|---|---|---|---|---|
| ACC-30 | 延迟可分解 | 一次失败回合 | 能读出连接/首字/工具/审批/渲染各段 | `test_request_timing.py` | **GREEN**（后端三段；渲染段属 Wave 7） |
| ACC-31 | 费用可归因 | 评测结果 | 成功任务总费用、返工占比、缺失条数 | `test_eval_cost_rollup.py`（4 passed） | **GREEN**（提交 45fa9c7） |
| ACC-32 | 离线门槛可跑 | CI 或本地 | 无需 key 即可判定不退化 | CI 日志 | 未开始 |
| ACC-33 | 付费评测隔离 | 普通 PR | 不触发真实付费调用 | CI 配置 | 未开始 |

## 5. 证据缓存与连续性

| ID | 验收面 | 操作 | 期望观察 | 证据类型 | 状态 |
|---|---|---|---|---|---|
| ACC-40 | 未变内容可复用 | 内容未变、mtime 变 | 命中缓存 | `test_evidence_wiring.py::test_touch_does_not_invalidate_read_ref`（真实 ReadTool） | **GREEN**（判据接线：内容身份取代 mtime+size） |
| ACC-41 | 内容变必失效 | 内容变、mtime/size 还原 | 未命中 | `test_evidence_wiring.py::test_content_change_with_restored_mtime_size_must_invalidate` | **GREEN**（同上） |
| ACC-42 | 权限收紧优先 | 只读/`.eeignore`/沙箱 | 即使有缓存也拒绝 | 安全回归 | 未开始（`may_use_cache` 纯函数已测，但当前缓存是会话内存态记账、不存内容，无跨权限复用路径；端到端安全断言未写） |
| ACC-43 | 无幽灵引用 | 回退/压缩/截断后读同区间 | 返回正文，不返回"见上文" | `test_read_visibility.py`（3 passed） | **GREEN**（提交 d928421） |
| ACC-44 | 压缩后记得约束 | 判据命令在第一轮就进历史，中途压缩不丢 | `test_continuity.py`（3 passed） | **GREEN**（提交 d2a6d86） |
| ACC-45 | 节省可解释 | 打开上下文面板 | 三类节省分开，未知不显示 0 | 快照测试 | **GREEN**（服务端缓存命中带覆盖率 `cacheLine`；修剪/压缩已有；未知→None 不显示 0；T19） |
| ACC-46 | 缓存可整体关闭 | 关闭开关 | 行为回到真实读取，不报错 | 开关测试 | 未开始 |

## 6. 流畅度

| ID | 验收面 | 操作 | 期望观察 | 证据类型 | 状态 |
|---|---|---|---|---|---|
| ACC-50 | 流式期间可输入 | 流式输出中打字 | 输入无卡顿、字不丢 | 手工 + 性能计数 | 未开始（合并 + 增量渲染已落地，真实 Electron 输入延迟未实测） |
| ACC-51 | 合并不改文本 | 1000+ delta 回放 | 最终文本与事件逐字一致 | 回放单测 | **GREEN**（streamBuffer.test 12 例 + store.test 3 例；T22） |
| ACC-52 | 长会话不劣化 | 长会话滚动 | 帧率与输入延迟在阈值内 | 性能测量 | 未开始（窄订阅 + 流式纯文本已落地，帧率/输入延迟未实测） |
| ACC-53 | 顺序不串 | 工具/错误/审批交错 | 位置与顺序正确 | 单测 + 手工 | **GREEN**（streamBuffer 合并按序 + selectors 引用稳定，不同 item/thread 不串；T22） |

## 当前总览

| 状态 | 数量 |
|---|---|
| GREEN | 21 |
| 未开始 | 17 |

已交付的 11 项均有测试与提交证据。后端全量回归：**1822 passed / 16 skipped / 0 failed**
（2026-10-29，耗时 1064 秒）。

### 已知的既有 flaky（不是本次改动引入）

`test_robustness.py::test_http_status_error_message` 偶发失败（约 1/5 次），
报 `httpx2.ReadError / WinError 10053`（本地 HTTP 桩的 socket 被中断）。

已用基线提交 `96a3e4e` 对照：在基线上同样跑 5 次，也出现 1 次失败。
判定为既有环境 flaky，与本次改动无关；未修（不在批准范围）。

> 本矩阵随每波推进更新。更新时必须同时更新"证据"列，写明测试文件名或回执 ID。

## 真实模型评测（deepseek-flash，2026-10-30）

见 `research/optimization/goal-ab-experiment.md`。核心结论：

- Goal 失败反馈在需要从测试输出定位的 bug 上，让模型平均少 1 次请求、成本低约 13.5%（n=5，方向稳定，非统计显著）。
- 简单任务上无差异（符合预期：那种任务不依赖失败输出）。
- 缓存命中率真实测得 94–95%，证明冻结前缀 + 增量环境这套设计在真实服务端确实省到了钱。

后端全量最终回归：**1846 passed / 16 skipped / 0 failed**。
