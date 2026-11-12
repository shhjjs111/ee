"""同一回合内看图：工具登记 pending_images 后，下一步请求就要带上图片。

原来的实现只在 `_add_user`（回合开始 / 下一条用户消息）里注入图片，所以 `read(as_image=true)`
在本回合里拿到的是「下一轮可见」——对「改完 UI 自己看一眼」这种用法等于不可用：模型得先把
回合交还给用户才看得见。这里锁住新行为：工具返回之后、下一步采样之前补一条 meta 用户消息。
"""
from __future__ import annotations

import base64
from pathlib import Path

from conftest import FakeClient, collect, finish, make_session, run, text_chunks, tool_chunks

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6260000000000300010000050000"
    "000000000049454e44ae426082"
)


def _image_blocks(params: dict) -> list[list[dict]]:
    out = []
    for m in params.get("messages") or []:
        c = m.get("content")
        if isinstance(c, list) and any(b.get("type") == "image_url" for b in c if isinstance(b, dict)):
            out.append(c)
    return out


def test_image_visible_in_the_same_turn(workspace):
    png = workspace / "shot.png"
    png.write_bytes(PNG)
    script = [
        tool_chunks(0, "c1", "read", {"path": str(png), "as_image": True}) + finish("tool_calls"),
        text_chunks("看完了") + finish("stop"),
    ]
    s, fake = make_session(workspace, script)

    run(collect(s, "读一下这张图"))

    assert len(fake.calls) == 2, "应该有两步：工具调用 + 收尾"
    blocks = _image_blocks(fake.calls[1])
    assert len(blocks) == 1, f"第二步请求里没有图片块：{[m.get('role') for m in fake.calls[1]['messages']][-3:]}"
    url = next(b["image_url"]["url"] for b in blocks[0] if b.get("type") == "image_url")
    assert url.startswith("data:image/png;base64,") and base64.b64decode(url.split(",", 1)[1]) == PNG
    assert s.state.pending_images == [], "注入后要清空队列"
    # 图片消息不能被当成真实用户发言（压缩时只保留原话，靠 _kind 区分）
    meta = [m for m in s.history if m.get("_images")]
    assert meta and meta[-1].get("_kind") == "note" and meta[-1].get("role") == "user"


def test_no_image_block_when_model_has_no_vision(workspace):
    png = workspace / "shot.png"
    png.write_bytes(PNG)
    script = [
        tool_chunks(0, "c1", "read", {"path": str(png), "as_image": True}) + finish("tool_calls"),
        text_chunks("好的") + finish("stop"),
    ]
    s, fake = make_session(workspace, script, model="deepseek-pro")
    assert s.profile.vision is False

    events = run(collect(s, "读一下这张图"))

    assert _image_blocks(fake.calls[1]) == [], "不支持看图的模型不该收到 image 块"
    assert any("不支持看图" in (getattr(e, "text", "") or "") for e in events)


def test_source_image_can_disappear_without_changing_history_bytes(workspace, ee_home):
    """图片进入历史后源文件被删：消息字节不能变，否则从那里起整段前缀失效。"""
    from ee.config import load_config
    from ee.core.session import Session

    png = workspace / "shot.png"
    png.write_bytes(PNG)
    script = [
        tool_chunks(0, "c1", "read", {"path": str(png), "as_image": True}) + finish("tool_calls", prompt=1_000),
        text_chunks("看完了") + finish("stop", prompt=1_200, hit=1_000),
        text_chunks("继续") + finish("stop", prompt=1_300, hit=1_100),
    ]
    fake = FakeClient(script)
    s = Session(load_config(workspace), cwd=workspace, client=fake, persist=False, home=ee_home)

    run(collect(s, "读一下这张图"))
    meta = [m for m in s.history if m.get("_images")][-1]
    snap = Path(meta["_images"][0])
    assert snap.exists() and snap.read_bytes() == PNG
    assert str(snap).startswith(str(ee_home)), "外部图片没有快照到会话目录"

    png.unlink()                                  # 源文件消失
    run(collect(s, "继续"))
    blocks = _image_blocks(fake.calls[-1])
    url = next(b["image_url"]["url"] for b in blocks[0] if b.get("type") == "image_url")
    assert base64.b64decode(url.split(",", 1)[1]) == PNG, "源文件删除改变了历史中的图片字节"


def test_missing_image_is_attributed(workspace):
    """历史里的图片缺失时，下一次真实请求要记 attachment_missing，而不是落成 unknown。"""
    s, _ = make_session(workspace, [text_chunks("ok") + finish("stop")])
    missing = workspace / "deleted.png"
    s.history.append({"role": "user", "content": "看这张图", "_images": [str(missing)]})

    run(collect(s, "继续"))
    rec = s.stats.records[-1]
    assert rec.get("cause") == "attachment_missing"


def test_snapshot_respects_session_budget(workspace, ee_home, monkeypatch):
    """会话附件预算满了就不复制，避免长会话把磁盘塞满。"""
    from ee.core import attachments as A
    from ee.config import load_config
    from ee.core.session import Session

    png = workspace / "shot.png"
    png.write_bytes(PNG)
    monkeypatch.setattr(A, "MAX_SESSION_ATTACHMENT_BYTES", 1)
    s = Session(load_config(workspace), cwd=workspace, persist=False, home=ee_home)
    assert s.snapshot_image(str(png)) == str(png)


def test_two_images_share_one_meta_message(workspace):
    """同一段里登记了两张图：合成一条消息，不拆成一堆用户发言。"""
    a = workspace / "a.png"
    b = workspace / "b.png"
    a.write_bytes(PNG)
    b.write_bytes(PNG)
    script = [
        tool_chunks(0, "c1", "read", {"path": str(a), "as_image": True})
        + tool_chunks(1, "c2", "read", {"path": str(b), "as_image": True}) + finish("tool_calls"),
        text_chunks("都看完了") + finish("stop"),
    ]
    s, fake = make_session(workspace, script)

    run(collect(s, "读这两张"))

    blocks = _image_blocks(fake.calls[1])
    assert len(blocks) == 1 and len([x for x in blocks[0] if x.get("type") == "image_url"]) == 2
    assert len([m for m in s.history if m.get("_images")]) == 1
