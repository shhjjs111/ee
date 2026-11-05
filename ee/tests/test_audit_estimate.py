"""token 估算的口径（2026-11 审计修复）。

message_text 只认内部键 `reasoning`，而实际发给模型的消息里推理叫 `reasoning_content`：
按 wire 形态估算时（note_wire 的断点归因）这块完全漏计。反过来，content 是数组时
（带图片的 user 消息）整段 json.dumps 会把 base64 当文本算，一张截图就能把估算推到
几十万 token —— 两个方向的偏差都会让「这次请求为什么没命中」的诊断数字不可用。
"""
from ee.core import compaction as C


def test_wire_style_reasoning_is_counted():
    """发给模型的消息里推理叫 reasoning_content，不能漏计。"""
    m = {"role": "assistant", "content": "好的",
         "reasoning_content": "先想清楚再回答" * 200}
    assert C.estimate_messages([m]) > 500, "reasoning_content 完全没计入"


def test_internal_style_reasoning_is_still_counted():
    """内部历史用 reasoning 键，这条不能被顺手改坏。"""
    m = {"role": "assistant", "content": "好的", "reasoning": "先想清楚再回答" * 200}
    assert C.estimate_messages([m]) > 500


def test_reasoning_is_not_double_counted():
    """两个键同时存在（wire 化之后）时只算一份。"""
    body = "先想清楚再回答" * 200
    one = {"role": "assistant", "content": "好的", "reasoning": body}
    both = {"role": "assistant", "content": "好的", "reasoning": body, "reasoning_content": body}
    assert C.estimate_messages([both]) < C.estimate_messages([one]) * 2


def test_base64_image_is_not_counted_as_text():
    """图片按张数计价，不能把 base64 当正文。"""
    big = "A" * 200_000                      # 约等于一张截图的 base64
    m = {"role": "user", "content": [
        {"type": "text", "text": "看这张图"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{big}"}},
    ]}
    got = C.estimate_messages([m])
    assert got < 5_000, f"base64 被当文本算了：{got}"


def test_image_still_contributes_some_tokens():
    """但图片本身要算：一张图至少按千 token 量级计入。"""
    m = {"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]}
    assert C.estimate_messages([m]) >= 1_000


def test_plain_text_is_unaffected():
    t = "hello world\n第二行"
    assert C.message_text({"role": "user", "content": t}) == t
    assert C.estimate_tokens(t) > 0
