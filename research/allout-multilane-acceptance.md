# allout 验收记录（历史与最新补测）

> 本文为各阶段的提交前验收快照；历史“未提交”等描述对应当时状态，收尾后的提交状态以 Git 记录为准。个人数据目录只做元数据一致性检查，不公开其清单或容量。

## 收尾提交前组合验证

本轮50个实现、测试、夹具及报告文件的最终组合快照由bot3完整验证，验证前后SHA256一致，未修改源码，索引为空。统筹随后只更新本节记录。

- 后端全量：**2824 passed / 19 skipped**，154.5s；使用仓库.venv、PYTHONUTF8=1、pytest -n 8。
- 前端全量：**430 passed / 67 files**；renderer/electron类型检查通过，源码构建通过。
- 完整隔离Electron：**38 passed / 3 skipped**；EE_E2E_REAL_MODEL=0，真实模型用例显式跳过，零新增真实API调用。
- `ruff check ee/ scripts/allout_bench.py` 与 `git diff --check` 通过。
- 保留两条已知非阻断warning：asyncio管道析构的closed pipe，以及layoutStore的Vite动态导入提示。
- 本次测试后移除独立pytest basetemp、test-results、dist与dist-electron，共31,060,887文件字节；源码、依赖、验收证据和用户数据保留。

本节记录提交前结果，不将离线完整套件通过等同于所有真实模型分支覆盖；自然失败恢复、fanout内不同补丁择优、正式资源规划下的真模型多槽验收限制仍按下文保留。用户使用源码开发版，本轮不制作安装器、不发布版本。

## 源码开发版最终复核（本轮，以此节为准）

用户长期使用源码开发版，本轮不制作安装包，不迁移、重写或复制真实会话正文。验收通过的是隔离的桌面真实模型首轮闭环与已覆盖的旧格式兼容性，不等于所有失败恢复分支的真实模型验收全部完成。

- 桌面真实模型样本由 bot3 执行；统筹已查看截图和结构化 JSON：`deepseek-v4.1-flash11` / opencode / `deepseek-v4.1-flash`，effort=max，真实工具成功，target 14 项通过、p2p=met、delivery=verified、source=first_try；成功回合52秒，6次请求，记录费用约¥0.0221。这是成功样本记录，不是本轮所有失败尝试的总费用。
- 发现并修复测试实例日志隔离缺口：仅当 `EE_TEST_USER_DATA` 存在时，Electron 的 logs 一并指向临时 userData/logs；后端 EE_LOG_DIR 随之隔离。真实实例的日志路径不变。`launchIsolated.extraEnv` 同时过滤 EE_HOME/EE_TEST_USER_DATA/EE_LOG_DIR，避免额外环境参数覆盖隔离目录。
- 旧会话兼容使用合成 JSONL/ThreadDetail：缺 delivery/usage、旧候选缺 comparisons/资源字段时正常打开、渲染与继续建会话；error/interrupted 不会被当作交付成功，load 不重写旧 JSONL。没有读取用户真实会话正文或执行迁移，也未覆盖所有历史版本的 schema。
- 统筹独立复跑：后端 `test_allout_history_compat.py` **2 passed（0.80s）**；前端 `allout-history-compat.test.tsx` **5 passed**；Electron `isolation.spec.ts` **1 passed（1.7s）**。同批 `real-model.spec.ts` 在 EE_E2E_REAL_MODEL=0 时 **1 skipped**，统筹没有新增付费调用。成员的相关回归为后端13通过、前端33通过，typecheck通过；本轮没有重跑历史全量数字。
- 独立测试前后只读取文件元数据：真实 .ee 与Electron数据目录的文件清单/大小/mtime元数据指纹均一致，stat错误0；本轮未发现真实数据写入。该证据只覆盖独立测试窗口，不是对全部历史内容的审计。
- 仍未覆盖：自然首轮失败→修复→分叉的完整真实模型链路、真实模型生成不同补丁后的fanout内A/B、正式资源规划下的真模型多槽判据。停止/失败显示主要由既有替身用例覆盖；中途代理断流的观察不代替专项断言。没有扩大资源守卫、修改模型容量或为追求覆盖修改判据。

交付证据已从临时目录转存为保留副本：`ee-desktop-bbd0b066.png`、`ee-desktop-bbd0b066.json`，统筹核对 SHA256 相同后才删除临时原件；随本轮报告一起交付。截图不含真实会话正文或密钥。

