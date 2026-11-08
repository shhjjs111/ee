"""D5-9 推理强度（参数构造、降级重试、三层取值、推理 token）与按用途分模型（取模型、回退、按实际模型计价）。"""
import asyncio
import json

import pytest
from conftest import FakeClient, FakeError, collect, finish, make_session, run, text_chunks

from ee.config import load_config
from ee.core import reasoning as RZ
from ee.core.events import Notice, TurnDone
from ee.core.providers.openai_chat import ChatClient, ModelError, parse_usage


def spec(name, cfg=None):
    from pathlib import Path
    cfg = cfg or load_config(Path("."))
    cfg.data["models"].setdefault(name, {"provider": "deepseek", "model": name})
    return cfg.resolve_model(name)


def params(model, effort, state=None):
    c = ChatClient(spec(model), client=object())
    if state:
        c.state.get(c.key).update(state)
    return c.build_params([{"role": "user", "content": "hi"}], None, effort)


# ------------------------------------------------------------------ 参数构造

@pytest.mark.parametrize("effort,top,body", [
    ("", None, None),
    ("low", "low", None),
    ("medium", None, None),            # DeepSeek 没有“中”档：旧值 中 = 服务端默认 high → 不发参数
    ("high", "max", None),             # 旧值 高 以前就发 max → 最高
    ("max", "max", None),
    ("off", None, {"thinking": {"type": "disabled"}}),
])
def test_deepseek_effort_params(effort, top, body):
    p = params("deepseek-flash", effort)
    assert p.get("reasoning_effort") == top
    assert (p.get("extra_body") or None) == body


def test_effort_params_by_family():
    assert params("o3-mini", "medium")["reasoning_effort"] == "medium"
    assert params("gpt-5.1", "high")["reasoning_effort"] == "high"
    assert "reasoning_effort" not in params("gpt-4o", "high")         # 不支持：不发
    assert "extra_body" not in params("gpt-4o", "off")
    assert params("qwen-max", "off")["extra_body"] == {"enable_thinking": False}
    assert "reasoning_effort" not in params("qwen-max", "high")
    assert params("glm-4.6", "off")["extra_body"] == {"thinking": {"type": "disabled"}}
    p = params("kimi-k2", "off")
    assert "extra_body" not in p and "reasoning_effort" not in p
    # 记住了不支持：不再发
    assert "reasoning_effort" not in params("deepseek-flash", "high", {"no_reasoning_effort": True})
    assert "extra_body" not in params("deepseek-flash", "off", {"no_thinking": True})


def test_cn_families_effort_reaches_max():
    """国产家族也能调推理强度——以前只配了「开关思考」，强度拉不上去。

    依据（2026-11 查阅官方文档）：
    - GLM：reasoning_effort 仅 GLM-5.2+，max/high/low（docs.z.ai/guides/capabilities/thinking）
    - Qwen：qwen3.x 是 low/medium/xhigh，默认 xhigh（platform.qianwenai.com 思考模式）
    - Kimi：K3/K3.1-Pro 是 low/high/max，默认 max；K2.x 没有这个参数
    - 豆包：low/medium/high，xhigh/max 被服务端映射成 high
    - MiniMax：M3.1-Flash 是 low/medium/high/xhigh/max，默认 max
    """
    assert params("glm-5.3", "max")["reasoning_effort"] == "max"
    assert params("qwen3.8-max", "max")["reasoning_effort"] == "xhigh"    # ee 的 max → 它家的 xhigh
    assert params("kimi-k3.1-pro", "max")["reasoning_effort"] == "max"
    assert params("doubao-seed-2.1-pro", "max")["reasoning_effort"] == "high"
    assert params("MiniMax-M3.1-Flash", "max")["reasoning_effort"] == "max"
    # 老型号不认这个参数，一个字节都不能发（发了会 400）
    for old in ("glm-4.6", "qwen-max", "kimi-k2.6", "doubao-seed-1.6", "MiniMax-M2.5"):
        assert "reasoning_effort" not in params(old, "max"), old
    # 始终推理的模型不能发 thinking
    assert "extra_body" not in params("kimi-k3.1-pro", "off")


