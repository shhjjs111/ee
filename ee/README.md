# ee：个人 AI 编程 agent

在终端里和你一起读代码、改代码、跑命令。只走 OpenAI Chat Completions 协议，DeepSeek、Qwen、Kimi、GLM、MiniMax、豆包、经中转站的 GPT 都能用。Windows 是首要平台，macOS / Linux 也能用。

- 交互界面：`ee`（Codex 风格，内联在终端里，不占全屏）
- 无界面：`ee exec "任务"`（最终回答输出到 stdout，适合脚本）
- 模型自检：`ee doctor [模型]`（会真实调用一次 API）

## 安装

需要 Python 3.11+。建议装 [ripgrep](https://github.com/BurntSushi/ripgrep)（`winget install BurntSushi.ripgrep.MSVC`）；没有也能用，只是搜索慢一些。

```powershell
cd ee                # 本目录（含 pyproject.toml）
pip install -e .
ee --version
```

## 设置 API key（只从环境变量读，不会打印出来）

PowerShell（只对当前窗口生效）：

```powershell
$env:DEEPSEEK_API_KEY = "sk-..."
ee
```

永久保存（新开的窗口才生效）：

```powershell
setx DEEPSEEK_API_KEY "sk-..."
```

macOS / Linux：`export DEEPSEEK_API_KEY=sk-...`

## 配置文件

- 用户级：`%USERPROFILE%\.ee\config.toml`（macOS/Linux 是 `~/.ee/config.toml`）
- 项目级：`<项目>\.ee\config.toml`，覆盖用户级（从当前目录往上找，到 git 根为止）
- 环境变量 `EE_HOME` 可以改 `.ee` 目录位置

不写配置也能用：内置默认模型 `deepseek-flash`（base_url `https://api.deepseek.com`，key 变量 `DEEPSEEK_API_KEY`），另有 `deepseek-pro`（`deepseek-v4-pro`）。

`[models.名字]` 里除了 `provider` 和 `model`，还可以逐项覆盖家族档案：`context_window`、`request_max_tokens`、`parallel_tools`、`reasoning_passback`（none / tool_turns / all）、`edit_style`（edit / apply_patch）、`temperature`、`family`（强制指定家族）、`extra_body`、`[models.名字.price]`（input / output / cache_read，每百万 token，用来算花费）。家族按模型名子串识别：deepseek / qwen / kimi|moonshot / glm / minimax / doubao / gpt|o1-9 / claude / gemini，其余用 default。

> 下面的 base_url 和模型名是示例，请以各家官方文档为准。

### DeepSeek（默认）

```toml
default_model = "deepseek-flash"

[providers.deepseek]
base_url = "https://api.deepseek.com"
api_key_env = "DEEPSEEK_API_KEY"

[models.deepseek-flash]
provider = "deepseek"
model = "deepseek-flash"

[models.deepseek-pro]
provider = "deepseek"
model = "deepseek-v4-pro"
```

### Qwen（阿里云百炼 / DashScope 兼容模式）

```toml
[providers.dashscope]
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
api_key_env = "DASHSCOPE_API_KEY"

[models.qwen-coder]
provider = "dashscope"
model = "qwen3-coder-plus"
```

### Kimi（Moonshot）

```toml
[providers.moonshot]
base_url = "https://api.moonshot.cn/v1"
api_key_env = "MOONSHOT_API_KEY"

[models.kimi]
provider = "moonshot"
model = "kimi-k2-turbo-preview"
```

### GLM（智谱）

```toml
[providers.zhipu]
base_url = "https://open.bigmodel.cn/api/paas/v4"
api_key_env = "ZHIPUAI_API_KEY"

[models.glm]
provider = "zhipu"
model = "glm-4.6"
```

### 中转站 GPT

```toml
[providers.relay]
base_url = "https://你的中转站/v1"
api_key_env = "RELAY_API_KEY"
# headers = { "X-Custom" = "..." }   # 中转站要求的额外请求头

[models.gpt]
provider = "relay"
model = "gpt-5"
[models.gpt.price]          # 可选：填了才显示花费
currency = "$"
input = 1.25
output = 10.0
cache_read = 0.125
```

GPT 家族会额外拿到 `apply_patch` 工具（它训练过的补丁格式）。也可以不写 `[models.x]`，直接 `ee -m relay/gpt-5`。

### shell（执行命令用哪个）

```toml
shell = "auto"    # auto | pwsh | powershell | git-bash | cmd | bash
```

- Windows 上 `auto` = 先找 `pwsh`（PowerShell 7），没有就用 `powershell.exe`（5.1）
- `git-bash` 会在 `C:\Program Files\Git\bin\bash.exe`、`%LOCALAPPDATA%\Programs\Git\bin\bash.exe`、git.exe 旁边和 PATH 里找 `bash.exe`（跳过 WSL 的 `System32\bash.exe`），找不到会明确报错
- 运行中用 `/shell` 切换（会写回配置），或在 `/settings` 里改默认值；单次启动可用 `ee --shell git-bash`
- 切换 shell 不会破坏缓存：工具名就叫 `shell`、描述通用，当前 shell 通过附在消息末尾的环境信息增量告诉模型

### 权限

```toml
[permissions]
mode = "default"                        # read-only | default | auto
allow = ["shell(make *)", "shell(npm run *)"]
ask   = ["shell(docker *)"]
deny  = ["shell(npm publish*)", "write(*.env)"]
```

- 只读：只能读和搜（含 `git status/diff/log` 等只读命令）
- 默认：工作区内读写免问；只读命令（ls、rg、git status/diff/log/show/branch/blame、gh pr view/list、Get-ChildItem、pytest…）免问；其他命令（git commit/checkout/switch/stash/merge/rebase、联网、写重定向、gh 写操作…）先问；写工作区外的文件先问
- 全自动：都不问，但仍拦截危险命令
- 命令会按 `| && || ; &`、`$(...)`、反引号拆开逐段判断，bash 和 PowerShell 语法都认
- 永远拒绝：`rm -rf /`、`Remove-Item -Recurse -Force C:\`、`del /s C:\`、`format C:`、`git push --force` 等
- 即使全自动也会问：`git reset --hard`、`git clean -fd`、`rm -rf 目录`、`Remove-Item -Recurse`、`del /s`、`rd /s`、`sudo` 等

审批弹窗（照 Codex）：`是，仅这次` / `是，以后 \`git commit\` 开头的命令都不问`（本会话内）/ `否，跳过` / `否，并告诉它怎么改`。方向键选、数字键直达。

## 快捷键

| 按键 | 作用 |
|---|---|
| Enter | 发送；运行中按 Enter = 排队，当前步结束后送入 |
| Ctrl+J / Alt+Enter | 换行（Windows Terminal 里 Shift+Enter 一般分辨不出来，请用 Ctrl+J） |
| `/` | 命令菜单 |
| `@` | 模糊搜索文件，插入路径 |
| `!命令` | 直接在当前 shell 里运行，输出会附在下一条消息里给模型 |
| Esc | 运行中：中断（保留已产生的历史，可以接着说）；空输入连按两次：回退并编辑上一条消息 |
| Ctrl+T | 查看上一段思考全文（思考默认折叠成一行） |
| ↑ / ↓ | 输入历史 |
| Ctrl+C | 运行中：中断；有输入：清空；空闲时连按两次：退出 |
| Ctrl+D | 空输入时退出 |
| `?` | 输入框为空时显示/隐藏快捷键 |

大段粘贴会折叠成 `[粘贴 120 行 #1]`，发送时自动展开；粘贴里的回车不会误发送。

## 命令

| 命令 | 作用 |
|---|---|
| `/new` | 开始新对话（system 和工具表不变，缓存前缀还能命中） |
| `/model [名字]` | 切换模型；不带参数弹出列表。换模型后服务端缓存从零开始 |
| `/permissions` | 切换 只读 / 默认 / 全自动 |
| `/settings`（`/config`） | 设置面板：默认模型、默认 shell、默认权限、最大步数、命令超时，方向键选择，写回 `config.toml` |
| `/shell [名字]` | 切换 shell 并写回配置 |
| `/status` | 模型、服务地址、key 是否设置（不显示 key）、缓存命中、花费、git、前缀哈希 |
| `/diff` | 彩色显示 `git diff HEAD`，含未跟踪文件 |
| `/commit [信息]` | 不带信息：让模型根据 diff 和最近提交风格写提交信息，确认后提交（有暂存就只提交暂存，否则 `git add -A`）；带信息：确认后直接提交 |
| `/review [关注点]` | 让模型审查当前未提交的改动（只读，不改文件） |
| `/undo` | 撤销上一回合 ee 对文件的改动（写文件前都做了快照） |
| `/compact` | 压缩上下文（M2 实现，现在只显示占用） |
| `/help` | 命令和快捷键 |
| `/quit`（`/exit`） | 退出 |

底栏：左边 `? 快捷键`（运行中是 `Esc 中断`），右边 `模型 · 权限模式 · 上下文剩余 N% · 命中 N% · ¥花费 · 分支(*=有未提交改动)`。中转站不返回缓存字段时命中显示 `—`；没配单价时花费显示 `¥?`。

## 无界面模式

```powershell
ee exec "把 utils.py 里的 parse_date 改成支持时区，并跑测试"
Get-Content task.txt | ee exec -          # 从 stdin 读任务
ee exec --json "列出 TODO"                # 事件逐行 JSON
ee --full-auto exec "跑 lint 并修复"       # 需要审批的操作放行（危险命令仍拦截）
```

无人值守时，需要审批的操作会被拒绝并告诉模型（除非 `--full-auto`）。退出码：完成 0，出错/中断 1。

## 缓存与花费

DeepSeek 等前缀缓存模型命中时输入价格只有几十分之一，所以 ee 严格保持请求前缀不变：

- system 提示词和工具表在会话第一次请求时冻结，每次请求前做 sha256 校验，变了会警告
- 日期、工作目录、shell、权限模式、git 分支/改动数等动态信息不进 system，而是作为 `<context-update seq=N>` 附在最新一条用户消息末尾：第一次全量，之后只发变化的部分
- 历史只追加不改写；DeepSeek 的 reasoning_content 在带工具调用的轮次回传（缺失补 ""），纯文本轮不回传，规则固定所以字节稳定
- 切权限模式、切 shell、/new 都不破坏前缀；/model 换到不同家族会重新冻结

## 数据位置

`%USERPROFILE%\.ee\`：`config.toml`、`sessions\*.jsonl`（会话记录）、`tool-output\`（被截断的完整工具输出）、`state\quirks.json`（自动降级记住的参数）、`state\doctor.json`、`history.txt`（输入历史）。

## 开发

```powershell
pip install -e ".[test]"
python -m pytest -q
```

测试用假的 OpenAI 兼容客户端和本地假 HTTP 服务器，不调用真实 API。
