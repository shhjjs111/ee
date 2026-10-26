按文件名 glob 模式查找文件，返回路径列表（按修改时间从新到旧）。

用法：
- 例：`**/*.py`、`src/**/*.{ts,tsx}`、`*test*`。不含 / 的模式匹配任意目录下的文件名。
- path 指定搜索根目录，默认工作目录。会自动跳过 .git、node_modules 等目录。
- 找文件请用这个工具，不要用 shell 跑 find / dir / Get-ChildItem -Recurse。
- 可以在一条回复里并行发多个 glob / grep。
