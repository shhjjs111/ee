"""错峰折扣计价（price discount）的行为契约。

背景：DeepSeek 等有错峰价（非峰时折扣），当前 profile 里写死峰时价，
导致费用按峰时高估。这里锁定「折扣只作用于输入+输出，缓存读取价不打折」的契约：
缓存命中已经是错峰前价格的 1/50，再打折会把已经极低的缓存成本算得更不准。
"""
from __future__ import annotations

from ee.core.providers.profiles import Price


def test_discount_applies_to_input_and_output_only():
    """输入 1M（未命中）+ 输出 1M，折扣 0.5 → 金额 = (1+1)*input*0.5。"""
    p = Price(input=2.0, output=8.0, cache_read=0.04, currency="¥")
    # 未命中：prompt=1M, hit=0, completion=1M
    cost = p.cost(prompt=1_000_000, completion=1_000_000, hit=0, discount=0.5)
    # miss=1M*2 + completion=1M*8 = 10 元，半价 = 5 元
    assert cost == 5.0


def test_discount_does_not_touch_cache_read():
    """缓存读取价不打折：命中 1M 的缓存部分仍按 cache_read 原价。"""
    p = Price(input=2.0, output=8.0, cache_read=0.04, currency="¥")
    cost = p.cost(prompt=1_000_000, completion=0, hit=1_000_000, discount=0.5)
    # 全部命中：1M * cache_read 0.04 = 0.04 元，不打折
    assert cost == 0.04


def test_no_discount_is_full_price():
    """不传 discount（默认 1.0）时行为不变。"""
    p = Price(input=2.0, output=8.0, currency="¥")
    cost = p.cost(prompt=1_000_000, completion=0, hit=0)
    assert cost == 2.0
