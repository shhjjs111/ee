"""D3-D：图片附件。"""
import asyncio
import base64

import pytest
from conftest import finish, text_chunks
from test_server import make_backend, wait_for

from ee.core import attachments as A
from ee.core.providers import transforms as T
from ee.core.providers.profiles import profile_for
from ee.server.app import RpcError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


# Word 导出的 XPS / docx：文件头是 ZIP（PK），只是名字常被写成 .png。
XPS = b"PK\x03\x04\x14\x00\x06\x00" + b"\x00" * 32


def b64(b):
    return base64.b64encode(b).decode()


def test_sniff_and_save(ee_home):
    assert A.sniff(PNG) == "image/png" and A.sniff(JPG) == "image/jpeg"
    assert A.sniff(b"RIFF\x00\x00\x00\x00WEBPxx") == "image/webp" and A.sniff(b"hello") is None
    paths, meta = A.save_all(ee_home, "t1", [{"name": "a.png", "data": "data:image/png;base64," + b64(PNG),
                                              "thumb": "data:image/jpeg;base64,xx"}])
    assert paths[0].endswith(".png") and meta[0]["mime"] == "image/png" and meta[0]["thumb"].startswith("data:")
    with pytest.raises(A.AttachmentError, match="格式"):
        A.save_all(ee_home, "t1", [{"name": "x.txt", "data": b64(b"hello world")}])
    with pytest.raises(A.AttachmentError, match="base64"):
        A.save_all(ee_home, "t1", [{"name": "x", "data": "!!!"}])
    with pytest.raises(A.AttachmentError, match="最多"):
        A.save_all(ee_home, "t1", [{"name": "x", "data": b64(PNG)}] * 9)


