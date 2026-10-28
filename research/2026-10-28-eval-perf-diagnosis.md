# 真实模型评测：性能异常诊断记录

日期：2026-10-28（**2026-10-29 定位根因、修复，并用真实 key 实测验证**）
状态：**根因已定位、已修复、已用真实 API 跑通验证**。原文写的是「问题未定位」；
下面的『修正』一节说明当时哪两处判读错了、为什么错。

## 一句话结论

慢的不是模型，也**不是「每步都慢」**：三次运行里各有一条 shell 命令跑满了
`MAX_TIMEOUT = 600` 秒上限，把整回合耗时完全盖住。模型侧 12 次请求合计 17.1 秒
（1.1–2.4s/次，缓存命中 90–99%），网络、上下文规模、推理 token 都正常。

## 真实 key 实测（2026-10-29，闸门生效后的最终数据）

同一任务（01-bugfix-context-delta）、真实 DeepSeek、`--max-tool-timeout 180`：

| 轮次 | 总耗时 | 工具耗时 | 模型+启动 | 最慢单次 | 超时 | 结果 |
|---|---|---|---|---|---|---|
| 修前（历史） | 669.0s | 641.1s | 约 28s | shell **600.3s** | 是 | max_steps |
| 实测 1 | 486.2s | 451.9s | 约 34s | shell 180.3s（撞上限） | 1 | max_steps |
| 实测 2 | 176.3s | 152.4s | 约 24s | shell 109.8s | 0 | — |
| 实测 3 | **136.7s** | 112.3s | 24.4s | shell **67.7s** | 0 | completed |

实测 3 的完整工具明细（这次终于能看到「在跑什么」）：

```
 67.7s shell  python -m pytest -q tests/test_prefix_budget.py tests/test_compaction.py ...
 19.3s shell  python -m pytest -q tests/test_prefix_cache.py 2>&1 | tail -80
 18.8s shell  python -m pytest -q tests/test_prefix_cache.py 2>&1 | tail -20
  3.0s shell  python -m pytest -q tests/test_prefix_budget.py tests/test_context.py ...
  2.8s shell  python -m pytest -q tests/test_prefix_cache.py 2>&1 | tail -60
  0.4s shell  git log --oneline -5; git status --short
  其余 read/grep/glob/edit 均在 0.1s 以内
```

**结论：600 秒的尾巴已经没了。** 剩下的时间有两块，都不是缺陷：

1. **模型自己扩大了验证范围**（67.7s）：任务提示只要求跑
   `tests/test_prefix_cache.py`，但模型额外跑了 `test_prefix_budget.py` /
   `test_compaction.py` 等一组文件。这是 agent 的合理行为（确认没弄坏别处），
   不是 bug；要压缩它得改任务提示或给 pytest 加速。
2. **每个用例的真实开销**：`test_prefix_cache.py` 6 个用例 15.2 秒，其中
   `test_history_append_only` 一个就 7.9 秒。而这个套件里**单步最短回合**
   （1 次模型调用、无工具、假客户端）实测 **1.45 秒**——用例慢是它自己
   spawn Session/回合的真实成本，不是 import 开销（`import ee.core.session`
   只要 0.62s）。


## 实测数据（对账自 `%APPDATA%\ee\logs\ee-backend.log`）

日志里模型请求有毫秒级的「开始 / 结束」时间戳，两者之间的空档就是工具执行时间。

| 回合 | 起止 | 总耗时 | 模型耗时 | 工具耗时 | 最大单次空档 |
|---|---|---|---|---|---|
| 01-bugfix-context-delta | 18:47:06 | 171.6s | 15.3s | 142.6s | 102.2s |
| 01（重跑） | 18:53:39 | 210.2s | 17.3s | 183.9s | 74.6s |
| 01（重跑） | 18:59:12 | 770.4s | 12.5s | 747.3s | **600.3s** |
| 02-bugfix-loopguard | 19:12:29 | 635.1s | 10.1s | 611.5s | **600.4s** |
| 06-tests-cap | 19:38:09 | 55.6s | 16.6s | 27.9s | 23.9s |
| 01（max_steps 收尾） | 19:40:32 | 669.0s | 17.1s | 641.1s | **600.3s** |