def test_high_stays_high_not_max():
    """「高」不能被悄悄升成「最高」——用户选什么就发什么。

    各家最高档的写法不同（Qwen=xhigh、GPT-5.2=xhigh、豆包=high），但中间档必须
    保持原值。以前用 effort_aliases={high: max} 实现映射，副作用是用户点「高」
    拿到的是最高档，等于偷偷替换了选择。
    """
    assert params("gpt-5.2", "high")["reasoning_effort"] == "high"
    assert params("gpt-5.2", "max")["reasoning_effort"] == "xhigh"
    # Qwen3.8-max 官方只有 low/medium/xhigh，**没有 high**：发 high 是非法值会 400，
    # 所以档位表里根本没有 high，旧值 high 经 effort_aliases 落到 max（即 xhigh）。
    assert RZ.levels(spec("qwen3.8-max").profile) == ["low", "medium", "max"]
    assert params("qwen3.8-max", "medium")["reasoning_effort"] == "medium"
    assert params("qwen3.8-max", "max")["reasoning_effort"] == "xhigh"
    assert params("qwen3.8-max", "high")["reasoning_effort"] == "xhigh"   # 旧值 high → 别名到 max → xhigh
    assert params("MiniMax-M3.1-Flash", "high")["reasoning_effort"] == "high"
    assert params("MiniMax-M3.1-Flash", "max")["reasoning_effort"] == "max"
    # 豆包服务端把 xhigh/max 都映射成 high，所以这两档发出相同的值是预期行为
    assert params("doubao-seed-2.1-pro", "high")["reasoning_effort"] == "high"
    assert params("doubao-seed-2.1-pro", "max")["reasoning_effort"] == "high"
    # 老型号一个字节都不发
    for old in ("gpt-5.1", "gpt-5", "o3-mini"):
        assert params(old, "high")["reasoning_effort"] == "high", old
        assert params(old, "max")["reasoning_effort"] == "high", old


def test_gpt_52_pro_has_no_low():
    """gpt-5.2-pro 官方只支持 medium / high / xhigh：给「低」就是必 400。"""
    p = spec("gpt-5.2-pro").profile
    assert RZ.levels(p) == ["medium", "high", "max"]
    assert params("gpt-5.2-pro", "max")["reasoning_effort"] == "xhigh"
    assert params("gpt-5.2-pro", "medium")["reasoning_effort"] == "medium"
    assert "reasoning_effort" not in params("gpt-5.2-pro", "low")     # 不支持：不发，而不是发个非法值
    # 普通 5.2 仍有「低」
    assert params("gpt-5.2", "low")["reasoning_effort"] == "low"


def test_profiles_only_promise_what_the_docs_say():
    """界面置灰与否必须和官方文档一致：许诺了却不生效，比置灰更难发现。

    - MiniMax-M3（非 Flash）：官方「reasoning_effort 仅 MiniMax-M3.1-Flash 生效」→ 不给档
    - GLM-5.0 / 5.1：z.ai「only supported by GLM-5.2 and above」→ 不给档
    - GLM-5.2：low 在服务端也落到 high，文案要写明
    - Qwen3.8 默认 xhigh；其它 Qwen3.x 没有官方依据，不写「默认已是最高档」
    """
    for model in ("MiniMax-M3", "glm-5", "glm-5.1", "chatglm-5"):
        assert RZ.support(spec(model).profile)["levels"] == [], model
    assert RZ.levels(spec("MiniMax-M3.1-Flash").profile)
    assert RZ.levels(spec("glm-5.2").profile) == ["low", "high", "max"]
    assert RZ.labels(spec("glm-5.2").profile)["low"] == "低（服务端按 high）"
    assert RZ.labels(spec("qwen3.8-max").profile)[""] == "默认（已是最高档）"
    assert RZ.labels(spec("qwen3.7-plus").profile)[""] == "默认"


