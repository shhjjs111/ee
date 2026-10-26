读取本地文件（或列出目录）。

用法：
- path 可以是绝对路径，也可以是相对工作目录的路径；Windows 路径用 \ 或 / 都行。
- 默认从第 1 行读最多 2000 行；大文件用 offset / limit 分段读，不要一次 30 行这样零碎地读。
- 输出每行前面是「行号 + 制表符」，制表符之后才是文件原文。编辑时不要把行号前缀带进 old_string。
- 超过 2000 字符的行会被截断。
- 想同时看几个文件时，在同一条回复里并行发多个 read 调用。
- 文件自上次读取后没变时，会返回「与上次读取相同」的引用而不是全文，节省上下文；确实需要时再读一次即可拿到全文。
- 不知道路径时先用 glob；要在文件里找内容用 grep，不要用 shell 跑 cat/type/Get-Content。
- 能直接读 PDF、Word（.docx）、Excel（.xlsx/.xlsm）、PowerPoint（.pptx）：自动转成文字（PDF 按页标出「第 N 页」，Word 的标题和表格转成 markdown，Excel 每行带行号和列字母）。PDF/PPTX 用 pages 选页（如 "2-5"），Excel 用 sheet 选工作表；转换后的文字同样可以用 offset/limit 续读。不要用 shell 去装库或写脚本来读这些文件。旧版 .doc/.xls/.ppt 不支持。
