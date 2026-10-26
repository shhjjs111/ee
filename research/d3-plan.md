# D3 实施方案（2026-10-26 13:50 定稿，子代理调研结果；实现时以此为准）

## A. MCP（新包 ee/ee/mcp/，自实现，无新依赖，httpx+asyncio）
- client.py 三种传输：
  - stdio：asyncio.create_subprocess_exec，stdin 显式 PIPE（不继承后端 stdin），limit=32MB，持续读空 stderr 只留最后 50 行。Windows：shutil.which 找程序；.cmd/.bat 用 list2cmdline + create_subprocess_shell，CREATE_NO_WINDOW。关闭用 ee/proc.py 的 kill_tree（to_thread）。
  - streamable HTTP：POST，Accept: application/json, text/event-stream；响应 JSON 或 SSE；带 Mcp-Session-Id、MCP-Protocol-Version。
  - 旧式 SSE：先收 endpoint 事件再 POST。
  - 流程 initialize → notifications/initialized → 分页 tools/list → tools/call；回应 ping、roots/list，其他请求回 -32601；各步超时。
- schema.py（参照 Codex 规范化）：展开 $ref/$defs，合并 allOf；anyOf/oneOf 去 null 取第一个；type 数组取第一个非 null；const→enum；删 format/additionalProperties/title/examples/$schema；缺 type 推断否则 string；数组缺 items 补 string；顶层强制 object+properties；required 只留存在属性；工具名清洗 [A-Za-z0-9_-]，>64 截断加 hash。
- config.py：[mcp] enabled/startup_timeout/tool_timeout/project_json/allowed_project；[mcp.servers.<name>] command/args/env/cwd 或 url/headers/type + enabled/trusted。从工作区向上到 git 根找第一个 .mcp.json（mcpServers 格式）；同名用户配置覆盖；项目服务器需用户允许，记为 name:sha8(配置)，配置变则重新允许；支持 ${VAR} 与 ${VAR:-默认}。
- manager.py：每事件循环一个管理器，按配置 hash 缓存连接，并行连接，失败只记状态；call（断线重连一次）、test（独立连接列工具后关）、状态监听。
- tool.py：McpTool 名 mcp__<srv>__<tool>，描述前加 [MCP srv]，read_only 取 readOnlyHint；文本原样；图片/音频存 tool-output/mcp-images/ 并写一行说明；resource 转文字；isError→ok=False。
- 接入 Session：首次冻结前缀（_ensure_prefix）前 await _attach_mcp，发状态“正在连接 MCP 服务器…”，失败发 Notice；MCP 工具追加在工具表末尾；set_model 重建工具表时重新加上。新增 McpStatus 事件 → Thread 通知 thread/mcp，detail() 加 mcp。子代理定义了 adef.tools 时不接 MCP。
- 权限（permissions.py 加 mcp__ 分支）：deny 先拦；只读模式下 readOnlyHint 工具询问（信任服务器直接放行），其余拒绝；全自动放行；信任服务器 / allow 规则（支持 mcp__srv__* 通配）/ 会话内“以后都行”放行；其余询问。顺手修：“未知工具”分支没查 session_tools 导致“以后都行”不生效。
- RPC：server/mcp_rpc.py（mcp/list {cwd}、mcp/test {name, server, cwd}），按 memory_rpc.register 写法注册；settings.py 加 _mcp_out/_mcp_in。
- 设置页 McpSettings.tsx：增删改、启用、信任、允许项目服务器、测试连接显示工具与状态点。
- 测试：ee/tests/fake_mcp_server.py（stdio），HTTP/SSE 用线程 http.server。