def test_claude_effort_is_not_configured():
    """Claude **不配**强度档：OpenAI 兼容层会把 reasoning_effort 静默忽略。

    依据（2026-11 查阅 https://platform.claude.com/docs/en/api/openai-sdk）：
    字段支持表把 `reasoning_effort` 明确列为 Ignored，表里也没有 `output_config`，
    且文档原话「Most unsupported fields are silently ignored rather than producing errors」。
    既然原生 Messages API 的 output_config.effort 走兼容层会被静默丢弃（**不报错**），
    那 openai_chat 的 400 降级重试永远不会触发——用户看到"已设为最高档"而实际毫无变化，
    是最坏的一种假装生效。所以内置档案一个字节都不发，界面置灰。

    需要时可以在 ~/.ee/config.toml 里按模型覆盖 effort_param（见 claude.toml 注释）。
    """
    for model in ("claude-opus-5-5", "claude-sonnet-4-6", "claude-3-5-sonnet"):
        assert RZ.support(spec(model).profile)["effort"] is False, model
        p = params(model, "max")
        assert "reasoning_effort" not in p and "extra_body" not in p, model


def test_gemini_and_gpt_effort_levels():
    """Gemini 3/4 走 OpenAI 兼容层顶层 reasoning_effort；GPT-5.2+ 能到 xhigh。"""
    assert params("gemini-3-pro", "high")["reasoning_effort"] == "high"
    assert "reasoning_effort" not in params("gemini-2.5-pro", "high")
    # GPT-5.2 及更新能到 xhigh；5.1/o 系列只到 high
    assert params("gpt-5.2", "max")["reasoning_effort"] == "xhigh"
    assert params("gpt-5.1", "max")["reasoning_effort"] == "high"


def test_has_reasoning_params_sees_nested_effort():
    """嵌套参数也算「发了推理参数」，否则 400 时的降级重试会被跳过。

    内置档案里已经没有嵌套写法的模型（Claude 那条实测会被兼容层忽略，已去掉），
    但点分路径是 effort_param 的公开写法，用户可以在 ~/.ee/config.toml 里覆盖成
    output_config.effort / google.thinking_config.thinking_level 这类嵌套参数。
    键名不以 effort 结尾时只看顶层会判成「没发」，400 就走不到去掉参数的重试。
    """
    from ee.core.providers.profiles import Profile

    nested = Profile(family="default", effort_param="output_config.effort",
                     effort_levels=["low", "medium", "high", "max"])
    top, body = RZ.request_params(nested, "max")
    assert body == {"output_config": {"effort": "max"}} and not top
    assert ChatClient.has_reasoning_params({"model": "x", "extra_body": body}) is True

    level = Profile(family="default", effort_param="google.thinking_config.thinking_level",
                    effort_levels=["low", "medium", "high"])
    _, body2 = RZ.request_params(level, "high")
    assert ChatClient.has_reasoning_params({"model": "x", "extra_body": body2}) is True

    # 顶层名和 thinking 老路径仍然认得
    assert ChatClient.has_reasoning_params(params("gpt-5.2", "max")) is True
    assert ChatClient.has_reasoning_params(params("deepseek-flash", "off")) is True
    # 没发就是没发
    assert ChatClient.has_reasoning_params(params("gpt-4o", "max")) is False
    assert ChatClient.has_reasoning_params(params("claude-opus-5-5", "max")) is False


def test_nested_effort_400_strips_and_remembers(tmp_path):
    """嵌套参数形态的 400 也要能去掉重试一次（用户自定义档案时会走到这条路）。"""
    from ee.core.providers.profiles import Profile
    c, fake = _client([FakeError(400, "Invalid request body"), text_chunks("ok") + finish("stop")], tmp_path)
    c.profile = Profile(family="default", effort_param="output_config.effort",
                        effort_levels=["low", "medium", "high", "max"])
    evs = run(_drain(c, "max"))
    assert fake.calls[0]["extra_body"] == {"output_config": {"effort": "max"}}
    assert "extra_body" not in fake.calls[1]
    notes = [v for k, v in evs if k == "notice"]
    assert any("去掉推理强度参数重试" in n for n in notes)
    assert c.state.get(c.key).get("no_reasoning_effort") is True


