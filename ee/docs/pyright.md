# pyright 配置说明（`pyrightconfig.json`）

CI 门禁仍是 `ruff` + `pytest` + `vitest` + `tsc`；pyright 只用于 IDE / pi-lens 的类型提示，
不阻塞构建与发布。这份文档解释配置为什么长这样，以及当前的类型债基线。

## 为什么需要这份配置

1. **`import ee` 要能解析**：包在 `ee/ee/`（导入名 `ee`），仓库根不是包根。不配
   `executionEnvironments` 时，`from ee import __version__` / `from ee import log`
   等会报 "unknown import symbol"——不是代码错误，是搜索路径没配。
2. **用仓库根的 `.venv`**：否则 `openai` / `httpx` / `pypdf` / `tree_sitter` 全部
   "找不到模块"，它们的类型信息一丢，下游会连锁报出一批假的属性/参数错误。
3. **只查产品代码**：`ee/scripts/`（排障/复现脚本）与 `ee/tests/`（大量故意不
   合类型的假对象）不进 `include`，否则测试替身的噪声会淹没真问题。
4. **`desktop/` 不归 pyright 管**：那边是 TypeScript，由 `tsc --noEmit` 负责。

## 严重度与当前基线

**当前基线：`pyright` 在产品代码（`ee/ee`）上是 0 error / 0 warning。** 四条曾降级为
warning 的规则已经提回 `error`（配置里不再有 override），以后再新增同类问题会直接标红：

| 规则 | 说明 |
|---|---|
| `reportAttributeAccessIssue` | 动态属性（子代理注入、平台专用 API `os.killpg` 等） |
| `reportOptionalMemberAccess` | 运行时有兜底的 `x.get(...)`（已改成显式窄化） |
| `reportOptionalIterable` | 同上 |
| `reportUnusedExpression` | 风格类 |

`reportPossiblyUnboundVariable` 一直保持 error——它是最容易变成运行时 NameError 的一类。

清理过程中改的都是「不改变运行时行为」的写法：显式 `isinstance`/`None` 分支、局部变量窄化、
把 `**kw` 换成具名参数、给动态属性在 `__init__` 里声明默认值。每批都跑了对应的测试。

## 怎么跑

```bash
pyright                                  # 按配置跑产品代码（需要 pyright 在 PATH）
pyright ee/ee/mcp/config.py              # 只跑一个文件
pyright --outputjson > out.json          # 机器可读，便于按规则统计
```

## IDE / pi-lens 里的同一条口径

本仓库的类型基线只覆盖 `ee/ee`。但 pi-lens 是按文件问 LSP 的，会绕过 `pyrightconfig.json`
的 `exclude` 去查 `ee/tests/*`——那些测试替身本来就故意不合类型（`backend.threads[x]`、
`x = None` 先赋值后注入），会把面板刷成十八九条阻断项。为了让面板和本文件的口径一致，
根目录 `.pi-lens.json` 把这些 pyright 规则在 `ee/tests/**` 上关掉：

```json
"rules": {
  "reportGeneralTypeIssues":     { "ignorePaths": ["ee/tests/**"] },
  "reportOptionalMemberAccess":  { "ignorePaths": ["ee/tests/**"] },
  "reportOptionalOperand":       { "ignorePaths": ["ee/tests/**"] },
  "reportOperatorIssue":         { "ignorePaths": ["ee/tests/**"] },
  "reportAttributeAccessIssue":  { "ignorePaths": ["ee/tests/**"] }
}
```

这不是把 pyright 关掉：`ee/ee` 上的诊断照旧报（`pyrightconfig.json` 的 include 不变）。
新出现的 pyright 规则仍会在测试里报一次——那说明它是新规则，判断后再决定要不要加进来。

基线会随代码变化；上面的 0/0 是清完存量后的快照，不是长期承诺。新增诊断应当当次修复，
不要把 override 加回去当止痛药。
