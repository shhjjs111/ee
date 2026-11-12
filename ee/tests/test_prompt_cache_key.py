"""会话级缓存键（prompt_cache_key / prompt_cache_retention）。

背景：服务端只有把「同一对话的多轮请求」认成一条缓存线，前缀缓存才稳定。ee 已经做了
「前缀逐字节冻结 + 只追加」，但没告诉服务端「这些请求属于同一对话」。发了 key 之后
路由与过期都按会话粒度算，长对话不会因为换节点整段未命中。

守卫：默认一个字节都不发（严格网关对未知 body 字段直接 400）、支持时按会话 id 发、
被 400 拒绝后记住不再发、fork 子代理沿用父会话的 key。
"""
from ee.config import load_config
from ee.core.providers.openai_chat import ChatClient
from conftest import FakeClient


def _cfg():
    """造一份带 openai provider 的配置（默认配置只有 deepseek）。"""
    from pathlib import Path
    cfg = load_config(Path("."))
    cfg.data.setdefault("providers", {})["openai"] = {"base_url": "https://api.openai.com/v1",
                                                      "api_key_env": "OPENAI_API_KEY"}
    return cfg


def _spec(alias="gpt-5", cfg=None, **extra):
    cfg = cfg or _cfg()
    cfg.data["models"].setdefault(alias, {"provider": "openai", "model": alias, **extra})
    return cfg.resolve_model(alias)


def _client(spec, session_id="sess-123"):
    c = ChatClient(spec, client=object(), session_id=session_id)
    return c


def _params(c, effort=""):
    return c.build_params([{"role": "user", "content": "hi"}], None, effort)


# ------------------------------------------------------------------ 发不发

def test_not_sent_by_default():
    """deepseek 走隐式前缀缓存，档案没打开 → 一个字节都不发（不认识的字段会被 400）。"""
    c = _client(_spec("deepseek-flash", _cfg(), provider="deepseek"))
    p = _params(c)
    assert "prompt_cache_key" not in p
    assert "prompt_cache_retention" not in p


def test_sent_when_profile_supports():
    c = _client(_spec("gpt-5"))
    assert c.profile.supports_prompt_cache_key
    assert _params(c)["prompt_cache_key"] == "sess-123"


def test_not_sent_without_session_id():
    """没有会话 id 时不发半截 key：空的 / 重复的 key 会让不同对话挤同一条缓存线。"""
    c = _client(_spec("gpt-5"), session_id="")
    assert "prompt_cache_key" not in _params(c)


def test_ttl_only_when_configured():
    spec = _spec("gpt-5")
    assert _params(_client(spec)).get("prompt_cache_retention") is None
    spec.profile.prompt_cache_ttl = "24h"
    assert _params(_client(spec))["prompt_cache_retention"] == "24h"


def test_ttl_is_normalized_to_the_family_values():
    """OpenAI 只认 in_memory / 24h；Anthropic 的 5m / 1h 直接发会被 400 永久降级。"""
    for raw, want in (("in_memory", "in_memory"), ("5m", "in_memory"),
                      ("1h", "24h"), ("24h", "24h"), ("24H", "24h")):
        spec = _spec("gpt-5")
        spec.profile.prompt_cache_ttl = raw
        assert _params(_client(spec))["prompt_cache_retention"] == want, raw


def test_unknown_ttl_is_not_sent():
    """宁可退默认（几分钟），也不发一个必然 400、然后被永久降级的值。"""
    spec = _spec("gpt-5")
    spec.profile.prompt_cache_ttl = "永远"
    p = _params(_client(spec))
    assert p["prompt_cache_key"] == "sess-123"
    assert "prompt_cache_retention" not in p


def test_ttl_of_unknown_family_is_sent_as_is():
    """不在已知表里的家族原样发：网关可能有自己的写法，猜反而会错。"""
    spec = _spec("deepseek-flash", _cfg(), provider="deepseek")
    spec.profile = spec.profile.merged({"supports_prompt_cache_key": True, "prompt_cache_ttl": "1h"})
    assert _params(_client(spec))["prompt_cache_retention"] == "1h"


def test_quirks_file_is_written_atomically(tmp_path):
    """直接 write_text 写到一半会留下坏 JSON，下次启动当空表 → 所有降级记忆清零。"""
    import json

    c = ChatClient(_spec("gpt-5"), client=object(), state_path=tmp_path / "quirks.json")
    c.state.get(c.key)["no_prompt_cache_key"] = True
    c.state.save()
    assert json.loads((tmp_path / "quirks.json").read_text(encoding="utf-8"))[c.key]["no_prompt_cache_key"] is True
    assert not list(tmp_path.glob("*.tmp")), "临时文件必须被 replace 掉"


def test_remembers_rejection():
    """服务端 400 拒绝后记住，不再发（否则每次请求都白跑一次 400）。"""
    c = _client(_spec("gpt-5"))
    c.state.get(c.key)["no_prompt_cache_key"] = True
    assert "prompt_cache_key" not in _params(c)

    # 只拒绝 key：key 不发，但配置了 TTL 时其余参数照旧
    c2 = _client(_spec("gpt-5"))
    c2.profile = c2.profile.merged({"prompt_cache_ttl": "24h"})
    c2.state.get(c2.key)["no_prompt_cache_key"] = True
    assert "prompt_cache_key" not in _params(c2)

    # 只拒绝 TTL：key 照发，TTL 不发
    c3 = _client(_spec("gpt-5"))
    c3.profile = c3.profile.merged({"prompt_cache_ttl": "24h"})
    c3.state.get(c3.key)["no_prompt_ttl"] = True
    p = _params(c3)
    assert p["prompt_cache_key"] == "sess-123" and "prompt_cache_retention" not in p


def test_unsupported_param_recognizes_cache_fields():
    from ee.core.providers.transforms import unsupported_param
    assert unsupported_param('{"error":{"message":"Unrecognized request argument: prompt_cache_key"}}') \
        == "prompt_cache_key"
    assert unsupported_param('{"error":{"message":"unsupported parameter prompt_cache_retention"}}') \
        == "prompt_cache_retention"


# ------------------------------------------------------------------ 会话级接线

def test_session_passes_its_id(ee_home, workspace):
    """主客户端拿到的 key 是本会话 id；不同会话不同。"""
    from ee.core.session import Session
    cfg = load_config(workspace)
    cfg.data.setdefault("providers", {})["openai"] = {"base_url": "https://api.openai.com/v1"}
    cfg.data["models"].setdefault("gpt-5", {"provider": "openai", "model": "gpt-5"})
    s = Session(cfg, cwd=workspace, client=FakeClient([]), persist=False)
    s2 = Session(cfg, cwd=workspace, client=FakeClient([]), persist=False)
    a, b = s.cache_key(), s2.cache_key()
    assert a and b and a != b


def test_fork_child_inherits_parent_key(ee_home, workspace):
    """fork 子代理与父会话共用前缀和历史 → 必须共用缓存键，否则服务端不认为同一条缓存线。"""
    from ee.core.session import Session
    cfg = load_config(workspace)
    s = Session(cfg, cwd=workspace, client=FakeClient([]), persist=False)
    parent = s.cache_key()
    s._fork_session_id = parent          # 子代理构造时就是这么设的
    assert s.cache_key() == parent
