# ee 后端（Python 内核）

这是 ee 桌面端的内核和本地服务。平时不用直接打交道：桌面端启动时会自动拉起它。没有终端交互界面。

当前路线与验证状态见仓库根的 `research\roadmap-current.md`；历史设计不代表当前待办。

## 它做什么
- 工具调用循环：read、write、edit、shell、grep、glob、todo（GPT 家族额外有 apply_patch）
- 网页：web_search（默认 Exa，失败回退 DuckDuckGo；也可选 DeepSeek/Tavily/Brave/关闭）、web_fetch（默认本机直接抓取 + HTML 转文本，也可选 DeepSeek，长页面截断可续读），每个请求默认 20 秒超时，代理跟随 `[network]`
- 子代理：task 工具派生独立上下文的子代理（explore 只读 / general 继承权限 / 自定义 `~/.ee/agents/*.md`、`.ee/agents/*.md`），可指定最大步数和模型；审批照常弹给用户，中断会一并停止子代理，只把 `<task_result>` 报告交回主代理
- 长期记忆：`~/.ee/memory.md`（用户级）+ `<项目>/.ee/memory.md`（工作区级），memory 工具增/列/删；新会话开始时作为固定消息注入（默认上限 8KiB，会话内不变，保住前缀缓存）
- 多模型：只走 OpenAI Chat Completions 协议，内置 DeepSeek、Qwen、Kimi、GLM、MiniMax、豆包、GPT、Claude、Gemini 的家族档案（各家的小差异在这里抹平）
- 权限三档 + 审批、工具输出截断、错误回喂、429/5xx 重试、前缀缓存统计
- 会话存储、git diff / 单文件撤销 / 撤销上一回合
- `ee serve`：本地 WebSocket JSON-RPC 服务，只监听 127.0.0.1，需要 token

## 命令
```powershell
ee serve [--port N] [--exit-on-stdin-eof]   # 桌面端后端，token 读 EE_SERVE_TOKEN
ee doctor [模型名]                           # 自检某个模型兼容性（会真实调用一次 API）
ee selftest                                  # 检查打包是否完整（openai/httpx/证书/编码），不联网
```

## 开发
必须用虚拟环境，不要用 PATH 里的 `python`：`openai` 与 `tree_sitter` 是延迟导入，装错解释器时大部分测试仍会通过，只在触及真客户端、Windows 沙箱、仓库地图时才失败——看起来像代码坏了，实际是环境不对。

```powershell
uv venv .venv                                        # 在仓库根建，只建一次（本机 .venv 就是 uv 建的，里面没有 pip）
uv pip install --python .venv/Scripts/python.exe -e "ee[test]"
./.venv/Scripts/python.exe -m pytest -c ee/pyproject.toml -q
```
没有 uv 时用标准库建环境，但**必须显式装 test 依赖**：
```powershell
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e "ee[test]"
```
需要 Python 3.11+。建议装 ripgrep（`winget install BurntSushi.ripgrep.MSVC`），没有也能用。

当前实测基线（2026-10-28，全量）：`1578 passed / 15 skipped / 0 failed`，约 17 分钟。只想跑相关子集时把测试文件路径附在命令后即可。