清理前检查了工作树与进程；移除本轮独立测试目录1428字节、desktop/test-results 45字节、desktop/dist 3525882字节、desktop/dist-electron 70602字节，合计 **3597957 临时/构建文件字节（约3.43MiB）**。证据原件179005字节已转为保留副本，不将转存计为净释放。以上均为文件长度统计，不是文件系统分配空间测量。无本轮残留Electron/Python进程；保留全部源码、测试、可复用驱动、报告、交付证据、依赖工具链、用户配置与历史。源码仍未提交、未发布。


## 本轮桌面真实模型样本记录（隔离修复前的调用证据）

桌面 UI 到真实模型、工具、判据、交付的闭环在**源码开发版**上跑通，用户真实数据未被触碰。
（这是本轮真实模型样本记录；最终隔离修复及统筹复核结果以文首为准。）

- **入口**：仓库 e2e（Playwright + Electron），经 `desktop/e2e/launch.ts` 隔离 `EE_HOME` 与
  `EE_TEST_USER_DATA`；新增 `desktop/e2e/real-model.spec.ts`，仅 `EE_E2E_REAL_MODEL=1` 时运行
  （实测无开关时 1 skipped、零请求、零费用）。
- **凭据**：临时 home 只写非敏感配置（provider/model/family/base_url/budget/permissions/
  `x-opencode-session` 占位符）；真实 key 由后端运行时从系统凭据库解析
  （`config.key_source()` 先查 store，与 `EE_HOME` 无关）。脚本/报告/日志/截图均无密钥。
- **样本**：临时 git 项目 `slugify`（seed 有意不完整；13 条冻结判据 + 1 条 smoke 作为 p2p），
  经真实 UI：选模型 `deepseek-v4.1-flash11` → 开火力全开（上限 2）→ 目标里填 target/p2p → 发送任务。
- **证据（脱敏）**：交付副本 `ee-desktop-bbd0b066.png` 与 `ee-desktop-bbd0b066.json`（临时原件已核对后清理）
  - `delivery.status = verified`、`source = first_try`、target「ok：14 个测试通过」、regression `met`
  - 真实工具 `glob` / `read`×5 / `write` / `shell` 全部 `ok`；项目内 `slugify.py` 被改写（+36/−26）
  - 模型 alias `deepseek-v4.1-flash11` · provider `opencode` · model `deepseek-v4.1-flash` · effort `max`
  - 6 次请求、**≈¥0.0221**（UI 累计同值）、回合 52s、用例总 59.1s
- **UI 与字段一致**：回合尾部「交付已复验」；执行卡片「请求 2 路 → 本轮 2 路（限制：你设置的上限）」，
  且**没有**“实际启动”字样（本轮 first_try 未分叉）。
- **数据隔离**：真实 `~/.ee` 在测试窗口内无新会话、worktrees 仍 5 项；无残留 Electron/后端进程；
  临时 home 与项目已删除。
- **本轮未覆盖**：自然失败→修复→分叉、不同补丁的真实 A/B、并行判据。
  停止/失败展示仍主要靠既有 fake e2e（`interaction.spec.ts` 审批/拒绝路径）；中途两次真实
  `RemoteProtocolError`（系统代理断流）按“出错”收尾，但那是环境错误，未作为失败展示的专项断言。
- **契约小缺口（未改代码）**：`thread/get` 详情没有 `allout_plan`（计划只经通知到前端），
  所以计划口径断言走界面文本；不影响结论。
- **驱动修复**：`launch.ts` 增加可选 `configToml/extraEnv`（默认行为不变）；spec 修正了
  模型菜单 `menuitem` 选择、`getByPlaceholder` 的 `exact`、`thread/get` 的 `{thread}` 形状、
  以及“带 goal 才出现「使用独立判据」”的快速失败断言。


## 最新验收结论（本次补测后，以此节为准）

本文保留多个阶段的历史记录；后文“未覆盖”若与本节不同，指的是该历史阶段。本次补测没有新增产品执行代码修复，新增了并行验收测试、一个基准夹具，并同步了驱动与样本清单测试。

