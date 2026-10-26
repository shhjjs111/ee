在当前 shell 里执行命令，返回输出和退出码；输出太长时保留首尾，全文存到文件。

## 规则
- 命令用 <context-update> 里那种 shell 的语法，换目录传 workdir 而不是 cd；常驻或很久的命令传 background=true，不要用 `&`、start、nohup 自己放后台。
