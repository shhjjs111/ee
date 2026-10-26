在当前 shell 中执行一条命令，返回合并后的 stdout/stderr 和退出码。

用法：
- 当前用的是哪种 shell（PowerShell / cmd / bash / Git Bash）以环境信息 <context-update> 里的为准，命令必须用那种 shell 的语法。PowerShell 里用 `;` 连接命令，用 `$env:NAME` 读环境变量。
- 用于 git、构建、测试、包管理等终端操作。读文件、搜索、找文件、改文件请用 read / grep / glob / edit / write，不要用 cat、grep、find、sed、Get-Content、Select-String。
- 需要在别的目录执行时传 workdir，不要用 cd。路径里有空格要加引号。
- 默认超时 120 秒（timeout 最大 600）。会一直运行的进程（dev server、watch、持续输出的日志）和要跑很久或需要交互输入的命令，传 background=true 后台运行：立即返回后台命令 id 和开头几秒的输出，之后用 shell_output 看新输出（可 wait 等待）、shell_write 写输入、shell_kill 结束。回合结束后台命令不会被杀，用完记得 shell_kill。不要在前台启动这类命令，也不要用 `&`、`start`、`nohup` 自己放后台。
- 输出过长会被截断：保留开头和结尾，完整内容存到文件并给出路径。
- 多条互不依赖的命令可以在同一条回复里并行调用；有先后依赖的用 `&&`（bash）或 `;`（PowerShell）连起来。
- 有些命令需要用户批准（写操作、联网、git 提交等）；被拒绝时按用户的意见调整，不要换个写法绕过。

Git 规范：
- 只有用户明确要求时才 git commit / push / 建 PR；不要改 git config，不要 --no-verify，不要 force push。
- 提交前先看 git status、git diff、git log --oneline -10，只暂存本次相关的文件，不要提交密钥。提交信息简洁，语言和风格跟随仓库已有提交。
- 不要用 git reset --hard、git checkout -- 、git clean 等丢弃不是你做的改动。
- 有 gh 命令时可以用它查 PR、issue、CI 状态；gh 的写操作同样需要用户要求。
