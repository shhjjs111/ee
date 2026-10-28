# 集百家之长：个人 Agent 设计与计划（2026-10-25）

> **历史草案，已被替代。** 当前路线与状态见 roadmap-current.md。下方未勾选项是当时的设计记录，不代表当前功能缺失；终端界面方向已取消，不据此重复实施。

来源笔记：research/codex.md、claude-code.md、opencode.md、tianshu.md

## 设计取舍
- 接口：DeepSeek Chat Completions（OpenAI 兼容），默认 flash，难题/规划可切 pro。
- 编辑工具：选 Claude Code 式“精确字符串替换”+ OpenCode 的容错匹配链，不用 Codex 的 apply_patch（那是 GPT 训练过的格式，DeepSeek 没有自由文本工具/语法约束）。
- 缓存优先（天枢/waveloom）：system+tools 会话内冻结并做 sha256 校验；动态信息做成 <context-update seq=N> 增量附录挂在最新 user 消息尾部；历史只追加不改写；reasoning_content 始终回传（缺失补 ""）；压缩只在用户回合边界；显示 prompt_cache_hit_tokens 命中率和花费。
- 结构：从单文件升级为小包 agent/（core 循环、tools/、prompts/、config），依赖只加 rich（界面）可选。

## 阶段
### M1 核心编程 agent
- [ ] 主循环：无 tool_calls 且无未执行调用才结束；步数上限到顶时追加“只总结不调工具”；无依赖的工具并行；每个 tool_call_id 必有输出；错误回喂
- [ ] 工具：read（带行号/offset）、write、edit（先读后改、唯一匹配、replace_all、容错链）、bash（超时）、grep/glob（rg）、todo（整表替换、仅一个进行中）
- [ ] 输出截断：超 2000 行/50KB 存文件，返回头尾预览+路径
- [ ] 权限：deny→ask→allow 规则，命令按 | && ; 拆开逐段判；写/删/跑命令默认问；once/always/reject(带理由)
- [ ] 缓存结构 + 命中率/花费显示
- [ ] 系统提示词：从 Codex/Claude Code 抄“持续到解决、调用前一句话、不回滚别人改动、改完不重读”等

### M2 稳定与上下文
- [ ] AGENTS.md 分层加载（作为 user 消息，32KB 上限）+ 每轮环境块（走增量附录）
- [ ] 压缩：用户边界触发（~0.86 窗口），结构化 checkpoint（目标/需求/决策/状态/下一步/文件），对齐 assistant↔tool 配对；旧工具输出 prune 成占位符
- [ ] 防死循环：同工具同参数连续 3 次暂停；DeepSeek 畸形工具调用兜底解析（content 里 JSON / DSML 标记）、参数类型修复
- [ ] web_search + web_fetch（需选搜索 API）

### M3 子代理与规划
- [ ] task 工具：独立上下文、只回传总结；explore 只读子代理（flash）；fork 式子代理复用父前缀命中缓存
- [ ] plan 模式（执行时拦截写操作，不删 schema）
- [ ] 证据门：改过文件但没通过一次验证（测试/运行）不能宣布完成

### M4 记忆与技能
- [ ] 会话存盘/恢复；长期记忆 MEMORY.md 索引（前 200 行）+ 主题文件按需读
- [ ] skills：只常驻名字和描述，用时追加正文（不改 system）
- [ ] 作业/资料：读 PDF、整理笔记到文件

### M5 体验
- [ ] 流式输出 + 终端界面（rich/prompt_toolkit）
- [ ] MCP 客户端、flash/pro 路由
