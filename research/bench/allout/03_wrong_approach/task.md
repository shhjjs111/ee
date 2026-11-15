实现 `dedupe.py` 里的 `dedupe(items)`：**保序**去重，按 `==` 比较（不是按可哈希性）。

- 元素可能不可哈希（dict / list），不能直接 `set()` 或 `dict.fromkeys()`；
- 保留第一次出现的顺序；
- 空列表返回 `[]`；
- 不要修改 `public/` 下的测试文件。
