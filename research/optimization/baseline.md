# ee 优化基线（Wave 0 / T01）

本文件记录本次优化开始时的真实环境与验证状态，供后续每波对比。数字均来自本轮工具执行，不是文档转述。

## 采集时间与代码状态

| 项 | 值 |
|---|---|
| 采集轮次 | 计划批准后第一波（Wave 0） |
| HEAD | `ca107a112ed7d711f31ac8012644e09b59f0f9ea` |
| 分支 | `main` |
| 会话目录 | 仓库根 `E:\ee建设\ee-main` |
| 工作树状态 | **不干净**，见下节 |

### 工作树未提交改动（非本计划产生）

| 路径 | 状态 | 归属判断 |
|---|---|---|
| `desktop\package.json` | 修改：devDependencies 增加 `@playwright/test` | 其他会话在途工作 |
| `desktop\package-lock.json` | 修改：+46 行，对应上条 | 其他会话在途工作 |
| `desktop\src\lib\layout.test.ts` | 未跟踪 | 其他会话在途工作 |

处置：本计划**不修改、不暂存、不提交、不清理**这三个文件。本计划新增文件与它们无交集。若后续需要运行全量前端测试，以现状为准并在报告中说明。

## 运行环境

| 项 | 值 |
|---|---|
| 平台 | Windows 11（10.0.26200） |
| Python | 3.12.15（仓库 `.venv`：`./.venv/Scripts/python.exe`） |
| openai SDK | 3.39.0 |
| Node | v24.22.1 |
| npm | 12.2.1 |
| Python 依赖清单 | `ee\pyproject.toml` |
| 前端依赖清单 | `desktop\package-lock.json`（当前含未提交改动） |

## 基线验证结果

| 范围 | 命令 | 结果 | 回执 |
|---|---|---|---|
| 后端定向（前缀缓存 / Goal 控制器 / 预算隔离） | `./.venv/Scripts/python.exe -m pytest -c ee/pyproject.toml ee/tests/test_prefix_cache.py ee/tests/test_goal_runner.py ee/tests/test_budget_and_isolation.py -q` | **25 passed**，exit 0，39.62s | `28b0beb7-4cec-4c38-86ab-c6ee2d4c1990` |
| 前端定向（store / reduce） | `npx vitest run src/lib/store.test.ts src/lib/reduce.test.ts`（cwd=`desktop`） | **12 passed**（2 文件），exit 0 | `4e14a3eb-bee2-4133-b7f4-21ed873df52e` |

未在本波执行：全量后端套件（约 17 分钟，按计划留到 Wave 7 发布验收）、真实模型付费评测（需 key，按计划手动执行）。

## 五条人工路径基线

这些路径需要原生桌面交互，本波**未执行**，仅登记预期与观察方式。实施者完成对应修复后逐条补实测。

| # | 路径 | 预期可见行为 | 观察方式 | 当前状态 |
|---|---|---|---|---|
| M1 | 发送消息并流式输出 | 立即出现“正在工作”，正文逐步出现，输入框仍可编辑 | 桌面端手工操作 + 截图 | 未执行 |
| M2 | 长任务中插话 | Enter 进插话队列，提示可撤回；不打断当前工具 | 手工操作 | 未执行 |
| M3 | 停止 | 立即出现停止反馈，最终显示真实中断而非“完成” | 手工操作 | 未执行 |
| M4 | 切换会话 | 两个会话各自保留草稿、状态、计时；不串内容 | 手工操作 | 未执行 |
| M5 | 重启后恢复 | 会话可打开，历史与费用不翻倍，运行中的回合显示为中断 | 重启应用 | 未执行 |

## 已知未验证项

- 计划中标注「待验证假设」的 5 项（回退后“见上文”、collected+skipped 误判、前端逐 delta 卡顿量化、修剪成本取舍、证据缓存收益）在本波仍未实测，Wave 0 的测试会开始覆盖前两项。
- 前端全量测试因工作树在途改动，未作为基线运行。