| 验收项 | 最新证据 | 结论 |
|---|---|---|
| 正式规划的真实双路分叉 | 重启后两路模型候选均完成判据，写回、target/p2p、verified 与严格落选回收通过；补丁相同，A/B 正确跳过 | 已有定向真跑证据；bot3 回报，统筹未重复调用模型 |
| 自然失败→修复→仍失败→分叉 | 两个有效样本均在正式 Thread.start_turn 入口首轮 verified；另一个样本因驱动命令错误无效，不计通过 | 本次未触达完整失败恢复链路，不算覆盖 |
| 真实 fanout 内不同补丁的 A/B | 一次驱动 p2p 路径错误使结果无效；修正后一次只有一个可排序候选，另一候选因择优窗口被取消 | 本次未达成；此前手工准备的两个 git 候选位置对调证据不能替代这一项 |
| 并行判据机制 | 模型替身、资源充足硬件夹具、生产 fanout、真实 shell/pytest 判据；事件屏障证实两条判据重叠且不超槽位，失败/取消后槽位释放、进程退出 | 机制集成通过；不等于本机正式规划下的真模型双槽验收 |
| 当前采样时的正式并行规划 | 16核、可用内存约7.85–8.01GB；请求2路或6路时，parallel_validation 开/关均为 judge_slots=1 | 正确按资源降级；没有放宽资源守卫凑覆盖 |

统筹独立复跑：

- `ee/tests/test_allout_parallel_validation.py`：**6 passed，15.11s**；无真实模型调用。
- `ee/tests/test_allout_bench.py`：**8 passed，2.96s**。先前的一个失败是新增 `05_slugify_hard` 后，旧断言仍要求6夹具/12样本；现在固定验证7个合法夹具、14个样本及两种模式，保留 dry-run 零模型调用路径。
- `ruff check ee/ scripts/allout_bench.py` 与 `git diff --check`：通过。产品执行代码未因本次补测变化，没有重复历史全量/前端/Electron测试；历史全量数字不代表包含这批新测试。
- 本次没有确认新的产品执行缺陷；自然完整链路与真实分歧择优仍有样本覆盖缺口，不能宣称全部实机验收通过，亦没有质量/性能提升幅度的对照结论。

真实调用费用：成员可记录用量约 **¥0.073**；另一个超时样本未返回完整 usage，费用**未核实**。其配置会话预算 ¥0.4 只能作预算参考，不能当作实际扣费或严格总费用上界。本次统筹复核零模型调用。

清理：先检查工作树与进程，统筹已删除并验证不存在的本轮独立测试目录为 `%TEMP%/ee-codex-parallel-mwj9o54c`（190499字节）、其 `-bench` 目录（865字节）、`%TEMP%/ee-codex-bench-final-mwj9th8m`（865字节），共 **192229 文件字节**。成员另报告清理其本轮临时目录；未统计或删除归属不明的既有缓存。保留全部源码、测试、夹具、可复用驱动、报告、依赖、配置、凭据与用户数据。工作仍未提交、未发布。


复现命令（在仓库根，凭据由 `load_config` 运行时解析，脚本不打印也不落盘任何密钥）：

```powershell
$env:PYTHONUTF8='1'
./.venv/Scripts/python.exe ee/scripts/allout_multilane_probe.py --json out.json
```

## 这是什么，不是什么

**是**：真实默认模型、真实 git 仓库与 worktree、真实判据、真实 `side_complete`，
直接驱动生产函数 `Thread._plan_allout` / `_allout_fanout` / `_allout_rank` /
`_cleanup_allout_losers` / `allout.candidate_patch` / `apply_patch` /
`Thread._delivery_gate`。

**不是**：自然端到端。没有"首轮实现失败 → 批评修复 → 分叉"这条真实链路；脚本直接进分叉。
也不 mock 任何模型响应、不改验收文件、不为通过而放大择优窗或覆盖 plan。

## 历史记录：重启前定向验收（脱敏）

