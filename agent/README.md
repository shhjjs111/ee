# 个人 AI Agent

## 运行
```bash
pip install openai
export DEEPSEEK_API_KEY=你的key        # Windows: set DEEPSEEK_API_KEY=...
python agent.py
```
可选环境变量：`DEEPSEEK_MODEL`（默认 deepseek-flash）、`DEEPSEEK_BASE_URL`（默认 https://api.deepseek.com）。

试试：「帮我算 (1234*5678)/9」「读一下 ./README.md 总结一下」

## 待办
- [x] 第1步：最小循环 + 计算、读文件（已含最大步数、工具错误回喂）
- [ ] 第2步：网页搜索、运行 Python、写文件
- [ ] 第3步：危险操作确认（写文件/跑代码前问一下）、API 重试、token 用量统计
- [ ] 第4步：记忆（对话存盘、长期笔记）、简单规划
- [ ] 第5步（可选）：流式输出、网页界面、MCP
