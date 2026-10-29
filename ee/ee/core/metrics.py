"""模型请求的分段计时（Wave 4 / T14）。

**为什么需要分段**：原先只有整回合墙钟和一行日志总用时。出问题时只能拿
`duration_s - tool_s` 反推模型开销——实测踩过坑：把「一条命令跑满 600 秒」
误读成「每步模型都慢」。

三段各有不同的处置方向：

| 指标 | 含义 | 慢的时候说明什么 |
|---|---|---|
| first_byte_ms | 请求发出 → 收到第一个流式分片 | 连接/网络/排队慢（换代理、重试） |
| first_token_ms | 请求发出 → 第一段可见正文/推理 | 模型想得久（换档位、缩短提示） |
| total_ms | 请求发出 → 流结束 | 输出太长或反复思考 |

口径约定：
- 都用**单调时钟**测时长（不受系统时间调整影响），单位毫秒；
- 首字一定不早于首包；
- 失败/取消也给出已测到的部分，不因为没走到结束就没有数据。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class RequestTiming:
    """一次模型请求的分段计时。t0 是单调时钟起点。"""
    t0: float = field(default_factory=time.monotonic)
    first_byte_at: float | None = None
    first_token_at: float | None = None

    def mark_byte(self) -> None:
        """收到第一个流式分片（任何内容，包括空 delta）。只记第一次。"""
        if self.first_byte_at is None:
            self.first_byte_at = time.monotonic()

    def mark_token(self) -> None:
        """收到第一段**可见**内容（正文或推理）。只记第一次。"""
        if self.first_token_at is None:
            self.first_token_at = time.monotonic()

    def _ms(self, t: float | None) -> int | None:
        return None if t is None else max(0, int((t - self.t0) * 1000))

    def as_dict(self) -> dict:
        """当前已测到的指标。没收到的项为 None，不用 0 冒充（0 意味着"立刻返回"）。"""
        return {
            "first_byte_ms": self._ms(self.first_byte_at),
            "first_token_ms": self._ms(self.first_token_at),
            "total_ms": self._ms(time.monotonic()),
        }
