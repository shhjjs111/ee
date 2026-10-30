# ee 任务执行效率评测（2026-10-30）

用真实 DeepSeek（`deepseek-flash`）跑 `ee/scripts/eval_profiles.py` 的 10 任务集，
测量"全量测试引导"修复前后的行为差异。所有数字来自本轮真实运行，不是估算。

## 结论（一句话）

修复"模型跑全量 pytest 必然超时"这个问题后：**10 类任务 × full/lean = 20 次运行全部完成，
0 条 error/timeout**；全部 69 次 pytest 调用**都指定了测试范围，0 次全量**。

## 被测改动

| 提交 | 内容 |
|---|---|
| `54d2f9a` | 后台命令进程退出后，输出里明确引导模型停止轮询 |
| `7673976` | shell 规则加"跑测试用相关子集"（**单独验证过：无效**，见下） |
| `0e77ce2` | ① 仓库根 `AGENTS.md` 写清测试怎么跑 ② shell 检测到无范围 pytest 时即时警示 |

## 问题与根因（钉死的证据链）

| 事实 | 证据 |
|---|---|
| 全量 pytest 约 1064 秒 | 历史全量回归耗时（`acceptance-matrix.md`） |
| eval 单次 shell 硬上限 180 秒 | `ee/scripts/eval_profiles.py` 的 `--max-tool-timeout` 默认 180 |
| 相关子集只要 31 秒（快 34 倍） | 实测 `test_truncate_and_tools` + `test_prefix_cache` |
| 135 个测试文件 | `ls ee/tests/test_*.py \| wc -l` |
| 修复前仓库根**没有** `AGENTS.md` | 模型看不到本仓库测试怎么跑 |
| 任务书不给测试路径 | 03 任务书只说"修 truncate"，验收测试藏在 `_eval_check.py` |

**根因不是模型不听话，是信息缺失 + 数学上必然超时**：模型开局跑全量（1064s）→ 180s 被砍 →
转后台再轮询几十到几百秒。任何"你要跑子集"的提示词都救不了，因为它不知道该跑哪个。

## 三次同任务演进（任务 03-bugfix-truncate，full 模式）

同一任务、同一模型、同一任务书，只改引导：

| 阶段 | 步数 | 耗时 | 超时工具 | 最慢单次命令 |
|---|---|---|---|---|
| 原始 | 24 | 520s | 有 | `pytest -q` 全量 |
| 仅加提示词规则（`7673976`） | 29 | 754.6s | 有 | `pytest -q` 全量 180.3s |
| **方案 C（`0e77ce2`）** | **15** | **106.1s** | **0** | `pytest -c ee/pyproject.toml ee/tests/test_truncate…` **23.6s** |

**决定性证据**：修复后模型执行的命令格式正是 `AGENTS.md` 里教的写法（带 `-c ee/pyproject.toml`），
说明它读了并按指引执行。仅加提示词规则那一次完全无效——模型第一动作仍是全量。

## 全量复验（10 任务 × full/lean，本轮）

| 任务 | full | lean |
|---|---|---|
| 01-bugfix-context-delta | ✓ 13 步 66s | ✓ 8 步 55s |
| 02-bugfix-loopguard | ✓ 14 步 62s | ✓ 13 步 42s |
| 03-bugfix-truncate | ✓ 13 步 110s | ✓ 18 步 123s |
| 04-feature-fmt-tokens | ✓ 14 步 49s | ✓ 15 步 57s |
| 05-multifile-loopguard-switch | ✓ 36 步 154s | ✓ 22 步 80s |
| 06-tests-cap | ✓ 12 步 41s | ✓ 8 步 45s |
| 07-docs-profiles | ✓ 28 步 110s | ✓ 39 步 99s |
| 08-homework-primes | ✓ 7 步 36s | ✓ 9 步 39s |
| 09-refactor-loopguard-text | ✓ 22 步 358s | ✓ 11 步 153s |
| 10-bugfix-style-title | ✓ 31 步 339s | ✓ 21 步 141s |

| 汇总 | full | lean |
|---|---|---|
| 完成率 | 100%（10/10） | 100%（10/10） |
| 平均步数 | 19.0 | 16.4 |
| 平均输入 token | 358,811 | 205,844 |
| 缓存命中率 | 96% | 95% |
| 平均耗时 | 129.8s | 80.3s |
| 异常/未通过条数 | **0** | **0** |

### pytest 调用统计（引导是否生效的硬指标）

对全部 20 次运行的工具调用做模式统计：

```
pytest 调用：疑似全量 0 次，有范围 69 次
```

## 剩余问题（未修，如实列出）

1. **`pip install` 撞 180s 超时**：full 侧 `最慢单次 shell 180.5s` 是
   `./.venv/Scripts/python.exe -m pip install -q -e "ee[test]"`，属环境类慢命令，
   不是 pytest 全量。任务仍完成（模型绕过了），但这类"装依赖"命令也值得给同样的即时提示。
2. **07-docs 的 lean 用了 39 步**（比 full 的 28 步还多）：文档任务的模式差异值得单独看，
   不影响完成率，但说明 lean 在非代码任务上未必更省。

## 验证记录（真实执行）

| 项 | 命令 | 结果 |
|---|---|---|
| 新增引导测试 | `pytest ee/tests/test_full_test_guidance.py` | 4 passed（先 RED：函数不存在 + AGENTS.md 缺失） |
| 后台轮询引导 | `pytest ee/tests/test_bgshell_guidance.py ee/tests/test_bgshell.py` | 10 passed |
| 预算回归 | `pytest ee/tests/test_prefix_budget.py ee/tests/test_lean_profile.py` | 通过（lean ≤1200，规则仅 full 加载） |
| 广回归 | `pytest test_bgshell test_shell_rules test_prefix_budget test_details test_safe_options` | **200 passed** |
| 全量评测 | `eval_profiles.py --runs 1 --model deepseek-flash` | 20/20 completed，0 异常 |

