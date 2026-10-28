---
id: 02-bugfix-loopguard
title: 修防死循环计数 bug
kind: bugfix
check: python -m pytest -q tests/test_loopguard.py
inject: [{"file": "ee/ee/core/loopguard.py", "old": "self.streak += 1", "new": "self.streak += 2"}]
revert_passes: true
---
用户反馈：模型只重复调用了两次同一个工具，ee 就提示“检测到重复操作”把调用拦下了，默认应该是连续 3 次才拦。请找到原因并修复，跑 ee 目录下的 `tests/test_loopguard.py` 验证。只跑这个测试文件，不要跑全量套件。
