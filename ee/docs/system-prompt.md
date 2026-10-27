# 自定义系统提示词（SYSTEM.md / APPEND_SYSTEM.md）

## 文件和加载顺序
| 文件 | 作用 |
|---|---|
| `SYSTEM.md` | 替换内置的基础提示词（精简模式下替换的是精简版）；按启用的工具拼出的「工具使用规则」段不替换，照常追加 |
| `APPEND_SYSTEM.md` | 追加在后面（不替换） |

两种文件各自按下面顺序取第一个可用的：
1. `<项目>/.ee/SYSTEM.md`（从工作目录往上找到 git 根为止，取最近的）——**必须先在 设置 → 系统提示词 里信任**才生效
2. `~/.ee/SYSTEM.md`（Windows：`%USERPROFILE%\.ee\SYSTEM.md`）——直接生效
3. 都没有：用内置基础提示词

最终系统提示词 = 基础提示词（SYSTEM.md 或内置基础段）→ 工具使用规则（随启用的工具变化）→ 模型补丁（按模型家族，可关）→ APPEND_SYSTEM.md → 回答风格段。
AGENTS.md、记忆、技能清单仍然作为单独的固定消息加载，不受影响。没有任何自定义文件时，发给模型的内容和原来逐字节相同。

文件要求：UTF-8（带不带 BOM 都行），单个不超过 64 KiB；空文件、太大、编码不对会被忽略，并在对话开头提示。

## 配置（`~/.ee/config.toml`）
```toml
[system_prompt]
model_patch = true            # 是否追加按模型家族的补丁；项目 .ee/config.toml 也可覆盖
allowed_project = []          # 信任过的项目文件（"路径指纹:内容指纹"），设置页的「信任」按钮会写这里
```
信任记录只从用户级配置读，项目自己的 `.ee/config.toml` 写了也不算。和项目 Hooks 用同一种指纹，但分开存放：信任了项目 Hooks 不代表信任了项目的 SYSTEM.md。文件内容一改，指纹就变，要重新信任。

## 什么时候生效
系统提示词在对话开始时固定下来（为了保住前缀缓存）。改了文件、信任或撤销信任、开关模型补丁，都从**新对话**开始生效。
设置页「查看最终系统提示词」：在已经开始的对话里看到的是实际发给模型的那份；还没开始的对话看到的是新对话会用的那份。

## 安全
**系统提示词不是安全边界。** 权限模式、审批、拒绝（deny）规则、危险命令强制确认都在程序里执行，SYSTEM.md 里写什么都绕不过去。
项目里的 SYSTEM.md 可能被别人改过、用来注入指令，所以默认不生效，必须由你确认后信任。

### 只读 / 计划模式的命令白名单也不是安全边界
白名单（`core/permissions.py` 的 `SAFE_COMMANDS`）靠**静态分析**判断一条命令是否只读，
目的是防止模型手滑在只读模式改了文件，**不是**用来执行不可信内容的隔离层。

原因很直接：shell 语法足够复杂，静态分析永远可能有下一种写法。已经修过的几轮可以说明这点——
PowerShell 的 `ForEach-Object` 可以把动作藏在参数里（裸词 `Kill`、冒号形式 `-MemberName:Kill`、
动态表达式 `$env:MYACTION` / `$x.Kill`），每发现一种就补一次，但无法证明"没有第四种"。

真正的强制隔离是 **命令沙箱**（`[sandbox]`，受限令牌 + 能力 SID ACL）：它限制的是**写**，
而只读模式的威胁正是写（删文件、改配置）。所以：
- **只读 / 计划模式下默认启用沙箱**（即使 `[sandbox] enabled=false`）——
  `[sandbox] readonly_default = false` 可关掉这个默认，但不建议；
- **只读 / 计划模式下默认禁网**（`[sandbox] readonly_network_default`，默认 `false` = 禁）。
  这只拦沙箱里的 shell 子进程（死代理 + pip/npm/cargo/uv 离线 + `GIT_SSH_COMMAND=false`），
  **不影响 `web_search` / `web_fetch`**——那两个工具不经沙箱，是用户主动发起的联网搜索，
  需要时直接说就行；
- 要把 ee 用于执行来源不明的内容，请显式开 `[sandbox] enabled=true`。

## RPC
`sysprompt/info {cwd?, thread_id?}`、`sysprompt/preview {cwd?, thread_id?}`、`sysprompt/trust {cwd, allow_id}`、`sysprompt/untrust {allow_id}`、`sysprompt/set {model_patch}`，详见 `ee/server/sysprompt_rpc.py`。

## 模型请求失败时的处理
| 分类 | 例子 | 处理 |
|---|---|---|
| 限流 | HTTP 429 | 自动重试（次数见 `[network] retries`，默认 2）；服务端给了 `Retry-After` / `retry-after-ms` 就照它等，要求超过 60 秒则不等，直接提示 |
| 服务端出错 / 过载 | 5xx、408、409 | 自动重试，指数退避（1.5s、3s、6s…最多 20s，加随机抖动） |
| 超时 / 网络 | 连接失败、读取超时 | 自动重试 |
| 余额或额度不足 | 402；429/403/400 里明确说欠费、余额、billing | 不重试，提示去服务商充值或在 设置 → 模型与供应商 换模型 |
| 鉴权 | 401、403 | 不重试，提示到 设置 → 模型与供应商 检查 key |
| 上下文超长 | 400 + “maximum context length”等、413 | 不重试；回合中自动压缩上下文一次再试（自动压缩关闭时提示用 /compact） |
| 参数错误 | 400、422 | 先按原有逻辑自动去掉不被支持的参数重试；仍失败则提示 |

已经开始输出后才断开的请求不会自动重试（避免重复输出）。