## 根因

`ee/ee/tools/shell.py:17` 的 `MAX_TIMEOUT = 600`。`bash_timeout`（配置里是 120）只是
**默认值**：模型在 tool call 里显式传 `timeout`，schema 允许一直传到 600，于是一条跑不完的
命令可以合法地占用 600 秒。实测三次空档是 600.3 / 600.4 / 600.3 秒，全部精确卡在这个值上
（不是 599、不是 601），形状就是「撞上限被强杀」：

```
19:00:14 -> 19:10:14   600.3s   (01)
19:13:01 -> 19:23:01   600.4s   (02)
19:41:36 -> 19:51:37   600.3s   (01 的第三次运行)
```

**是 3 次，不是 2 次**——19:41 那次属于 01 的第三次运行。

**那条命令是什么：无法证实。** `--json` 事件流被 `eval_profiles.py` 丢弃，`--no-persist`
又不写会话 JSONL，命令原文没有留下来。同一任务的更早运行里有 102.2s / 74.6s / 69.3s 的工具
空洞，形状与 pytest 一致，任务本身也明确要求跑 pytest；单文件 `tests/test_prefix_cache.py`
实测 15.0–18.8 秒，与日志里两个 ~18.6 秒的空档吻合。**推断**那条 600 秒命令是更大范围的
pytest（全量套件远超 600 秒），但这只是形状推断、不是命令原文——按「未证实」对待。

## 修正：原文里两处判读错误

**1. 「每步 56–91 秒，比正常值 5.3 秒/次慢 10–17 倍」是把整回合墙钟除以步数得出的假象。**

`turn_done.duration_ms` 是整回合墙钟时间，**包含工具执行**。669.0s ÷ 12 步 = 55.8s 不是模型
延迟，是「总时间 ÷ 步数」。扣掉那一条 600 秒命令后，01 是 169s、02 是 35s，与 06 的 55.6s
同量级——不存在普遍性的 10–17 倍变慢。

**2. 「探针 5.3 秒/次」不是模型延迟。**

19:37:21.827 会话开始 → 19:37:24.986 才建好客户端（3.2s，含 `import openai` 与 TLS），
请求本身 1.8 秒，回合总 5.3 秒。真实模型请求一直只有 1–2 秒。

另修正一处计数：原文说「47 次请求对应 2 个任务，每步约 2 次」。实测 18 点 28 次 + 19 点
40 次 = 68 次请求，覆盖 01 的三次重跑、02、03、06 和两次探针；01 本身 12 次、02 本身 7 次，
**全部是第 1 次尝试，没有一次重试**。日志里 6 次「第 2 次」全部来自测试假服务器
（`127.0.0.1:5xxxx`，`messages=1 tools=0`），与真实调用无关。

## 原文排除项复核（结论仍然成立）

| 假设 | 实测 | 判定 |
|---|---|---|
| 网络慢 | 首字节 0.20s，连接 0.04s | 排除 |
| `git status` 慢 | 0.10–0.19s | 排除 |
| 推理 token 爆炸 | 单次 reasoning 常为 0–468 tok | 排除 |
| 单次模型调用慢 | 0.8–2.4s；最慢一次 5.7s（那步生成 1123 completion tok） | 排除 |
| 验收 pytest 慢 | 单文件 15–18.8s | 不足以解释 600s |
| API 重试 | 真实调用零重试 | 排除 |

## 已做的修复

1. **`tool_end` 事件补 `duration_ms` / `timed_out` / `exit`**（`ee/ee/headless.py`）。
   `tools/details.py` 的 `CommonDetails` 本来就有这两个字段，只是没写进 JSON 事件流，
   于是外部只能看到「模型请求开始/结束了」，工具时间是黑箱——这是本次判读错误的直接成因。
   回归测试：`tests/test_exec.py::test_tool_end_reports_duration_and_timeout`。
