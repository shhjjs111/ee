"""模型请求分段计时的反证（Wave 4 / T14，对应 ACC-30）。

现在只有"整回合墙钟"和一行日志里的总用时。出了问题只能靠
`duration_s - tool_s` 反推模型开销——实测踩过坑：把「一条命令跑满 600 秒」
误读成「每步模型都慢」。需要能分开的三段：

- **首包延迟**（请求发出 → 收到第一个流式分片）
- **首字延迟**（请求发出 → 第一段可见正文/推理）
- **总时长**（请求发出 → 流结束）

没有首包与首字之分时，无法区分"连得慢"和"想得久"——这两者的处置完全不同。
"""
from __future__ import annotations

import asyncio

import pytest

from conftest import finish, make_session, run, text_chunks


def _metric_of(events, name):
    from ee.core.events import Notice

    for e in events:
        if isinstance(e, Notice) and getattr(e, "metrics", None):
            if name in e.metrics:
                return e.metrics[name]
    return None


class TestRequestTiming:
    def test_request_reports_first_byte_and_first_token(self, ee_home, workspace):
        """一次正常请求结束后，事件流里要有首包与首字两段耗时。"""
        from conftest import collect

        s, fake = make_session(workspace, [text_chunks("你好") + finish("stop")], delay=0.02)
        ev = run(collect(s, "打个招呼"))

        metrics = None
        for e in ev:
            m = getattr(e, "metrics", None)
            if m:
                metrics = m
        assert metrics is not None, "请求结束后没有输出任何计时指标"
        for key in ("first_byte_ms", "first_token_ms", "total_ms"):
            assert key in metrics, f"缺少 {key}：{metrics}"
            assert metrics[key] >= 0
        # 首字不可能早于首包
        assert metrics["first_token_ms"] >= metrics["first_byte_ms"]
        # 总时长不短于首字
        assert metrics["total_ms"] >= metrics["first_token_ms"]