| 项 | 值 |
|---|---|
| 模型 | alias `deepseek-v4.1-flash11` · provider `opencode` · model `deepseek-v4.1-flash` |
| 上下文来源 | `family=deepseek` 的内置档案继承，`context_window=1000000`（config 未显式写；**不是**缺失） |
| 计划（生产 `_plan_allout` → `derive`） | **1 路** · `limiting=resource` · judge_slots=1 · effort=max |
| 择优窗 | base 30.0s / hard 60.0s（无历史判据耗时） |
| fanout 实际 | planned=1 · records=1 · with_verdict=1 · cancelled=0 · 37.3s |
| 该候选 | `met=true` · 2 个测试 · `regressed=false` · 成本 0.0035 |
| 定向 A/B（2 个真实 git 候选、不同补丁） | patch_sha 不同 · 净胜 **+2 / −2** · `comparisons` **1 / 1** · `fair_pairs=[["ab-1","ab-2"]]` |
| 写回 | patch=`calc.py` · applied=true · expected_ok=true · `unexpected=[]` |
| 交付闸门 | **gate=met / status=verified** · target「ok：2 个测试通过」· p2p「ok：1 个测试通过」· acceptance 未变 |
| 落选回收 | `removed=["ab-2"]` · 落选目录已删 · **分支保留** · 胜者目录保留 · kept=[] |
| 测试保护 | 运行前后既有测试文件指纹一致（`protected_tests=2`，扫描完整） |
| 用量（估算） | requests 7 · prompt 13,415 · completion 1,043 · **约 ¥0.0085**（provider 未报价的请求不计入） |
| 墙钟 | 75.9s（含 A/B 与两次交付复验） |
| 清理 | 临时仓库 / home / worktree 全部删除；未触碰用户 `~/.ee` |

### 2026-11-16 重启后：正式规划 **2 路** 补测（仍为定向集成）

重启后可用内存 8.7 GB，同一套生产代码、同一份默认配置（无任何覆盖）直接得到 2 路：

| 项 | 值 |
|---|---|
| 计划（`_plan_allout` → `derive`） | **2 路** · `limiting=user`（请求上限 2） · judge_slots=1 · explore_slots=1 · 窗口 30s/60s |
| fanout 实际 | planned=2 · records=2 · with_worktree=2 · **with_verdict=2** · cancelled=0 · 47.4s |
| 候选 | cand-1 `met=true` 2 测试 ¥0.0053 / cand-2 `met=true` 2 测试 ¥0.0085；均 `regressed=false` |
| 排序 | 两个候选都通过；补丁字节相同 → A/B 按设计跳过（`fair_pairs` 仍给出 1 对，`comparisons=0/0`），按客观指标选 cand-1 |
| 写回 / 交付 | `calc.py` applied · expected_ok · `unexpected=[]` · **gate=met / verified** · target「2 个测试通过」· p2p「1 个测试通过」 |
| 落选回收 | `removed=["cand-2"]` · 目录已删 · **分支保留** · 胜者目录保留 · kept=[]（新 strict 闸门下的真实回收） |
| 用量（估算） | 14 次请求 · prompt 33,036 · completion 2,249 · **约 ¥0.0176** · 墙钟 91.1s |
| 清理 | 临时仓库/home/worktree 全删；未触碰 `~/.ee/worktrees` |

范围说明：这补上了“正式规划真的批出 2 路 + 真跑 2 个候选 + 写回/交付/回收”这段；
仍是**定向集成**（直接驱动 `_allout_fanout` 起分叉），不是“首轮失败→修复→分叉”的自然端到端。
两个候选产出同字节补丁，真实 A/B 分歧仍由本文上面的定向 A/B（不同补丁，±2 / 1 组）覆盖。

## 历史阶段结论（对应重启前结果）

1. **本机当前批不出 2 路**：实测可用内存 4.5 GB，`derive` 按「每路 2 GB」估算得到
   `(4.5-2)//2 = 1`，于是 `candidates=1`、`limiting=resource`。这是设计内的正确降级，
   不是缺陷；要跑真实 2 路 fanout 需要可用内存 ≥ 6 GB。脚本**没有**覆盖该值。
2. 因此"多路"这一环用**两个真实 git 候选**走同一批生产函数验证：
   `_allout_rank` 的公平配对、位置对调两次投票、`comparisons` 计数、胜者写回、
   交付复验、落选目录回收（分支保留）都真跑通了；但它**不等价于** 2 路 fanout。
3. 改善空间（需产品/测量决策）：`per_lane_gb` 下限固定 2 GB、只升不降，导致
   "16 核 + 4–5 GB 可用" 的机器常态单路。可考虑按判据命令实测 RSS 估算，或让用户
   显式设置并行路数预算（而不是只按内存推算）。
