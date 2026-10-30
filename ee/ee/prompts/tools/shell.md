在当前 shell 里执行命令，返回输出和退出码；输出太长时保留首尾，全文存到文件。

## 规则
- 命令用 <context-update> 里那种 shell 的语法，换目录传 workdir 而不是 cd；常驻或耗时的命令传 background=true，别用 `&`、start、nohup 自己放后台。
- 不用 shell 写文件改代码（echo >、sed -i、Set-Content 等）。
- 跑测试先定位相关测试文件跑子集（-k 或指定文件），别全量。
