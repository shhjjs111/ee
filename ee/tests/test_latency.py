"""首包等待期间的心跳提示与接口级延迟统计。

**为什么要有**：实测本机使用记录（2026-11-01）72 次模型请求，首包中位 18.8 秒、
p90 35.9 秒、最大 69 秒。在那 20 秒里界面只有一句不动的「正在连接模型…」，
用户分不清是在排队还是卡死，也不知道该继续等还是换接口。这里守两件事：

1. 等待超过心跳间隔后，状态行会变成「已等待 N 秒」，并带上该接口最近的
   首包中位数作参照（样本不足时不给参照——2 个样本算出来的“中位”是误导）；
2. 统计按 base_url 归集、只放内存，不写用户真实目录。
"""
from __future__ import annotations

import asyncio

import pytest
from conftest import FakeStream, finish, text_chunks

from ee.config import ModelSpec
from ee.core import latency
from ee.core.providers.openai_chat import ChatClient
from ee.core.providers.profiles import profile_for


def _spec(base_url="http://127.0.0.1:1/v1"):
    return ModelSpec(alias="t", provider="p", model="m", base_url=base_url, api_key_env="",
                     profile=profile_for("m"), stored_key="sk-test-123456789")


class _SlowClient:
    """create 迟迟不返回响应对象：模拟上游排队（真实场景里就是那 18.8 秒）。"""

    def __init__(self, delay: float):
        self.delay = delay
        outer = self

        class _Completions:
            async def create(self, **params):
                await asyncio.sleep(outer.delay)
                return FakeStream(text_chunks("哦") + finish("stop"))

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


@pytest.fixture(autouse=True)
def _clean_stats():
    latency.reset()
    yield
    latency.reset()


# ---------------------------------------------------------------- 统计单元

def test_median_needs_enough_samples():
    """样本不足不给中位：宁可不说，也不给一个假参照。"""
    for ms in (23000, 3800):
        latency.record("http://a", ms)
    assert latency.median_ms("http://a") is None
    latency.record("http://a", 22000)
    assert latency.median_ms("http://a") == 22000


def test_median_of_even_count_is_average():
    for ms in (4000, 20000, 22000, 24000):
        latency.record("k", ms)
    assert latency.median_ms("k") == 21000


def test_unknown_and_missing_values_are_ignored():
    """没测到首包（None）不能记成 0——那会让统计看起来“很快”。"""
    latency.record("k", None)
    latency.record("k", -1)
    assert latency.samples("k") == 0


def test_keeps_only_recent_samples():
    for i in range(latency.KEEP + 10):
        latency.record("k", i)
    assert latency.samples("k") == latency.KEEP
    assert latency.median_ms("k") is not None


def test_key_is_per_interface_not_per_model():
    """同一中转站的不同模型共用统计：排队/网络慢通常是接口级现象。"""
    assert latency.key_for("http://a/v1", "x") == latency.key_for("http://a/v1", "y")
    assert latency.key_for("", "m") == "(model)m"


def test_heartbeat_text_includes_median_only_when_known():
    assert "中位" not in latency.heartbeat_text("t", 15, None)
    assert "已等待 15 秒" in latency.heartbeat_text("t", 15, None)
    text = latency.heartbeat_text("t", 20, 18800)
    assert "已等待 20 秒" in text and "18.8 秒" in text


# ---------------------------------------------------------------- 接线

def test_status_updates_while_waiting_for_first_byte():
    """上游迟迟不返回首包时，状态行会持续更新，而不是一句话钉在那里。"""
    cli = ChatClient(_spec(), client=_SlowClient(0.35), retries=0)
    cli.heartbeat_s = 0.1
    statuses: list[str] = []

    async def go():
        async for kind, val in cli.stream([{"role": "user", "content": "hi"}], None):
            if kind == "status":
                statuses.append(val)

    asyncio.run(go())
    waited = [s for s in statuses if "还没有返回首包" in s]
    assert waited, statuses
    assert "已等待" in waited[0]
    assert statuses[-1] == "" or statuses[-1] == "已连接，等待模型输出…" or "已连接" in statuses[-1]


def test_status_shows_interface_median_once_enough_samples():
    """积累够样本后，等待提示里要带上该接口的中位数作为参照。"""
    key = latency.key_for("http://127.0.0.1:1/v1", "m")
    for ms in (18000, 19000, 20000):
        latency.record(key, ms)
    cli = ChatClient(_spec(), client=_SlowClient(0.35), retries=0)
    cli.heartbeat_s = 0.1
    statuses: list[str] = []

    async def go():
        async for kind, val in cli.stream([{"role": "user", "content": "hi"}], None):
            if kind == "status":
                statuses.append(val)

    asyncio.run(go())
    assert any("中位 19.0 秒" in s for s in statuses), statuses


def test_first_byte_is_recorded_after_a_successful_request():
    """一次成功请求要把首包记进统计，否则提示永远没有参照可用。"""
    cli = ChatClient(_spec(), client=_SlowClient(0.0), retries=0)

    async def go():
        async for _ in cli.stream([{"role": "user", "content": "hi"}], None):
            pass

    asyncio.run(go())
    key = latency.key_for("http://127.0.0.1:1/v1", "m")
    assert latency.samples(key) == 1


