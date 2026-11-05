# ee 项目说明

Windows 桌面 AI 编程 Agent：Python 内核（`ee/`，`ee serve` 起本地 WebSocket JSON-RPC）+ Electron/React 桌面端（`desktop/`）。只支持 OpenAI Chat Completions 协议，中文界面与交流。详细路线见 `research/roadmap-current.md`。

## 跑测试（重要：串行很慢，默认加 -n）

**全量串行约 17 分钟（135 个测试文件，实测 1064 秒）**，单次 shell 命令有超时上限，跑全量会超时白等。
**加上 `-n 8` 并行后全量约 150–165 秒（实测两轮 2132 passed / 0 failed）。**

`-n` 是 pytest-xdist 的进程数，装不装它都行（没装就串行，行为不变）。本机 16 核用 **8** 而不是 `auto`：
测试里有 PowerShell 5.1 子进程（单次约 5s），16 路并发会把 10s 级的 `wait_for` 等超时——
实测 `-n auto` 会红 2 条（`test_server` 的 websocket 闭环、`test_bgshell_guidance`），`-n 8` 两轮全绿。
再少核的机器上 xdist 自己会封顶到 CPU 数，不会反而变慢。

**仓库路径带中文时必须加 `PYTHONUTF8=1`**（2026-11-05 实测）：Windows 上 Python 默认用 ANSI
代码页（cp936）处理 execnet 传给 worker 的路径，`E:\ee建设\...` 这类路径在 worker 启动阶段
就被破坏，报 `EOFError: expected 1 bytes, got 0`（错误里能看到 `E:\ee????\...`）。此时
`-n` 任何值都跑不起来，只能串行（17 分钟）。加上 `PYTHONUTF8=1` 后恢复正常：全量
**2261 passed / 15 skipped，131 秒**。`scripts/verify.sh` 里已经带上这个变量。

```bash
# 单个测试文件（最常用）
./.venv/Scripts/python.exe -m pytest -c ee/pyproject.toml ee/tests/test_xxx.py -q

# 按关键字过滤
./.venv/Scripts/python.exe -m pytest -c ee/pyproject.toml -k truncate -q

# 多个相关文件（文件多时值得加 -n：8 个文件的 130 例 121s → 23s）
./.venv/Scripts/python.exe -m pytest -c ee/pyproject.toml ee/tests/test_a.py ee/tests/test_b.py -q -n 8

# 全量（务必带 -n）
./.venv/Scripts/python.exe -m pytest -c ee/pyproject.toml -q -n 8

# 只看收集到的用例，不执行
./.venv/Scripts/python.exe -m pytest -c ee/pyproject.toml --co -q
```

小批量（单个文件、12 例这种）加 `-n` 收益很小（17.8s → 14.5s，启动 worker 有开销），
文件多了才有意义。`./scripts/verify.sh` 已按 CPU 自动选 worker 数，直接用即可。

先改哪个模块，就跑它对应的 `ee/tests/test_<模块>.py`；不确定测试文件叫什么时，先用 glob/grep 找 `**/*<关键词>*.test.py`。

## 环境

- **必须用仓库根的 `.venv`**（`./.venv/Scripts/python.exe`）。PATH 上的 `python` 是 Anaconda，缺 `openai`/`tree_sitter`，会产生大量假失败。
- **测试不需要 `pip install -e`**：`ee/tests/conftest.py` 已经把仓库根加进 `sys.path`，直接跑 `pytest -c ee/pyproject.toml` 即可。pytest / openai / tree-sitter 等依赖 `.venv` 里已有；`pip install -e "ee[test]"` 会联网、很慢（实测撞过单次 shell 超时），而且不装也能测。
- 前端：`cd desktop && npx vitest run <文件>`；类型检查 `npm run typecheck`。
- 后端 lint：`ruff check ee/`（规则在 `ee/pyproject.toml`，只检「代码有误」类：语法、未定义名、未用导入/变量）。
- 真实 Electron 的 Playwright：`cd desktop && npm run build && npx playwright test`（约 1 分钟）。要改界面才必须跑，但它守的是 jsdom 测不出来的东西（布局、滚动链、真实回合闭环）。**必须经 `e2e/launch.ts` 启动**，否则会改用户真实的 `~/.ee`。
- 行尾：仓库用 LF（`.gitattributes` 统一），新增文件不要留 CRLF。

## 验证（CI 已恢复自动）

仓库已公开（2026-10-30），公开仓库的 GitHub Actions 永久免费，`push`/`pull_request` 自动触发。
本地仍可手动验证（无网络/想快速跑时）：

```bash
./scripts/verify.sh           # 全量：后端 lint + 后端全量 pytest + 前端 vitest + typecheck
./scripts/verify.sh --quick   # 快速：后端 lint + 只跑改动相关的后端测试 + 前端全量 + typecheck
./scripts/verify.sh --e2e     # 再加真实 Electron 的 Playwright（会先 npm run build）
```

`ee/tests/test_ci_offline_gate.py` 会守着这件事：**自动触发或本地等价入口，二者必须有其一**。

## 代码约定

- 权限是硬边界：只读/计划模式不得写文件；项目级 `.ee/config.toml` 只能收紧安全设置，不能放宽。
- 改共享能力（工具签名、事件字段、RPC）后 grep 所有消费方，同步后端 + 前端类型。
- 测试通过不等于行为正确：UI 改动需要真实渲染验证；性能结论需要可测指标。
