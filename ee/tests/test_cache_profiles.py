"""provider 缓存档案：块大小 / 最短可缓存长度 / TTL / 是否有缓存。

这些数字决定 `ee usage` 怎么扣结构性损耗：低于最短长度的 prompt 整段进不了缓存，
末尾不满一块的 token 必然 miss，空闲超过 TTL 的未命中是服务端过期。数字错了，
报告就会把结构性损耗说成「前缀不稳」，进而错误地调压缩水位。
"""
from __future__ import annotations

from ee.core.providers.profiles import profile_for


def test_deepseek_is_exact_prefix_with_64_token_blocks():
    p = profile_for("deepseek-v4.1-flash")
    assert p.cache_type == "exact-prefix"
    assert p.cache_granularity == 64
    assert p.cache_min_tokens == 64
    assert p.cache_ttl_s == 0, "DeepSeek 不声明 TTL：不知道就不猜"


def test_openai_declares_granularity_and_ttl():
    p = profile_for("gpt-5.2")
    assert p.cache_type == "implicit-prefix"
    assert p.cache_granularity == 128
    assert p.cache_min_tokens == 1024
    assert p.cache_ttl_s == 600


def test_anthropic_declares_minimum_and_ttl():
    p = profile_for("claude-opus-5-5")
    assert p.cache_type == "explicit-breakpoint"
    assert p.cache_min_tokens == 4096
    assert p.cache_ttl_s == 300


def test_providers_without_cache_say_so():
    assert profile_for("kimi-k3").cache_type == "none"
    assert profile_for("minimax-m3").cache_type == "none"


def test_unknown_family_does_not_guess():
    p = profile_for("some-unknown-model-xyz")
    assert p.cache_type == ""
    assert p.cache_granularity == 0
    assert p.cache_min_tokens == 0
    assert p.cache_ttl_s == 0