# ---------------------------------------------------------------- 启动回填

START = ("2026-11-01 16:{s:02d}:00,000 INFO [ee.model] 模型请求开始：{alias}（{url}，"
         "系统代理 http://127.0.0.1:7897（来自系统代理设置））messages=82 tools=18 第 1 次")
END = ("2026-11-01 16:{s:02d}:25,000 INFO [ee.model] 模型请求结束：{alias} 用时 22.3s finish=stop "
       "文本 1192 字 工具调用 0 个 usage={{'prompt': 165246}} 首包={ms}ms 首字={ms}ms")


def _log_lines(url, ms_list, alias="t"):
    out = []
    for i, ms in enumerate(ms_list):
        out.append(START.format(s=i, alias=alias, url=url))
        out.append(END.format(s=i, alias=alias, ms=ms))
    return out


def test_backfill_reads_first_byte_from_log(tmp_path):
    """重启后统计清零，但慢接口往往刚开工就慢：从日志尾部回填，第一次慢请求就有参照。"""
    f = tmp_path / "ee-backend.log"
    f.write_text("\n".join(_log_lines("https://x/v1", [18000, 19000, 20000])) + "\n", encoding="utf-8")
    assert latency.backfill_from_log(f) == 3
    key = latency.key_for("https://x/v1")
    assert latency.samples(key) == 3
    assert latency.median_ms(key) == 19000


def test_backfill_uses_alias_to_pair_address_with_first_byte(tmp_path):
    """两个接口的记录混在同一个文件里也不能串：按 alias 把开始行的地址和结束行的首包配对。"""
    f = tmp_path / "log"
    f.write_text("\n".join(_log_lines("https://a/v1", [1000, 1000, 1000], alias="a")
                         + _log_lines("https://b/v1", [9000, 9000, 9000], alias="b")) + "\n",
                 encoding="utf-8")
    latency.backfill_from_log(f)
    assert latency.median_ms(latency.key_for("https://a/v1")) == 1000
    assert latency.median_ms(latency.key_for("https://b/v1")) == 9000


def test_backfill_falls_back_to_rotated_file(tmp_path):
    """刚轮转过、当前文件几乎空时，往前翻备份，否则一启动照样没参照。"""
    (tmp_path / "log.1").write_text("\n".join(_log_lines("https://x/v1", [18000, 19000])) + "\n",
                                   encoding="utf-8")
    cur = tmp_path / "log"
    cur.write_text("\n".join(_log_lines("https://x/v1", [20000])) + "\n", encoding="utf-8")
    assert latency.backfill_from_log(cur) == 3
    assert latency.median_ms(latency.key_for("https://x/v1")) == 19000


def test_backfill_keeps_only_recent_samples(tmp_path):
    """回填同样受 KEEP 限制，且保留的是最近的（文件末尾）那些。"""
    f = tmp_path / "log"
    f.write_text("\n".join(_log_lines("https://x/v1", list(range(10, 10 + latency.KEEP + 10)))) + "\n",
                 encoding="utf-8")
    latency.backfill_from_log(f)
    key = latency.key_for("https://x/v1")
    assert latency.samples(key) == latency.KEEP
    assert latency.median_ms(key) == 29        # 最后 20 条是 20..39，偶数个取中间两个 (29,30) 的平均向下取整


def test_backfill_skips_default_interface_and_orphan_lines(tmp_path):
    """没配 base_url 的默认接口对不上运行时的 key；只有结束行（开始行被轮转截掉）也跳过。"""
    f = tmp_path / "log"
    f.write_text("\n".join(_log_lines("默认接口", [18000, 19000, 20000])
                         + [END.format(s=30, alias="t", ms=18000)]) + "\n", encoding="utf-8")
    assert latency.backfill_from_log(f) == 0
    assert not latency._data


def test_backfill_only_takes_recent_requests(tmp_path):
    """日志里的陈年记录不代表“现在这个接口”：只回填最近 hits_limit 次。"""
    f = tmp_path / "log"
    f.write_text("\n".join(_log_lines("https://x/v1", [1000] * 10 + [30000] * 5)) + "\n",
                 encoding="utf-8")
    assert latency.backfill_from_log(f, hits_limit=5) == 5
    assert latency.samples(latency.key_for("https://x/v1")) == 5
    assert latency.median_ms(latency.key_for("https://x/v1")) == 30000


def test_backfill_is_idempotent_and_missing_file_is_harmless(tmp_path):
    """serve/exec 两条入口都会配日志：重复调用不能把同一批记录记两遍。"""
    assert latency.backfill_from_log(tmp_path / "nope.log") == 0
    f = tmp_path / "log"
    f.write_text("\n".join(_log_lines("https://x/v1", [18000, 19000, 20000])) + "\n", encoding="utf-8")
    latency.backfill_from_log(f)
    latency.backfill_from_log(f)
    assert latency.samples(latency.key_for("https://x/v1")) == 3
