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