4. **Windows 行尾边界（已修，附离线证据）**：`acceptance` 用字节哈希比对既有测试。
   主工作区是 CRLF、git blob 与 checkout 是 LF 时（没有 `.gitattributes` 的普通 Windows
   工程，`core.autocrlf=input` 就会这样），`git status` 干净但字节不同——旧实现拿主工作区的
   `_accept0` 去比候选，于是**每个候选都被判「改了既有测试」**，多路根本跑不起来。

   修法（不是归一化文本，也不是只看 git status）：候选在「共享不可变 baseline 建好、
   模型还没跑」时采集**自己的**验收字节基线，之后只对这份基线逐字节比对；主工作区仍用
   `_accept0` 与最终交付闸门复验。基线不完整（受保护测试缺失/不可读/扫描截断）时
   `acceptance.baseline_problem` 返回原因，候选直接判不合格——不重设空基线假装完整。

   证据：`ee/tests/test_allout_acceptance_baseline.py`（6 例，真实 git 夹具，
   `core.autocrlf=input`、CRLF 工作区 + LF blob、`git status` 干净）——
   ①未动测试的候选不再误报；②真改写既有测试仍拒绝；③真删除既有测试仍拒绝；
   ④基线缺失受保护测试时 fail-closed（且不跑判据）；⑤子目录工作区的验证文件映射既
   不误报也不漏报；⑥同一夹具上「主工作区基线比候选」仍会失败（反证用例不是空转）。
   主工作区被外部改动仍走 `needs_review`，由既有 `test_allout_delivery::test_protected_test_change_blocks_met` 守着。

## 最终变更与验收记录（2026-11，本轮收尾）

只动了这一处产品边界，没有改默认模型、模型容量、未知上下文限制或资源守卫：

| 文件 | 变更 |
|---|---|
| `ee/ee/core/acceptance.py` | 新增 `baseline_problem()`：判定候选自己的基线能否用于逐字节比对（空基线、扫描截断、受保护测试/验证文件缺失或不可读都算不可用，返回人话原因） |
| `ee/ee/server/threads.py` | `_allout_fanout.run_one` 在 worktree 建好、模型启动前采集**候选自己的**验收基线；基线不可用 → 该候选 `regressed` + `baseline_error` 并收尾，不跑判据。`acceptance_intact(cwd, base)` 改为对该基线比对；主工作区仍是 `_accept0` + `_delivery_gate` |
| `ee/tests/test_allout_acceptance_baseline.py`（新） | 6 例真实 git 夹具回归（见上文第 4 条） |
| `research/allout-multilane-acceptance.md` | 本节 + 把复现命令改成真实 PowerShell 写法 |

验证（离线，未再花模型费用）：

```text
相关子集（acceptance_baseline / isolation / run / delivery / rank / selection / delivery_expected）
=> 86 passed
后端全量  PYTHONUTF8=1 ./.venv/Scripts/python.exe -m pytest -c ee/pyproject.toml -q -n 8
=> 2804 passed, 18 skipped（本轮安全收口前的记录；当时仅含验收基线修复）
```

## 自动回收的安全收口（2026-11，最终）

bot1 的独立审计（`channel-result-bd3ab3d8`）证明旧的 strict 删除会误删数据。本轮只修阻断项，**没有新增任何自动删除路径**，也没有改默认模型/资源守卫。

| 风险 | 现在的行为 |
|---|---|
| 登记过期、目录被换成同 HEAD 的外来 repo/clone | `git rev-parse --git-common-dir` 两边一致才算同裔仓库；再用 `git worktree list --porcelain` 的**完整块**证明它是成员。linked worktree 的 `--show-toplevel` 是它自己，不拿它和主仓库路径比 |
| worktree 内被切到别的分支 | list 块的 `branch refs/heads/...`、target 自己的 `symbolic-ref`、收尾记录三者一致才继续 |
| HEAD 在收尾后变化 | list 块的 HEAD、target 的 `rev-parse HEAD`、收尾保存的 head 三者一致 |
| worktree 被 locked | 从 list 块的 `locked` 行判定（不再用 `lock_reason(None)` 的模糊语义） |
| `git worktree remove` 失败 | `check=True`，失败只保留并给原因；**已删除 `shutil.rmtree` fallback**（手动入口也不再硬删） |
| `git status` 读不到 | `W.status` 增加 `status_ok`/`error`，失败绝不报“干净”；cleanup 要求 `status_ok` 才继续 |
| ignored 的目录 / 嵌套 git / 符号链接 | 一律进 `skipped` → 拒删；只有判据自己生成的 `__pycache__` / `.pytest_cache`（无嵌套 `.git`、无 symlink、条目有界）允许丢弃 |
| 依赖目录（`.venv` / `node_modules`…） | 严格路径不按名字放过（目录与其普通文件都保留）；手动删除维持旧语义（需显式 `discard_unbacked`） |
| 未跟踪的 `*.tmp` 样文件 | 严格路径不再套用 `_TMP_RESIDUE` 按名豁免 |
| `git worktree add` 成功但 `_save` 失败 | 立即 `git worktree remove --force` + `git branch -D` 回收本次创建；回滚失败把路径/分支/原因写进异常并记录 |
| `create_for_agent` 同步脏改动失败且回滚也失败 | 不再 `pass`：异常里写清目录/分支，并记录 `sync_dirty_rollback` |
| `prepare_dirty_base` 的临时 worktree 删不掉（含外层被取消） | 结果里带 `cleanup.error`，并记录 `prepare_dirty_base_cleanup`（日志 + 进程内可见列表） |

