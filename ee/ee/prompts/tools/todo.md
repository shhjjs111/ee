维护本次任务的计划清单（每次传入完整清单，整表替换）。界面会把它渲染成勾选清单给用户看。

何时用：
- 任务需要 3 步以上，或用户一次给了多项任务。
- 开始做某一步前把它标成 in_progress；做完立刻标 completed，不要攒着一起标。
- 同一时刻最多只有一项 in_progress。
- 只有真正完成（包括需要的验证）才标 completed；卡住了就保持 in_progress 并新增一项说明阻塞。

何时别用：
- 单一、简单的改动，或纯问答。

每一项的 content 用简短祈使句（如「运行测试」「修复 parse 的空指针」），status 为 pending / in_progress / completed。
