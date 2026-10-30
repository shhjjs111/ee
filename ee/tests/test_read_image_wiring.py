"""端到端：read(as_image=true) 登记图片 → 下一轮 _add_user 注入 _images。

不 mock 中间层：用真实 Session + 真实 ToolState，验证 pending_images 从工具
登记到消息组装（wire_messages 的 _images 挂载点）的完整链路。
"""
from __future__ import annotations

from pathlib import Path


def test_pending_images_injected_into_next_user_message(tmp_path, monkeypatch):
    from ee.core.session import Session
    from ee.config import load_config

    cfg = load_config(tmp_path)
    s = Session(cfg, cwd=tmp_path, mode="default", persist=False)
    # 关闭真实 client，避免 _add_user 触发网络
    s._client = None

    png = tmp_path / "shot.png"
    png.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c6260000000000300010000050000"
        "000000000049454e44ae426082"
    ))

    # 模拟上一轮工具调用：read(as_image=true) 登记图片
    s.state.pending_images.append(str(png))

    # 下一轮用户消息
    s._add_user("看看这张图")

    msg = s.history[-1]
    assert msg.get("_images") == [str(png)], f"待看图片没注入：{msg.get('_images')}"
    assert s.state.pending_images == [], "注入后应清空 pending_images"


def test_pending_images_merged_with_user_images(tmp_path):
    """pending_images 与用户本轮直接发的图片合并，去重。"""
    from ee.core.session import Session
    from ee.config import load_config

    cfg = load_config(tmp_path)
    s = Session(cfg, cwd=tmp_path, mode="default", persist=False)
    s._client = None

    png = tmp_path / "a.png"
    png.write_bytes(b"x")
    s.state.pending_images.append(str(png))

    s._add_user("看图", images=[str(png)])  # 用户本轮也带了同一张

    msg = s.history[-1]
    assert msg.get("_images") == [str(png)], "应去重，不重复注入"
    assert s.state.pending_images == []