def test_always_thinking_models_do_not_offer_off():
    """官方明确「不能关闭思考」的型号不给「关闭思考」，而不是等服务端 400。

    - GLM-5.3 / 5.3-FLASH：官方「强制思考不能关闭」
    - MiniMax-M3.1-Flash：官方「始终开启思考，不支持关闭」
    这些变体清了 thinking_param；但 for_model 只取第一个匹配上的变体，
    所以强度档必须一起带上，不能被顺手丢掉。
    """
    for model in ("glm-5.3", "glm-5.3-flash", "MiniMax-M3.1-Flash"):
        p = spec(model).profile
        assert RZ.support(p)["off"] is False, model
        assert RZ.support(p)["levels"], f"{model} 的强度档被 thinking 变体吃掉了"
        top, body = RZ.request_params(p, "off")
        assert not body, model
    # 同家族里能关的型号不受影响
    assert RZ.support(spec("glm-5.2").profile)["off"] is True
    assert RZ.support(spec("MiniMax-M3").profile)["off"] is True


def test_custom_effort_param_goes_to_extra_body(workspace):
    cfg = load_config(workspace)
    cfg.data["models"]["mine"] = {"provider": "deepseek", "model": "my-model", "effort_param": "effort_level",
                                  "thinking_param": "enable_thinking"}
    c = ChatClient(cfg.resolve_model("mine"), client=object())
    p = c.build_params([], None, "low")
    assert p["extra_body"] == {"effort_level": "low"} and "reasoning_effort" not in p
    assert c.build_params([], None, "off")["extra_body"] == {"enable_thinking": False}


def test_support_and_normalize():
    ds = RZ.support(spec("deepseek-flash").profile)
    assert (ds["effort"], ds["off"], ds["levels"]) == (True, True, ["low", "max"])
    g = RZ.support(spec("gpt-4o").profile)
    assert (g["effort"], g["off"], g["levels"]) == (False, False, [])
    assert RZ.normalize("高") == "high" and RZ.normalize("关闭思考") == "off" and RZ.normalize("默认") == ""
    assert RZ.normalize("max") == "max" and RZ.normalize("最高") == "max" and RZ.normalize("瞎写") is None


# ------------------------------------------------------------------ 降级重试

def _client(script, tmp_path, model="deepseek-flash"):
    fake = FakeClient(script)
    return ChatClient(spec(model), client=fake, state_path=tmp_path / "quirks.json"), fake


async def _drain(c, effort):
    out = []
    async for ev in c.stream([{"role": "user", "content": "hi"}], None, effort=effort):
        out.append(ev)
    return out


def test_400_named_param_degrades_and_remembers(tmp_path):
    c, fake = _client([FakeError(400, "Unrecognized request argument supplied: reasoning_effort"),
                       text_chunks("ok") + finish("stop")], tmp_path)
    evs = run(_drain(c, "high"))
    assert fake.calls[0]["reasoning_effort"] == "max" and "reasoning_effort" not in fake.calls[1]
    assert any(k == "notice" and "reasoning_effort" in v for k, v in evs)
    st = json.loads((tmp_path / "quirks.json").read_text(encoding="utf-8"))
    assert st[c.key]["no_reasoning_effort"] is True
    assert "reasoning_effort" not in c.build_params([], None, "high")


def test_400_unclear_strips_reasoning_once_then_remembers(tmp_path):
    c, fake = _client([FakeError(400, "Invalid request body"), text_chunks("ok") + finish("stop")], tmp_path)
    evs = run(_drain(c, "off"))
    assert fake.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "extra_body" not in fake.calls[1]
    notes = [v for k, v in evs if k == "notice"]
    assert any("去掉推理强度参数重试" in n for n in notes) and any("已去掉并记住" in n for n in notes)
    assert c.state.get(c.key).get("no_thinking") is True and not c.state.get(c.key).get("no_reasoning_effort")