离线证据：`ee/tests/test_worktree_safety.py`（13 例，真实 git 夹具 + 失败注入）——
同 HEAD 外来 clone、真实切分支、ignored 嵌套 git、ignored 符号链接（本机无权限时 skip）、
`git status` 失败、`git worktree remove` 失败后目录完整、`*.tmp` 样未跟踪文件、
安全缓存允许丢弃、普通 ignored 文件先备份后删、`_save` 失败回滚、
同步失败回滚可见、baseline 删除失败可见、正常落选只删目录保留分支。

最终离线结果（含上述安全收口 + 验收基线修复）：

```text
PYTHONUTF8=1 ./.venv/Scripts/python.exe -m pytest -c ee/pyproject.toml -q -n 8
=> 2816 passed, 19 skipped in 150.26s（1 条已知 asyncio 管道析构 warning）
./.venv/Scripts/python.exe -m ruff check ee/ee ee/tests
=> All checks passed
```

该安全修正阶段尚未补测的边界（后续进展以文首最新结论为准）：

- 源码**全部未提交**（未 commit、未切分支、未改索引/历史、未改用户配置）。
- 上文的真实模型调用是**定向集成**（旧代码版本）；本轮**未**用最新代码重跑真模型，
  也不声称历史真跑结果等于当前代码的实测。
- 正式配置下 fanout **实际只有 1 路**（本机可用内存 4.5 GB → 资源守卫）；
  真实 2 路 fanout、自然 e2e、`judge_slots>1` 的并发判据**都未覆盖**。
- 没有性能收益的保证：本轮没有做计时对比，也没有宣称更快。
- 安全回收仍是“策略层 + git 事实”的保守判定：存在 ignored 目录/依赖目录时宁可保留整个
  worktree 让人自己确认；这是有意的取舍，不是漏实现。

## 2026-11-16 收尾补测：自然链路（A）与分歧 A/B（B）

环境：重启后可用内存 8.7 GB，生产计划稳定给出 2 路（未改模型/资源守卫）。
真实模型 `deepseek-v4.1-flash11`（opencode，effort=max），全部隔离 toy 仓库 + 临时 home，
`persist=False`，未触碰用户 `~/.ee` 会话与 worktree。

### A. 自然完整链路：**未触达 fanout**（如实记录，不算通过）

走的是正式入口 `Thread.start_turn(text, goal=..., allout={enabled:true,candidates:2})`，
判据在运行前冻结；两个有效样本都是首轮通过；另一个命令错误的样本不计证据：

| 样本 | 结果 | 事件证据 | 修复/fanout | 成本 | 墙钟 |
|---|---|---|---|---|---|
| `03_wrong_approach` | `solved` / `verified`（`source=first_try`） | 1 次 `judge` 即 `met`（2 测试） | 无 | ¥0.0069 | 35.0s |
| `05_slugify_hard`（新夹具，判据在仓库外） | `solved` / `verified`（`first_try`） | 1 次 `judge` 即 `met`（13 测试） | 无 | ¥0.0189 | 83.7s |

另一个样本因**我的**判据命令在 PowerShell 下少 `&`（判据根本没执行）而超时，已修并在
`scripts/allout_bench.py` 里改成与其他命令同一调用写法；该样本不计入证据。
结论：模型首轮就能满足这类冻结判据 → **自然“首轮失败→修复→仍失败→分叉”没有发生**。

### B. fanout 真实分歧 A/B：**未达成**（精确失败证据）