def test_wire_vision_and_fallback(tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(PNG)
    hist = [{"role": "user", "content": "看图", "_images": [str(p)]}]
    flash = profile_for("deepseek-flash")
    pro = profile_for("deepseek-v4-pro")
    assert flash.vision is True and pro.vision is False
    w = T.wire_messages(hist, flash)[0]
    assert w["content"][0] == {"type": "text", "text": "看图"}
    assert w["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "_images" not in w and hist[0]["content"] == "看图"          # 历史不被改
    assert T.wire_messages(hist, flash) == T.wire_messages(hist, flash)  # 字节稳定
    w2 = T.wire_messages(hist, pro)[0]
    assert isinstance(w2["content"], str) and "不支持看图" in w2["content"]
    p.unlink()
    w3 = T.wire_messages(hist, flash)[0]
    assert len(w3["content"]) == 1 and "已被删除" in w3["content"][0]["text"]


def test_misnamed_file_never_wired_as_image(tmp_path):
    """扩展名撒谎（.png 实为 ZIP/XPS）时，绝不能当成 png 发出去。

    真实故障（2026-11-06）：read(as_image=true) 读了 E:\\毕业设计\\_page1.png，
    它其实是 XPS，data_url 认不出格式就按 image/png 硬编码，上游回 400
    「You have uploaded an unsupported image」；这条坏图还进了历史，每轮都重发，
    会话从此永久卡死。所以：认不出格式就必须不发。
    """
    p = tmp_path / "_page1.png"
    p.write_bytes(XPS)
    assert A.sniff(p.read_bytes()) is None
    assert A.data_url(str(p)) is None, "认不出格式时不该产出 data URL"
    w = T.wire_messages([{"role": "user", "content": "看图", "_images": [str(p)]}],
                        profile_for("deepseek-flash"))[0]
    assert all(part["type"] != "image_url" for part in w["content"]), "坏图不能进请求"
    assert len(w["content"]) == 1 and "不是可用的图片" in w["content"][0]["text"]
    assert T.wire_messages([{"role": "user", "content": "看图", "_images": [str(p)]}],
                           profile_for("deepseek-flash"))[0] == w, "失败提示也要字节稳定（前缀缓存）"


def test_turn_with_image_end_to_end(ee_home, workspace):
    script = [text_chunks("图里是一个 PNG") + finish("stop"), text_chunks("好") + finish("stop")]

    async def go():
        b, fake, ev = make_backend(ee_home, script)
        tid = (await b.call("thread/start", {"cwd": str(workspace), "model": "deepseek-flash"}))["thread"]["id"]
        await b.call("turn/start", {"thread_id": tid, "text": "这是什么",
                                    "attachments": [{"name": "s.png", "data": b64(PNG)}]})
        await wait_for(ev, "turn/completed")
        user_msg = [m for m in fake.calls[0]["messages"] if m["role"] == "user"][-1]
        assert isinstance(user_msg["content"], list) and user_msg["content"][1]["type"] == "image_url"
        d = (await b.call("thread/get", {"thread_id": tid}))["thread"]
        u = next(it for it in d["items"] if it["type"] == "user")
        assert u["attachments"][0]["name"] == "s.png"
        assert (ee_home / "sessions" / tid / "attachments").is_dir()
        # 只有图片没有文字也能发
        ev.clear()
        await b.call("turn/start", {"thread_id": tid, "text": "", "attachments": [{"name": "j.jpg", "data": b64(JPG)}]})
        await wait_for(ev, "turn/completed")
        with pytest.raises(RpcError, match="格式"):
            await b.call("turn/start", {"thread_id": tid, "text": "x", "attachments": [{"name": "t", "data": b64(b"nope")}]})
        # 分叉：图片路径换到新会话目录，原会话删除后分叉仍能发图
        d = (await b.call("thread/get", {"thread_id": tid}))["thread"]
        u2 = [it for it in d["items"] if it["type"] == "user"][1]
        nt = (await b.call("thread/fork", {"thread_id": tid, "item_id": u2["id"]}))["thread"]
        await b.call("thread/delete", {"thread_id": tid})
        assert not (ee_home / "sessions" / tid).exists()
        hist = b.threads[nt["id"]].session.history
        img = [m for m in hist if m.get("_images")][0]["_images"][0]
        assert nt["id"] in img and (ee_home / "sessions" / nt["id"] / "attachments").is_dir()
        import os
        assert os.path.exists(img)
    asyncio.run(go())


def test_no_vision_model_notice(ee_home, workspace):
    async def go():
        b, fake, ev = make_backend(ee_home, [text_chunks("看不到") + finish("stop")])
        tid = (await b.call("thread/start", {"cwd": str(workspace), "model": "deepseek-pro"}))["thread"]["id"]
        await b.call("turn/start", {"thread_id": tid, "text": "看图", "attachments": [{"name": "s.png", "data": b64(PNG)}]})
        await wait_for(ev, "turn/completed")
        user_msg = [m for m in fake.calls[0]["messages"] if m["role"] == "user"][-1]
        assert isinstance(user_msg["content"], str) and "不支持看图" in user_msg["content"]
        assert any(m == "item/started" and p["item"]["type"] == "notice" and "不支持看图" in p["item"]["text"] for m, p in ev)
    asyncio.run(go())


def test_models_list_and_settings_vision(ee_home):
    async def go():
        b, _, _ = make_backend(ee_home, [])
        ms = {m["name"]: m for m in (await b.call("models/list", {}))["models"]}
        assert ms["deepseek-flash"]["vision"] is True and ms["deepseek-pro"]["vision"] is False
        st = (await b.call("settings/get", {}))["settings"]
        assert st["models"]["deepseek-flash"]["default_vision"] is True and st["models"]["deepseek-flash"]["vision"] is None
        st["models"]["deepseek-pro"]["vision"] = True
        st2 = (await b.call("settings/set", {"settings": {"models": st["models"]}}))["settings"]
        assert st2["models"]["deepseek-pro"]["vision"] is True
        ms = {m["name"]: m for m in (await b.call("models/list", {}))["models"]}
        assert ms["deepseek-pro"]["vision"] is True
    asyncio.run(go())

def test_attachment_read_returns_original_image(ee_home, workspace):
    """界面点缩略图要能看到**原图**：缩略图只有 160px（前端 canvas 压缩），放大必糊。

    原图存在 `~/.ee/sessions/<会话id>/attachments/<id>.<ext>`，这里按 id 取回。
    id 与 thread_id 都会参与路径拼接，所以非法值必须是拒绝而不是"找不到"——
    否则 `../..` 这类输入能读到任意文件。
    """

    async def go():
        b, fake, ev = make_backend(ee_home, [text_chunks("好") + finish("stop")])
        tid = (await b.call("thread/start", {"cwd": str(workspace), "model": "deepseek-flash"}))["thread"]["id"]
        await b.call("turn/start", {"thread_id": tid, "text": "看图",
                                    "attachments": [{"name": "s.png", "data": b64(PNG)}]})
        await wait_for(ev, "turn/completed")

        d = (await b.call("thread/get", {"thread_id": tid}))["thread"]
        u = next(it for it in d["items"] if it["type"] == "user")
        aid = u["attachments"][0]["id"]

        r = await b.call("attachment/read", {"thread_id": tid, "attachment_id": aid})
        # 必须是磁盘上那份原图的完整字节，不是缩略图
        expect = "data:image/png;base64," + b64(PNG)
        assert r["data_url"] == expect
        assert r["mime"] == "image/png"   # 界面的文件名用 thread/get 里的 meta，接口不重复带

        # 路径穿越：id 不是 12 位十六进制就拒绝
        for bad in ("../..", "..\\..", "a" * 12 + "/../x", ""):
            with pytest.raises(RpcError):
                await b.call("attachment/read", {"thread_id": tid, "attachment_id": bad})
        # 12 位十六进制但不存在：是"已不在磁盘上"，不是穿越
        with pytest.raises(RpcError, match="不在"):
            await b.call("attachment/read", {"thread_id": tid, "attachment_id": "0123456789ab"})

    asyncio.run(go())
