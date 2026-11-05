"""读不出的 .eeignore 不能静默失效（2026-11 审计 P2#60）。

`EeIgnore.load` 原来对 `OSError` 只是 `continue`：那份 .eeignore 里的排除规则
（通常是 `.env` / `*.pem` / `secrets/`）就此全部不生效，而用户完全不知道——
`permissions._eeignored` 只会看到「规则表里没有这条」，于是照常放行。

报告原文说「一条坏规则让整份忽略清单失效」，这一半不成立：`_translate` 是手写状态机，
`[` 这类坏模式会被当字面量转义，不会抛异常；`errors="replace"` 也兜住了编码问题。
真实可达的只有「文件读不出」（被占用 / 无权限）这一条路径，所以修的是「不再静默」，
而不是把加载失败改成拒绝一切（那会在权限异常时把用户锁死）。
"""
import logging
from pathlib import Path

from ee.core.eeignore import EeIgnore


def _ws(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir(exist_ok=True)      # 限定向上查找的边界，别扫到真实目录
    return tmp_path


def test_unreadable_eeignore_is_reported(tmp_path, monkeypatch, caplog):
    ws = _ws(tmp_path)
    f = ws / ".eeignore"
    f.write_text("*.secret\n", encoding="utf-8")

    real = Path.read_text

    def boom(self, *a, **k):
        if self.name == ".eeignore":
            raise PermissionError("另一个进程正占用")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", boom)

    with caplog.at_level(logging.WARNING, logger="ee.eeignore"):
        ig = EeIgnore.load(ws)

    assert ig.unreadable == [f], "读不出的文件要被记下来，不能只 continue 了事"
    assert "读不出" in caplog.text, caplog.text
    assert ig.rules == [], "这份文件确实没贡献任何规则——要的是把这个事实暴露出来"


def test_readable_eeignore_has_no_problems(tmp_path):
    ws = _ws(tmp_path)
    (ws / ".eeignore").write_text("*.secret\n", encoding="utf-8")

    ig = EeIgnore.load(ws)

    assert ig.unreadable == []
    assert len(ig.rules) == 1