2. **新增会话级硬上限 `shell_max_timeout`**（`ee/ee/config.py` 默认 `0` = 不额外限制；
   `ee/ee/tools/base.py` 的 `ToolContext.max_timeout`；`ee/ee/tools/shell.py` 封顶；
   `ee/ee/core/session.py` 接线）。配成 120 之类，模型显式传 600 也不再越过。
   回归测试：`tests/test_shell_timeout_cap.py`。
   **回滚验证**：去掉封顶逻辑后命令跑满 34.8 秒自然结束（`timed_out=False`），测试转红—
   证明测试真的绑定在这个修复上。
3. **把这个上限接到无人值守路径上（关键，否则等于没修）**：
   - `ee exec` 新增 `--max-tool-timeout 秒`，经 `unattended_configure` 写进会话配置。
   - `eval_profiles.py` 新增同名参数，**默认 180**，并传给 `ee exec`。
   
   为什么必须有这一步：配置默认是 `0`（不限制），只为不改变交互式既有行为。但评测走的是
   `ee exec` 子进程，不给它硬上限，模型照样能传 `timeout: 600` 占满 10 分钟。实测这一条是
   分水岭——补上之后同样是「模型显式要求 600 秒」的命令，工具在 **5.3 秒**被截断
   （`timed_out=True`），整轮 7.1 秒结束；修复前是 600.3 秒。
4. **`eval_profiles.py` 把模型时间与工具时间分开记**：新增 `tool_s`、`tool_timed_out`、
   `slowest_tool`，汇总表加「其中工具(s) / 超时工具 / 最慢单次」三列。以后再出现墙钟异常，
   一眼能看出落在模型侧还是工具侧。回归测试：`tests/test_eval_profiles.py` 新增 2 例。
5. **修 `resolve_ee_cmd` 的回落分支**（`ee/scripts/eval_profiles.py`）。`1bc0e12` 只修好了
   「旁边就有 `ee.exe`」这条正常路径；找不到 `ee.exe` 时仍然返回裸 `[python, "-m", "ee"]`，
   这依然依赖 cwd，评测会退化成 `no_output · 0 步`。现在改为 `-c` + `sys.path` 注入包目录，
   实测在无关 cwd 下 `exec --help` 返回 rc=0。

## 顺带发现并修掉的一个既有测试失败

全量套件里 `tests/test_redo_edit.py::test_eval_ee_cmd_resolution` 一直是红的，
根因是 `1bc0e12` 的**漏改**：它把 `resolve_ee_cmd(None)` 的默认行为从
`[python, "-m", "ee"]` 改成「优先返回 `ee.exe`」，但没同步这条断言——而那条断言
验证的恰恰是刚被修掉的缺陷行为（`-m ee` 依赖 cwd）。

已用 HEAD 原始代码验证这是**既有问题、与本次性能修复无关**：把 `eval_profiles.py`
还原成 HEAD 版本后，同一条测试照样失败。

断言已改为验证真正的不变量：默认命令不能是 `-m` 形式、首元素必须是可执行文件。
回滚验证：把实现退回旧行为后测试立刻转红（`默认命令仍依赖 cwd`），恢复即绿。

## 引擎自身的每回合开销（分层实测）

用假客户端把模型耗时归零，剩下的就全是 ee 自己的开销：

| 项 | 耗时 |
|---|---|
| Session 构造 | 594–625ms |
| 第 1 回合（新会话，含首建影子仓库） | 1375ms → **1015ms** |
| 第 2/3 回合（稳态） | **406 / 422ms** |
| 首次 capture（建影子仓库） | 985ms → 约 640ms |
| 稳态 capture | 156–172ms |

结论：**稳态约 400ms/回合，模型请求中位 1.25s——模型才是主体。**
那 136.7 秒的真实评测里，模型 24.4s + 工具 112.3s，工具里 67.7s 是模型自己
多跑了一组 pytest（`test_prefix_budget` + `test_compaction` 等），不是引擎慢。

**Windows 上每个 git 子进程恒定 70–100ms，与工作量无关**（实测 40 个文件的工作区里
`add -A` 和 `add -A --dry-run` 一样快：63ms vs 62ms）。所以这类开销只能靠减少进程数，
不能靠"少做点活"。