def test_400_unclear_twice_raises_and_does_not_remember(tmp_path):
    c, fake = _client([FakeError(400, "context too long"), FakeError(400, "context too long")], tmp_path)
    with pytest.raises(ModelError):
        run(_drain(c, "high"))
    assert len(fake.calls) == 2 and not c.state.get(c.key).get("no_reasoning_effort")


def test_400_without_reasoning_params_not_retried(tmp_path):
    c, fake = _client([FakeError(400, "bad request")], tmp_path)
    with pytest.raises(ModelError):
        run(_drain(c, ""))
    assert len(fake.calls) == 1


def test_thinking_mode_passback_error_is_not_unsupported_param():
    from ee.core.providers.transforms import unsupported_param
    assert unsupported_param("The reasoning_content in the thinking mode must be passed back") is None
    assert unsupported_param("unknown parameter: thinking") == "thinking"
    assert unsupported_param("enable_thinking is not supported") == "enable_thinking"


# ------------------------------------------------------------------ 取值：全局一个档位 + 会话

def test_effort_global_default_and_session_override(ee_home, workspace):
    s, fake = make_session(workspace, [text_chunks("ok") + finish("stop")])
    assert s.effort == ""
    s.config.data["reasoning"]["effort"] = "low"
    s2, _ = make_session(workspace, [])
    assert s2.effort == ""                     # 已经建好的配置对象，改内存不影响它
    s.set_effort("高")                          # 会话里选：DeepSeek 上旧写法 高 → 最高
    run(collect(s, "hi"))
    assert fake.calls[0]["reasoning_effort"] == "max"
    # 会话里选的只管本会话，不落盘
    assert not (ee_home / "state" / "model_prefs.json").exists()
    s3, _ = make_session(workspace, [])
    assert s3.effort == ""                     # 新会话回到全局默认
    s4, _ = make_session(workspace, [], model="deepseek-pro")
    assert s4.effort == ""                     # 换模型不影响：档位不按模型分
    (ee_home / "config.toml").write_text('[reasoning]\neffort = "max"\n', encoding="utf-8")
    s5, _ = make_session(workspace, [], model="deepseek-pro")
    assert s5.effort == "max"                  # 全局一个值，对所有模型生效
    s6, _ = make_session(workspace, [])
    assert s6.effort == "max"
    s6.set_effort("默认")
    assert s6.effort == ""
    with pytest.raises(ValueError):
        s6.set_effort("超级高")


def test_model_reasoning_effort_key_is_ignored(ee_home, workspace):
    (ee_home / "config.toml").write_text('[reasoning]\neffort = "low"\n[models.deepseek-pro]\nprovider = "deepseek"\n'
                                         'model = "deepseek-v4-pro"\nreasoning_effort = "off"\n', encoding="utf-8")
    s, _ = make_session(workspace, [], model="deepseek-pro")
    assert s.effort == "low"                  # [models.X] reasoning_effort 不再盖掉面板上的选择


def test_switch_model_keeps_session_effort(ee_home, workspace):
    s, _ = make_session(workspace, [])
    s.set_effort("max")
    s.set_model("deepseek-pro")
    assert s.effort == "max"                   # 换模型后会话里选的档位照旧生效


def test_unsupported_level_reported_and_not_sent(ee_home, workspace):
    cfg = load_config(workspace)
    cfg.data["models"]["g"] = {"provider": "deepseek", "model": "gpt-4o"}
    from ee.core.session import Session
    fake = FakeClient([text_chunks("ok") + finish("stop")])
    s = Session(cfg, model="g", cwd=workspace, client=fake, persist=False)
    sup = s.effort_support()
    assert (sup["effort"], sup["off"]) == (False, False)
    msg = s.set_effort("high")
    assert "不支持" in msg
    run(collect(s, "hi"))
    assert "reasoning_effort" not in fake.calls[0]