## 配置
- 用户配置：`%USERPROFILE%\.ee\config.toml`，桌面端设置页会读写它（保存前备份为 `.bak`，注释不保留）
- 项目配置：项目目录下 `.ee/config.toml`（设置页不改它）
- API key：设置页保存到 Windows 凭据管理器（不可用时使用受保护的后备存储），或填环境变量名，比如 `DEEPSEEK_API_KEY`。读取优先级：凭据存储 > 旧版配置明文 > 环境变量；启动时迁移旧明文，设置接口不返回完整 key。
- 网页搜索（设置 → 工具与记忆，或 `[web]`）：`search_backend = "exa" | "duckduckgo" | "deepseek" | "tavily" | "brave" | "off"`，默认 `exa`（免费、无需 key，Exa 公共 MCP 端点；有 `EXA_API_KEY` 时会带上）；`duckduckgo` 免费但可能被限流；主后端出错时按 `search_fallback = ["duckduckgo"]` 依次回退（`[]` = 不回退），全部失败才报错。`fetch_backend = "direct" | "auto" | "deepseek" | "off"`，默认 `direct`（本机直接抓取，免费）；`timeout = 20`、`max_results = 8`；Tavily/Brave 的 key 存 `tavily_api_key` / `brave_api_key` 或用环境变量 `TAVILY_API_KEY` / `BRAVE_API_KEY`。DeepSeek Search/Fetch 为可选付费项，自 2026-11-01 00:00（北京时间）起按成功请求计费，各 $0.75/千次。
- 子代理 `[subagent]`：`enabled = true`、`max_steps = 30`（默认步数）、`max_steps_limit = 100`
- 记忆 `[memory]`：`enabled = true`、`workspace = true`、`max_bytes = 8192`
- 工具开关在新会话开始时决定工具表（之后冻结进缓存前缀），改设置从下一个新对话生效；已启用的工具换 key 或换后端，对空闲中的会话立即生效。
- 网络（设置 → 模型与供应商 → 网络，或 `[network]`）：`proxy = "system"`（跟随系统：先看 HTTPS_PROXY 等环境变量，Windows 再读系统代理注册表；不支持 PAC 自动配置脚本）/ `"direct"` / `"http://127.0.0.1:7890"`；`connect_timeout = 15`、`read_timeout = 90`（秒，两次收到数据的最长间隔）、`retries = 2`。本机地址永远直连。

## 日志与排错
- 后端日志：`ee-backend.log`，位置依次为环境变量 `EE_LOG_DIR`（桌面端传入）→ Windows `%APPDATA%\ee\logs` → `~/.ee/logs`。记录每次 RPC 失败、慢 RPC（≥5 秒）、回合开始/结束、模型请求开始/结束/异常（含 traceback）、所用代理；事件循环卡住 ≥8 秒会把卡住处的调用栈写进去。
- 桌面端日志：同目录 `backend.log`（后端启动命令、stdout/stderr、界面侧 RPC 失败、重连、自动重启）。
- 端到端自检：**必须在 `ee/` 目录内执行**，否则 `python -m ee` 拿到的 `ee` 是命名空间包（没有 `__version__`），后端启动即崩：
  ```powershell
  cd ee
  ../.venv/Scripts/python.exe scripts/e2e_ws.py ../.venv/Scripts/python.exe -m ee serve --exit-on-stdin-eof
  ../.venv/Scripts/python.exe scripts/e2e_ws.py --blackhole ../.venv/Scripts/python.exe -m ee serve --exit-on-stdin-eof
  ```
  脚本会先自检目录，站错地方会立刻报错并给出可直接粘贴的命令。`--blackhole` 测超时路径。尚未接入 CI。
- 子进程一律走 `ee/proc.py:run_capture`（stdin=DEVNULL、超时杀进程树）；不要直接 `subprocess.run`，也不要在事件循环上同步跑子进程或慢 IO（用 `asyncio.to_thread`）。原因见仓库根 HANDOFF.md 的 Windows 无响应问题。

## 协议速览
方法：`app/info`、`thread/list|start|get|rename|delete|undo`、`turn/start|interrupt`、`approval/respond`、`settings/get|set`、`models/list`、`workspace/list|open|remove`、`files/search`、`git/diff`、`git/revert_file`、`memory/get|add|remove|save`。

子代理进度挂在父 task 工具条目的 `meta.subagent`（`{agent, description, model, mode, max_steps, status, step, tools[], text, summary, usage}`）上，通过 `item/updated` 推送；子代理的审批是普通 approval 条目，`call_id` 形如 `父调用id/子调用id`。

事件：`item/started|delta|updated|removed`、`turn/started|status|completed`、`thread/updated|usage`。`turn/status {thread_id, text}` 是临时状态（“正在连接模型…”），空字符串表示清除。

启动成功会打印一行 `EE_SERVE_READY {"port":N}`。
