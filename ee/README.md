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

## 协议速览
方法：`app/info`、`thread/list|start|get|rename|delete|undo`、`turn/start|interrupt`、`approval/respond`、`settings/get|set`、`models/list`、`workspace/list|open|remove`、`files/search`、`git/diff`、`git/revert_file`。

事件：`item/started|delta|updated|removed`、`turn/started|completed`、`thread/updated|usage`。

启动成功会打印一行 `EE_SERVE_READY {"port":N}`。
