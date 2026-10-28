---
id: 01-bugfix-context-delta
title: 修 context-update 增量 bug
kind: bugfix
check: python -m pytest -q tests/test_prefix_cache.py
inject: [{"file": "ee/ee/core/context.py", "old": "if self.last.get(k) != v]", "new": "if self.last.get(k) == v]"}]
revert_passes: true
---
在 ee 目录下运行 `python -m pytest -q tests/test_prefix_cache.py` 有测试失败。找到根因并修好代码，不要改测试。修完再跑一遍确认通过。只跑这一个测试文件，不要跑全量测试套件——它要十几分钟。
