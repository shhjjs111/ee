在文件内容里按正则搜索（基于 ripgrep，没有 rg 时自动回退）。

用法：
- pattern 是正则（rg 语法），如 `def\s+main`、`TODO|FIXME`。要搜字面量里的特殊字符记得转义。
- path 限定目录或文件；include 用 glob 过滤文件，如 `*.py`、`*.{ts,tsx}`。
- output_mode：content（默认，文件 + 行号 + 匹配行）、files（只列文件）、count（每个文件的匹配数）。
- 结果按文件修改时间排序，数量有上限；被截断时缩小范围再搜。
- 搜索请一律用这个工具，不要用 shell 跑 grep / rg / Select-String。
- 想做几种不同搜索时，在同一条回复里并行调用多个 grep / glob。
