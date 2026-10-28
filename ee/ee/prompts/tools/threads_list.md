列出本会话派出去的子会话：标题、运行状态、终态原因、worktree 分支、最后一条消息。

## 规则
- 想知道自己派了哪些子任务、它们成功没有，用 threads_list。
- `status` 是交互状态（idle/running/waiting_approval/compacting）；`terminal_reason` 才是结果（completed / error / interrupted / budget / max_steps）。idle 不代表成功。
- 只能看到本会话派发的子会话；其他会话的子会话不在结果里。