def test_reasoning_tokens_parsed_and_totaled(ee_home, workspace):
    u = parse_usage({"prompt_tokens": 10, "completion_tokens": 50, "completion_tokens_details": {"reasoning_tokens": 30}})
    assert u["reasoning"] == 30
    assert "reasoning" not in parse_usage({"prompt_tokens": 1, "completion_tokens": 1})
    chunk = [{"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 40, "prompt_cache_hit_tokens": 0,
                                       "completion_tokens_details": {"reasoning_tokens": 25}}}]
    s, _ = make_session(workspace, [text_chunks("ok") + [{"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
                                    + chunk])
    evs = run(collect(s, "hi"))
    t = s.stats.totals()
    assert t["reasoning"] == 25 and t["by_model"]["deepseek-flash"]["reasoning"] == 25
    assert "reasoning" not in make_session(workspace, [])[0].stats.totals()     # 没给就不显示
    assert [e for e in evs if isinstance(e, TurnDone)][-1].reason == "completed"


def test_side_requests_do_not_carry_session_effort(ee_home, workspace):
    s, fake = make_session(workspace, [text_chunks("feat: x") + finish("stop")])
    s.set_effort("off")
    out = run(s.side_complete("写提交信息", "diff", role="commit"))
    assert out == "feat: x"
    assert "extra_body" not in fake.calls[0] and "reasoning_effort" not in fake.calls[0]


# ------------------------------------------------------------------ 按用途分模型

PRO = {"provider": "deepseek", "model": "deepseek-v4-pro"}


def test_role_model_and_fallback(ee_home, workspace):
    cfg = load_config(workspace)
    assert cfg.role_model("title") is None                      # 留空 → 跟随主模型
    cfg.data["roles"]["title"] = "deepseek-pro"
    cfg.data["roles"]["compact"] = "不存在的模型"
    cfg.data["roles"]["commit"] = "deepseek/deepseek-chat"      # provider/模型 写法
    assert cfg.role_model("title") == "deepseek-pro"
    assert cfg.role_model("compact") is None                    # 配错 → 跟随主模型
    assert cfg.role_model("commit") == "deepseek/deepseek-chat"
    from ee.core.session import Session
    s = Session(cfg, cwd=workspace, client=FakeClient([]), persist=False)
    assert s.role_spec("title").alias == "deepseek-pro"
    assert s.role_spec("compact").alias == "deepseek-flash"
    assert s.role_spec(None).alias == "deepseek-flash"


def test_legacy_models_roles_table_is_normalized(ee_home, workspace):
    (ee_home / "config.toml").write_text('[models.roles]\ntitle = "deepseek-pro"\nexplore = "deepseek-flash"\n',
                                         encoding="utf-8")
    cfg = load_config(workspace)
    assert "roles" not in cfg.model_names()
    assert cfg.role_model("title") == "deepseek-pro" and cfg.role_model("explore") == "deepseek-flash"
    from ee.server import settings as S
    out = S.get_settings()["settings"]
    assert "roles" not in out["models"] and out["roles"]["title"] == "deepseek-pro"
    S.set_settings({"models": out["models"]})             # 保存模型表不会把旧写法弄丢或报错
    cfg = load_config(workspace)
    assert cfg.role_model("title") == "deepseek-pro"


def test_side_request_uses_role_model_and_its_price(ee_home, workspace):
    s, fake = make_session(workspace, [text_chunks("修复登录") + finish("stop", prompt=1_000_000, completion=0)])
    s.config.data["roles"]["title"] = "deepseek-pro"
    run(s.side_complete("起标题", "帮我修登录", role="title"))
    assert fake.calls[0]["model"] == "deepseek-v4-pro"
    t = s.stats.totals()
    assert t["cost"] == pytest.approx(9.0)                          # pro 的未命中单价 9 元/百万
    assert t["by_model"] == {"deepseek-pro": {"requests": 1, "prompt": 1_000_000, "completion": 0, "reasoning": 0,
                                              "cost": pytest.approx(9.0), "currency": "¥"}}


def test_commit_message_uses_commit_role(ee_home, workspace, monkeypatch):
    from ee.core import gitinfo
    monkeypatch.setattr(gitinfo, "staged_diff", lambda cwd: "diff --git a b")
    monkeypatch.setattr(gitinfo, "recent_log", lambda cwd: "")
    s, fake = make_session(workspace, [text_chunks("fix: a") + finish("stop")])
    s.config.data["roles"]["commit"] = "deepseek-pro"
    msg, stage_all = run(s.commit_message())
    assert msg == "fix: a" and stage_all is False and fake.calls[0]["model"] == "deepseek-v4-pro"


def test_compact_role_model_and_cost(ee_home, workspace):
    s, fake = make_session(workspace, [
        text_chunks("好的") + finish("stop", prompt=1_000_000, completion=0),
        text_chunks("## 摘要\n做完了") + finish("stop", prompt=1_000_000, completion=0),
    ])
    run(collect(s, "第一件事"))
    s.config.data["roles"]["compact"] = "deepseek-pro"
    res = run(s.compact())
    assert res["ok"]
    assert [c["model"] for c in fake.calls] == ["deepseek-flash", "deepseek-v4-pro"]
    # 压缩请求仍带完整历史（同一 system）
    assert fake.calls[1]["messages"][0] == fake.calls[0]["messages"][0]
    bm = s.stats.totals()["by_model"]
    assert bm["deepseek-flash"]["cost"] == pytest.approx(2.0) and bm["deepseek-pro"]["cost"] == pytest.approx(9.0)
    assert s.stats.cost == pytest.approx(11.0)


def test_compact_role_too_small_falls_back_to_transcript(ee_home, workspace):
    s, fake = make_session(workspace, [text_chunks("好的") + finish("stop"), text_chunks("摘要") + finish("stop")])
    run(collect(s, "第一件事" * 50))
    s.config.data["models"]["tiny"] = {"provider": "deepseek", "model": "deepseek-tiny", "context_window": 100}
    s.config.data["roles"]["compact"] = "tiny"
    evs = []
    res = run(s.compact(evs.append))
    assert res["ok"]
    assert fake.calls[1]["model"] == "deepseek-tiny" and "tools" not in fake.calls[1]      # 侧路精简记录
    assert any(isinstance(e, Notice) and "改用精简记录" in e.text for e in evs)


def test_plan_mode_and_review_turn_use_role_models(ee_home, workspace):
    s, fake = make_session(workspace, [text_chunks("方案") + finish("stop"), text_chunks("审完") + finish("stop"),
                                       text_chunks("普通") + finish("stop")], mode="plan")
    s.config.data["roles"].update({"plan": "deepseek-pro", "review": "deepseek-pro"})
    run(collect(s, "出个方案"))
    s.set_mode("default")
    s._turn_role = "review"
    run(collect(s, "审查"))
    s._turn_role = None
    run(collect(s, "继续"))
    assert [c["model"] for c in fake.calls] == ["deepseek-v4-pro", "deepseek-v4-pro", "deepseek-flash"]
    # 同一个冻结的 system：换用途模型不改前缀
    assert fake.calls[0]["messages"][0] == fake.calls[2]["messages"][0]
    assert set(s.stats.totals()["by_model"]) == {"deepseek-pro", "deepseek-flash"}


def test_review_rpc_turn_uses_review_role(ee_home, workspace):
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    (ee_home / "config.toml").write_text('[roles]\nreview = "deepseek-pro"\n', encoding="utf-8")
    fake = FakeClient([text_chunks("没问题") + finish("stop"), text_chunks("好") + finish("stop")])
    from ee.server.app import Backend

    async def main():
        b = Backend(home=ee_home, client_factory=lambda sp: ChatClient(sp, client=fake))
        t = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]
        await b.call("review/start", {"thread_id": t["id"], "kind": "custom", "instructions": "看看"})
        await b.threads[t["id"]].task
        await b.call("turn/start", {"thread_id": t["id"], "text": "谢谢"})
        await b.threads[t["id"]].task
    asyncio.run(main())
    assert [c["model"] for c in fake.calls] == ["deepseek-v4-pro", "deepseek-flash"]


def test_explore_subagent_uses_explore_role(ee_home, workspace):
    from test_subagent_parallel import RouterClient, report, session, task_call
    cli = RouterClient({
        "parent": [task_call(0, "t1", "查A") + task_call(1, "t2", "改B", agent="general") + finish("tool_calls"),
                   report("汇总")],
        "查A": [report("A 在这")],
        "改B": [report("B 改好了")],
    })
    s = session(workspace, cli)
    s.config.data["roles"]["explore"] = "deepseek-pro"
    run(collect(s, "开工"))
    models = {k: p["model"] for k, p in cli.calls}
    assert models["查A"] == "deepseek-v4-pro" and models["改B"] == "deepseek-flash" and models["parent"] == "deepseek-flash"
    bm = s.stats.totals()["by_model"]
    assert bm["deepseek-pro"]["requests"] == 1 and bm["deepseek-flash"]["requests"] == 3


def test_models_list_reports_effort_support_and_roles(ee_home, workspace):
    from ee.server.app import Backend
    (ee_home / "config.toml").write_text('[roles]\ntitle = "deepseek-flash"\n', encoding="utf-8")
    r = asyncio.run(Backend(home=ee_home).call("models/list", {"cwd": str(workspace)}))
    m = {x["name"]: x for x in r["models"]}
    es = m["deepseek-flash"]["effort_support"]
    assert (es["effort"], es["off"], es["levels"]) == (True, True, ["low", "max"])
    assert m["deepseek-flash"]["effort_values"]["max"] == "max"
    assert r["roles"]["title"] == "deepseek-flash" and r["roles"]["plan"] == ""


def test_thread_options_rpc_persists_effort_and_style(ee_home, workspace):
    fake = FakeClient([text_chunks("好") + finish("stop")])
    from ee.server.app import Backend

    async def main():
        b = Backend(home=ee_home, client_factory=lambda sp: ChatClient(sp, client=fake))
        t = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]
        assert t["effort"] == "" and t["style"] == "default" and t["effort_support"]["effort"] is True
        r = await b.call("thread/options", {"thread_id": t["id"], "effort": "low", "style": "concise"})
        assert r["effort"] == "low" and r["style"] == "concise"
        await b.call("turn/start", {"thread_id": t["id"], "text": "hi", "effort": "off"})
        await b.threads[t["id"]].task
        # 重启后照旧
        b2 = Backend(home=ee_home, client_factory=lambda sp: ChatClient(sp, client=fake))
        d = await b2.call("thread/get", {"thread_id": t["id"]})
        return d
    d = asyncio.run(main())
    assert fake.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert d["thread"]["effort"] == "off" and d["thread"]["style"] == "concise"
    with pytest.raises(Exception):
        asyncio.run(_bad_option(ee_home, workspace))