据此做掉的三处（均有无副作用的验证）：

1. **影子仓库配置内联写入**（`snapshot.py` `_write_config`）。原先 `_ensure()` 用
   `init` + 5 次 `git config`，`_ensure` 656ms；改成 init 后直接把这几个常量键追加进
   `.git/config`，**656ms → 312ms**。带校验 + 兜底：写完用一次 `config --list --local`
   核对，不符或解析失败就退回逐个 `git config`；config 为空时先重新 init 拿段头。
2. **`_big_untracked` 按工作区 mtime 缓存**。每次 capture 都跑一次 `git ls-files`
   （约 65ms），大文件很少新增。缓存后 capture 219ms → 140ms。
3. **只读回合跳过收尾 capture**。只读回合不可能有文件改动，before 就是 after。
   用 `_access()` 顺手累积的 `_turn_may_write` 判断。不牺牲 `/undo`——
   `_disk_undo` 里是 `after = e.get("after") or cur`，缺了就退回实时快照。

**一处被否决的改动**（留档避免重犯）：曾把 `self.git` 改成惰性求值，想省掉构造时那次
`git status`（234ms）。`test_robustness.py::test_slow_git_does_not_block_other_rpcs`
立刻抓到——`thread/detail` 会在自己第一次访问 `.git` 时才同步跑 git 子进程，
把 200ms+ 阻塞搬到服务线程热路径，实测卡 1.6 秒。已回滚，代码里留了注释。

## 未验证 / 未做

- **那条 600 秒命令的原文**：已丢失，只能按形状推断为较大范围的 pytest。
- **全量套件的单次总耗时**：这台机器上单进程跑满 10–13 分钟不返回。改用**串行分段**
  （每段 12 个文件、独立超时）才能拿到确定结果——xdist 在本机不可用（见下）。
- **pytest-xdist 在本机不可用**：装得上，但 worker 起不来，连单个文件都报
  `EOFError: expected 1 bytes, got 0`（execnet 启动阶段）。已卸载，不留在环境里。
  提速需要另找方案（拆分 / 减少真实 sleep），本次未做。
- 每个 `ee exec` 任务有 10–13 秒启动开销（`import openai` 约 3s + 建 HTTP 客户端/TLS 约 3s +
  其他导入）。桌面端后端是长驻进程只付一次；评测每任务起新进程所以每次都付。未优化。
- ~~真实 API key 的端到端实测~~：**已补上**。2026-10-29 用真实 key 跑了三轮
  （见文首「真实 key 实测」），闸门确认生效：600.3s → 180.3s（撞上限）→ 67.7s（正常）。
- `tests/test_robustness.py::test_http_status_error_message` 在整段串行跑时偶发
  `WinError 10053`（本机测试 server 的连接被中止）。单独跑 3 次全过、整段连跑 2 轮
  281 passed——属偶发抖动，不是代码缺陷，但会污染全量统计。

## 复现方法

```bash
# 必须在仓库根；用项目自带的 .venv
.venv/Scripts/python.exe ee/scripts/eval_profiles.py \
  --repo "$(pwd)" --tasks 01 --runs 1 --profiles full --max-steps 12 --keep \
  --out .rivet/scratch/eval-01-keep
```

排查墙钟异常时先看日志再下结论：

```bash
# 每次模型请求的真实耗时
grep "模型请求结束" "$APPDATA/ee/logs/ee-backend.log" | tail -20
# 两次请求之间的空档 = 工具执行时间；>500s 就是撞了 shell 上限
```

**跑测试必须用 `.venv/Scripts/python.exe`。** 用系统 `python`（本机是 Anaconda）会因为没有
`openai` 包而出现一串 `ModuleNotFoundError`（`openai_chat.py:110`），看起来像代码坏了，
其实是解释器选错了。同理，`tests/test_eval_profiles.py::test_default_ee_cmd_does_not_depend_on_cwd`
在系统 Python 下会失败，因为那个解释器旁边没有 `ee.exe`——这不是缺陷，是环境选错。
