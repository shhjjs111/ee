# ee 项目说明

Windows 桌面 AI 编程 Agent：Python 内核（`ee/`，`ee serve` 起本地 WebSocket JSON-RPC）+ Electron/React 桌面端（`desktop/`）。只支持 OpenAI Chat Completions 协议，中文界面与交流。详细路线见 `research/roadmap-current.md`。

## 跑测试（重要：全量很慢）

**全量 pytest 约 17 分钟（135 个测试文件，实测 1064 秒）**，单次 shell 命令有超时上限，跑全量会超时白等。

一律跑相关子集：

```bash
# 单个测试文件（最常用）
./.venv/Scripts/python.exe -m pytest -c ee/pyproject.toml ee/tests/test_xxx.py -q

# 按关键字过滤
./.venv/Scripts/python.exe -m pytest -c ee/pyproject.toml -k truncate -q

# 多个相关文件
./.venv/Scripts/python.exe -m pytest -c ee/pyproject.toml ee/tests/test_a.py ee/tests/test_b.py -q

# 只看收集到的用例，不执行
./.venv/Scripts/python.exe -m pytest -c ee/pyproject.toml --co -q
```

先改哪个模块，就跑它对应的 `ee/tests/test_<模块>.py`；不确定测试文件叫什么时，先用 glob/grep 找 `**/*<关键词>*.test.py`。

## 环境

- **必须用仓库根的 `.venv`**（`./.venv/Scripts/python.exe`）。PATH 上的 `python` 是 Anaconda，缺 `openai`/`tree_sitter`，会产生大量假失败。
- **测试不需要 `pip install -e`**：`ee/tests/conftest.py` 已经把仓库根加进 `sys.path`，直接跑 `pytest -c ee/pyproject.toml` 即可。pytest / openai / tree-sitter 等依赖 `.venv` 里已有；`pip install -e "ee[test]"` 会联网、很慢（实测撞过单次 shell 超时），而且不装也能测。
- 前端：`cd desktop && npx vitest run <文件>`；类型检查 `npm run typecheck`。
- 行尾：仓库用 LF（`.gitattributes` 统一），新增文件不要留 CRLF。

## 验证（CI 已恢复自动）

仓库已公开（2026-10-30），公开仓库的 GitHub Actions 永久免费，`push`/`pull_request` 自动触发。
本地仍可手动验证（无网络/想快速跑时）：

```bash
./scripts/verify.sh           # 全量：后端全量 pytest + 前端 vitest + typecheck
./scripts/verify.sh --quick   # 快速：只跑改动相关的后端测试 + 前端全量 + typecheck
```

`ee/tests/test_ci_offline_gate.py` 会守着这件事：**自动触发或本地等价入口，二者必须有其一**。

## 代码约定

- 权限是硬边界：只读/计划模式不得写文件；项目级 `.ee/config.toml` 只能收紧安全设置，不能放宽。
- 改共享能力（工具签名、事件字段、RPC）后 grep 所有消费方，同步后端 + 前端类型。
- 测试通过不等于行为正确：UI 改动需要真实渲染验证；性能结论需要可测指标。