async def _bad_option(home, workspace):
    from ee.server.app import Backend
    b = Backend(home=home, client_factory=lambda sp: ChatClient(sp, client=FakeClient([])))
    t = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]
    await b.call("thread/options", {"thread_id": t["id"], "style": "不存在"})


def test_ai_title_uses_title_role(ee_home, workspace):
    (ee_home / "config.toml").write_text('[title]\nai = true\n[roles]\ntitle = "deepseek-pro"\n', encoding="utf-8")
    fake = FakeClient([text_chunks("好的，我来看") + finish("stop"), text_chunks("“修复登录报错”。") + finish("stop")])
    from ee.server.app import Backend

    async def main():
        b = Backend(home=ee_home, client_factory=lambda sp: ChatClient(sp, client=fake))
        t = (await b.call("thread/start", {"cwd": str(workspace)}))["thread"]
        await b.call("turn/start", {"thread_id": t["id"], "text": "登录的时候报 500，帮我看看怎么回事"})
        th = b.threads[t["id"]]
        await th.task
        await th._title_task
        return th
    th = asyncio.run(main())
    assert th.meta["title"] == "修复登录报错"
    assert fake.calls[1]["model"] == "deepseek-v4-pro" and "tools" not in fake.calls[1]
    assert th.usage["by_model"]["deepseek-pro"]["requests"] == 1
