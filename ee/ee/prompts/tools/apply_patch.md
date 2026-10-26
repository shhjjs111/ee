用补丁编辑文件。参数 patch 是一个字符串，内容是下面这种精简的、按文件组织的 diff 格式：

*** Begin Patch
[一个或多个文件段]
*** End Patch

每个文件段以下面三种头之一开始：
*** Add File: <路径>      新建文件，后面每一行都以 + 开头（即文件内容）
*** Delete File: <路径>   删除文件，后面没有内容
*** Update File: <路径>   原地修改文件（可紧跟一行 *** Move to: <新路径> 来改名）

Update 的改动块：
- 可以用 `@@ <某一行原文>` 定位到类、函数等位置（例如 `@@ def greet():`），再写改动。
- 改动行：' '（空格）开头是上下文，'-' 开头是删除，'+' 开头是新增。
- 每处改动前后默认各带 3 行上下文；上下文不足以唯一定位时，用 @@ 指明所在的类/函数。
- 在文件末尾追加时可以加一行 *** End of File。

示例：
*** Begin Patch
*** Add File: hello.txt
+Hello world
*** Update File: src/app.py
*** Move to: src/main.py
@@ def greet():
-print("Hi")
+print("Hello, world!")
*** Delete File: obsolete.txt
*** End Patch

注意：
- 必须写清楚操作头（Add / Delete / Update）。
- 新建文件时每一行也必须以 + 开头。
- 路径用相对工作目录的路径或绝对路径。
- 补丁应用成功后不用再读文件确认；失败会给出具体原因。
