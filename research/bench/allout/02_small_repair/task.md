修复 `chunk.py` 里的 `chunks(items, size)`：

- 按 `size` 切分，最后一块可以不足 `size`；
- `size <= 0` 抛 `ValueError`；
- 空列表返回 `[]`；
- 不要修改 `public/` 下的测试文件。
