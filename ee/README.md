# ee 后端（Python 内核）

这是 ee 桌面端的内核和本地服务。平时不用直接打交道：桌面端启动时会自动拉起它。没有终端交互界面。

## 它做什么
- 工具调用循环：read、write、edit、shell、grep、glob、todo（GPT 家族额外有 apply_patch）
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
```powershell
cd ee
pip install -e .
python -m pytest -q
```
需要 Python 3.11+。建议装 ripgrep（`winget install BurntSushi.ripgrep.MSVC`），没有也能用。

## 配置
- 用户配置：`%USERPROFILE%\.ee\config.toml`，桌面端设置页会读写它（保存前备份为 `.bak`，注释不保留）
- 项目配置：项目目录下 `.ee/config.toml`（设置页不改它）
- API key：设置页里直接填（明文存配置文件），或填环境变量名，比如 `DEEPSEEK_API_KEY`
- 网络（设置 → 模型与供应商 → 网络，或 `[network]`）：`proxy = "system"`（跟随系统：先看 HTTPS_PROXY 等环境变量，Windows 再读系统代理注册表；不支持 PAC 自动配置脚本）/ `"direct"` / `"http://127.0.0.1:7890"`；`connect_timeout = 15`、`read_timeout = 90`（秒，两次收到数据的最长间隔）、`retries = 2`。本机地址永远直连。

## 日志与排错
- 后端日志：`ee-backend.log`，位置依次为环境变量 `EE_LOG_DIR`（桌面端传入）→ Windows `%APPDATA%\ee\logs` → `~/.ee/logs`。记录每次 RPC 失败、慢 RPC（≥5 秒）、回合开始/结束、模型请求开始/结束/异常（含 traceback）、所用代理；事件循环卡住 ≥8 秒会把卡住处的调用栈写进去。
- 桌面端日志：同目录 `backend.log`（后端启动命令、stdout/stderr、界面侧 RPC 失败、重连、自动重启）。
- 端到端自检：`python ee/scripts/e2e_ws.py [--blackhole] <后端命令>`（拉起假模型服务器，模拟发“你好”；`--blackhole` 测超时报错）。
- 子进程一律走 `ee/proc.py:run_capture`（stdin=DEVNULL、超时杀进程树）；不要直接 `subprocess.run`，也不要在事件循环上同步跑子进程或慢 IO（用 `asyncio.to_thread`）。原因见仓库根 HANDOFF.md 的 Windows 无响应问题。

## 协议速览
方法：`app/info`、`thread/list|start|get|rename|delete|undo`、`turn/start|interrupt`、`approval/respond`、`settings/get|set`、`models/list`、`workspace/list|open|remove`、`files/search`、`git/diff`、`git/revert_file`。

事件：`item/started|delta|updated|removed`、`turn/started|status|completed`、`thread/updated|usage`。`turn/status {thread_id, text}` 是临时状态（“正在连接模型…”），空字符串表示清除。

启动成功会打印一行 `EE_SERVE_READY {"port":N}`。