## B. 斜杠命令与自定义提示
- 后端 core/prompts.py + RPC prompts/list、prompts/expand：~/.ee/prompts/*.md 与 <项目>/.ee/prompts/*.md，frontmatter description/argument-hint，替换 $ARGUMENTS；内置 prompts/init.md 生成 AGENTS.md。
- 前端 lib/slash.ts + components/SlashMenu.tsx，Composer 只插一个 hook：/compact /new /undo(thread/undo) /init /model /mode（二级选项设 draftModel/draftMode）/memory（带文字 memory/add，否则打开设置）/mcp + 自定义提示。store 加 settingsSection 以跳转设置分区。

## C. 配置迁移 + 单价
- 新模块 ee/ee/core/migrate.py，版本链 [(2, fn), ...]；load_config 与 settings.effective() 开头调用；write_setting 写回保留注释；备份 config.toml.pre-v2.bak。v2：search_backend "deepseek"→"exa"，fetch_backend "auto"→"direct"。secure-keys 会在此链上加 v3（明文 key 迁入凭据管理器）。
- settings._d2_out 兜底 fetch_backend 改 "direct"；_d2_out/_d2_in 加 search_fallback；设置页加选项。
- Profile 加 variants，profile_for() 按模型名匹配合并 price/vision；deepseek.toml 加 [[variants]] match=["deepseek-v4-pro"] 价格 0.30/9/27 元每百万。flash 是否支持图像需查官方文档。

## D. 附件
- ee/ee/core/attachments.py：存 ~/.ee/sessions/<id>/attachments/；用户条目记 attachments 元数据（含前端 canvas 缩略图）。wire_messages 会去掉 _ 开头键：user 消息挂 _images（路径），发送时转 data URL；vision=false 降级为文字提示+提示条目。前端 canvas 压缩，不用 Pillow。
- 调用链加 attachments 参数：app.turn_start → Thread.start_turn → Session.run_turn → _add_user。settings.MODEL_KEYS 加 vision，models/list 返回 vision。
- 前端 lib/attachments.ts + 附件托盘组件，Composer 小改，气泡显示缩略图。

## E. 分叉 / 编辑重发
- server/branch.py：thread/fork、thread/edit_resend。store 新增 truncate {from_seq} 记录（SessionStore.load 处理），历史截断用已有 rewind。
- 撤销：state.snapshots 每回合一层（仅内存），_turn 开头记层号，从被编辑回合倒序恢复；无快照提示不可撤销。turn_starts 压缩后清空、重启不恢复，需注意。
- 分叉复制该消息之前历史与附件目录，meta 记 parent_id，新会话输入框预填该消息；左栏分叉图标；thread_delete 连同 sessions/<id>/ 目录删除。

## F. API key 安全存储（方案已定，未实现）
- 各家：Codex 凭据存储 File(默认 auth.json 0600)/Keyring/Auto/Ephemeral，Windows 用凭据管理器；OpenCode auth.json 明文 0600；Claude Code macOS Keychain，其他平台 0600 文件，env 优先。
- ee/ee/core/secrets.py：Windows 用 ctypes 调 CredWriteW/CredReadW/CredDeleteW（GENERIC，目标名 ee/provider/<名>，无需 keyring 包）；不可用时回退 ~/.ee/credentials.json（Windows DPAPI CryptProtectData 加密，其他平台 0600 并在 UI 警告）；Linux/mac 有 keyring 用 keyring。配置 [secrets] store=auto|keyring|file。
- config.toml 只存 api_key_ref；覆盖 providers.*.api_key 与 web.tavily/brave/exa/deepseek_api_key（tools/web.py）。优先级改为 环境变量 > 凭据存储 > 旧明文（现在 config.py ModelSpec.api_key() 与 web.py backend_key/deepseek_key 顺序相反）。
- settings/get 只返回来源(env/store/plain)+••••末4位+存储后端说明，绝不回传明文。
- 迁移在 cli.py serve 启动处；不得生成含明文的 .bak（save_user_file 会原样备份，要改），抹掉已有 .bak 里的明文；日志只记“已迁移 N 个”。
- 脱敏 redact()：log.py Filter、store.py SessionStore.append、session._friendly_error、net.describe_error、app.py RPC 错误；规则=已知 key 值 + sk-… / Bearer … 正则。
- 前端：SettingsPage.tsx ProviderEditor（约84–150行）、AgentSettings.tsx KeyInput（约71行）改为状态/来源/替换/删除，说明“保存在 Windows 凭据管理器”；types.ts 加 source。
- 需改的旧测试：test_server.py:184–197、test_robustness.py:225–240、test_server_d2.py:32–43、test_web.py:36/96/100。conftest 加 autouse 内存后端；cli selftest --secrets + workflow 用打包 exe 跑。
- 未覆盖：bash 输出里的 key（echo $DEEPSEEK_API_KEY）会进模型上下文。
