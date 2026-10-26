生成仓库地图：用语法解析列出项目里最重要的定义（类、函数、方法、接口、类型…的签名行，带行号），按“被多少代码引用”排名，在 token 预算内输出“文件 → 关键定义”。

用法：
- 刚接手陌生项目、要弄清整体结构或某个功能大概在哪时先调用它，再按地图里的文件和行号去 read，比逐个 glob / read 省得多。
- focus：关注的文件或符号（如 `["session.py", "run_turn"]`），这些和与它们关联紧密的定义会排在前面。
- path：只看某个子目录（相对工作目录），排名仍按整个项目的引用关系算。
- max_tokens：输出预算，默认约 1500；项目大、想看更多时可调到 4000 左右。
- 支持 Python、JavaScript/TypeScript/TSX、Go、Rust、Java、C/C++、C#、Ruby、PHP、Kotlin、Lua；遵守 .gitignore 和 .eeignore，跳过 node_modules 等目录。
- 地图只是索引（每行是定义的第一行），要看实现请 read；找具体字符串仍用 grep。同一会话里结构没大变时不必重复调用。