## 方法学教训（下次别重犯）

1. **提示词层改动必须用真实模型验证**。`7673976` 的单测全绿，但真实模型第一动作仍是全量——
   单测只能证明"规则文本存在"，不能证明"模型会照做"。
2. **单次好转可能是运气**。修 `54d2f9a` 后复测 03 得到 234s，当时以为好了；
   实际是模型随机性，下次就是 755s。n=1 不能定论。
3. **补信息 > 加规则**。告诉模型"该跑哪个文件"比告诉它"要跑子集"有效得多。

## 成本

本轮全部评测约 800 万 tokens（含多次复测），DeepSeek flash 单价下总计约 2-3 元。

---

## Wave 6 / T20：成本驱动的上下文策略（影子模式，2026-10-30）

新增 `ee/ee/core/context_policy.py`，**只观察记录，不执行任何改写**——这是计划原文的硬约束：
改写早期历史会让后续请求按"未命中缓存"计费，本仓库实测命中率 92–96%，
省 token 反而可能涨钱。所以先记录"本可以怎么做"，等配对评测证明收益再谈开启。

| 不变量 | 实现 | 测试 |
|---|---|---|
| 影子不执行 | `Decision.applied` 恒为 False | `test_shadow_never_applies_even_when_enabled_flag_true` |
| 缺失数据保守 | window/used 缺失 → `hold` | `test_shadow_is_conservative_when_data_missing` |
| 前缀不稳保守 | 未冻结前缀 → `hold`（改写代价高） | `test_shadow_holds_when_prefix_unstable` |
| 失败多保守 | failures≥3 或 retries≥8 → `hold` | `test_shadow_holds_when_failures_high` |
| **默认关闭** | `settings()` 默认 `enabled=False` | `test_policy_disabled_by_default` |
| **关闭零副作用** | 关闭时连日志都不写 | `test_observe_writes_nothing_when_disabled` |
| **绝不改历史** | 即便建议 compact 也不动 history | `test_observe_never_rewrites_history` |
| **项目层不能开启** | 加入 `PROJECT_DROP_TOP` | `test_project_layer_cannot_enable_policy` |
| 记录可回放 | `as_record()` 纯字典，JSON 可序列化 | `test_record_is_plain_dict_for_replay` |

验证（真实执行）：`test_context_policy.py` **15 passed**；接入 session 后
`test_prune + test_compaction + test_compaction_incremental + test_prefix_cache` = **45 passed**
（零回归）；config 链路 `test_redo_security_config + test_config_scope + test_config_shell`
= **43 passed / 1 skipped**。

**未做（等配对评测）**：真的开启策略。计划要求"只有配对评测证明成功任务总成本下降且完成率
不降后才在可撤销设置中开启"——这需要真金白银跑 A/B，属于下一阶段。


### 实测：策略在各模型窗口下的触发情况（修正口径后）

先用**累计** token 当上下文占用得出"273% 超窗"是错的（`eval_profiles` 的 `avg_prompt` 是整次运行
所有请求的累计，不是单次上下文）。正确口径是 `context_tokens()`（最后一次请求的
`last_prompt + last_completion`，与自动压缩同口径）：

| 指标 | full | lean |
|---|---|---|
| 累计输入 token | 358,811 | 205,844 |
| 平均步数 | 19 | 16 |
| **平均每步上下文** | ≈18,885 | ≈12,865 |
| **末步上下文（≈2×平均，线性增长）** | **≈37,770** | **≈25,730** |

对照各模型窗口（修剪阈值 60%）：

| 模型 | 窗口 | 60% 阈值 | full 末步占比 | 是否触发 |
|---|---|---|---|---|
| default / qwen / doubao / glm | 131,072 | ≈78,600 | 28.8% | 否 |
| claude | 200,000 | 120,000 | 18.9% | 否 |
| kimi | 262,144 | 157,286 | 14.4% | 否 |
| deepseek-flash | 1,000,000 | 600,000 | 3.8% | 否 |

**结论：20 次真实任务里，上下文峰值约 37.8K，任何模型窗口下都远不到 60% 修剪阈值。**
所以 T20 的影子策略在这个任务集上**记录的全是 hold**；跑配对 A/B 会得到"两组无差异"，
不值得花评测费。策略真正有意义的是**超长会话**（几百轮）或**极小窗口模型**，不是单任务。

### 顺带修掉的两个真缺陷（本轮实测暴露）

| 缺陷 | 现象 | 修法 |
|---|---|---|
| 前缀未冻结被当成"不稳定" | 刚开会话 `prefix is None` → 永远 hold，影子记录全是噪声 | 只有 `shaA → shaB` 的**真实切换**才算不稳定 |
| 取错用量字段 | 用 `last_prompt + last_completion` 而非 `context_tokens()`，缺少粗估兜底 | 改用 `session.context_tokens()`，与自动压缩同口径 |

两条都有回归测试：`test_fresh_session_without_prefix_still_advises`、
`test_prefix_switch_is_detected_as_unstable`。

验证：`test_context_policy.py` **17 passed**；连带
`test_prune + test_compaction + test_compaction_incremental + test_prefix_cache + test_redo_security_config`
= **68 passed**（零回归）。

