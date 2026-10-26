---
id: 03-bugfix-truncate
title: 修工具输出截断条件
kind: bugfix
check: python -m pytest -q tests/test_truncate_and_tools.py tests/_eval_check.py
inject: [{"file": "ee/ee/tools/truncate.py", "old": "if len(lines) <= max_lines and size <= max_bytes:", "new": "if len(lines) <= max_lines or size <= max_bytes:"}]
revert_passes: true
---
`ee/ee/tools/truncate.py` 的 `truncate_output` 好像有问题：一行特别长、总共几百 KB 的命令输出没有被截断，整段塞进了上下文。按文件开头注释，超过 2000 行**或**超过 50KB 都应该截断。请修好，并补一个能覆盖这个情况的测试。

## 验收测试
```python
from ee.tools.truncate import truncate_output


def test_long_single_line_is_truncated(tmp_path):
    out, cut = truncate_output("x" * 200_000, tmp_path)
    assert cut and len(out) < 100_000


def test_many_short_lines_truncated(tmp_path):
    out, cut = truncate_output("\n".join(["a"] * 5000), tmp_path)
    assert cut


def test_small_not_truncated(tmp_path):
    assert truncate_output("hello\nworld", tmp_path) == ("hello\nworld", False)
```