用新夹具 `05_slugify_hard`（多种合法实现，降低同补丁概率）做定向 fanout，未手工改补丁：

| 尝试 | planned / records | 实际产出 | 结果 |
|---|---|---|---|
| 1 | 2 / 2 | 两候选都 `met`（6 测试） | **驱动 bug**：p2p 指向了 calc 夹具的 `tests/test_regression.py`（该项目没有）→ 两者被判 `regressed`。已修：p2p 按夹具选 `tests/test_smoke.py` |
| 2 | 2 / 2 | `with_verdict=1`、`cancelled=1` | 第二个候选在 30s 择优窗内未交卷被取消 → 只有 1 个可排序候选，**A/B 未发生**（无 `comparisons`）。单候选仍完成了写回、target+p2p、交付 `verified`（¥0.0253 / 14 请求 / 102.9s） |

没有通过手工改候选补丁、扩大择优窗、改温度阶梯或跳过判据来凑覆盖。

**本次观察（不代表普遍概率）**（非缺陷）：`judge_slots=1` + 默认 30s 择优窗下，本次第二个有效尝试的两路候选
只有一个能在窗口内交卷，因此分歧 A/B 自然出现的概率很低。要稳定覆盖它，需要提高
`judge_slots`（并行验收课开关）或把窗口按本轮判据耗时自适应得更宽——两者都是已设计好
但尚未在真跑中验证的路径。

### 本轮成本与产物

- 可计量费用：¥0.0069 + ¥0.0189 + ¥0.0220 + ¥0.0253 ≈ **¥0.073**；另有一个超时样本未返回
  usage，其配置会话预算为 ¥0.4，仅能作预算参考，不能证明实际扣费上界；本轮总费用未核实。
- 新增/修改：`scripts/allout_bench.py`（保留完整事件证据 + PowerShell `{python}` 调用写法 +
  外置 `judge/` 复制）、`ee/scripts/allout_multilane_probe.py`（`--task slugify` /
  `--no-directed-ab` / 真实候选补丁指纹 / p2p 按夹具）、新夹具
  `research/bench/allout/05_slugify_hard/`（公开 2 例、判据 13 例、hidden 同判据）。
- 未改产品代码，所以没有重跑后端全量；脚本 `ruff` 通过。

## 未覆盖

- 自然端到端（首轮失败 → 修复 → 分叉 → 写回）：本轮实测**未触达**（见上）。
- 两个候选补丁**不同**时的真实 fanout 内 A/B（本轮两次都没拿到两个可排序候选）。
- 并行验收开关（`parallel_validation`）下的 `judge_slots>1` 并发判据。

## 历史记录：首轮统筹复核与清理

Codex 对最终工作区独立复核（无真实模型调用、无源码改动）：

- 定向回归：`test_worktree_safety.py`、`test_allout_acceptance_baseline.py`、`test_bestof_fairness.py`、`test_allout_delivery.py`、`test_allout_selection.py`，结果 **58 passed / 1 skipped，22.71s**。跳过项需要 Windows 符号链接权限。
- `ruff check ee/`：通过；`git diff --check`：通过。
- 全量后端 **2816 passed / 19 skipped**、前端 **425 passed**、Electron **37 passed / 2 skipped** 为成员执行记录；统筹没有重复全量或重复真实模型调用。
- 真实模型结果仍以本文历史定向验收为准；最终安全修正后没有重新调用模型。正式资源规划下真实 2 路分叉、自然端到端与并行判据仍未覆盖，未证明质量或性能提升幅度。
- 检查工作树与运行进程后，确认以下路径为本轮临时测试/生成构建产物、无已跟踪源码或运行进程使用，已删除并验证不存在：
  - `%TEMP%/ee-codex-final-mwj81at8`：1,803,374 字节。
  - `desktop/dist`：3,525,882 字节。
  - `desktop/dist-electron`：70,188 字节。
- 本次最终清理合计 **5,399,444 文件字节（约 5.15 MiB）**；这是文件长度合计，非文件系统分配空间实测。构建已验证通过，生成物随后按任务清理要求移除，需再次运行桌面构建时可重新生成。
- 清理前后源码/测试/脚本的工作树状态一致；保留全部实现、测试、此报告、可复用验收驱动、依赖工具链、配置、凭据、用户数据及归属不明的既有缓存。未创建提交、未发布、未切分支或修改主仓库索引/历史。
