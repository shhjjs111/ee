---
id: 05-multifile-loopguard-switch
title: 给防死循环加总开关
kind: multi-file
check: python -m pytest -q tests/test_loopguard.py tests/test_config_shell.py tests/_eval_check.py
---
给防死循环加一个总开关：配置 `[loop_guard]` 里新增 `enabled`，默认 `true`（写进 `ee/ee/config.py` 的 DEFAULTS）。
`enabled = false` 时 `LoopGuard.from_config(...)` 得到的守卫永远不拦截（check 一直返回 ok，end_step 不提醒、不停止）。
保持现有行为和测试不变，补上测试。

## 验收测试
```python
import copy

from ee.config import DEFAULTS
from ee.core.loopguard import LoopGuard


def test_default_enabled():
    assert DEFAULTS["loop_guard"]["enabled"] is True


def test_disabled_never_blocks():
    g = LoopGuard.from_config({"enabled": False})
    for _ in range(10):
        assert g.check("same")[0] == "ok"
    for _ in range(30):
        assert g.end_step([("same", False)]) is None
    assert g.halt is None


def test_enabled_still_blocks():
    cfg = copy.deepcopy(DEFAULTS["loop_guard"])
    g = LoopGuard.from_config(cfg)
    assert [g.check("s")[0] for _ in range(3)][-1] == "block"
