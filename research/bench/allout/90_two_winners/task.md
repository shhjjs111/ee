实现 `slugify.py` 里的 `slugify(text)`：

- 转小写（Unicode casefold）；
- 把连续的空白/下划线/连字符压成一个 `-`；
- 去掉首尾的 `-`；
- 空串返回空串；
- 不要修改 `public/` 下的文件。

`public/criterion_runner.py` 是外层判据入口，也不要修改它。
