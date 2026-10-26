"""图片附件（D3-D）。

- 界面把图片在本地用 canvas 压缩（长边 ≤ 2048，JPEG/PNG）后以 base64 随 turn/start 发来，
  后端存到 ~/.ee/sessions/<会话id>/attachments/<随机名>.<扩展名>。
- 历史里的用户消息带 `_images: [绝对路径, ...]`（以 _ 开头的键不会原样发给模型）；
  wire_messages 发送时按档案 vision 决定：
    vision=true  → content 变成 [{"type":"text"}, {"type":"image_url","image_url":{"url":"data:…"}}...]
    vision=false → 在文字后加一行说明（模型看不到图），界面另出提示条目。
  同一条消息每次产出的字节一样（文件不变），前缀缓存不受影响。
- 格式按文件头识别（JPEG/PNG/GIF/WebP，与 DeepSeek 文档一致），不看扩展名和声明的 MIME。
"""
from __future__ import annotations

import base64
import binascii
import uuid
from pathlib import Path

MAX_IMAGES = 8
MAX_BYTES = 20 * 2**20          # 单张解码后上限（DeepSeek 内联上限 32 MiB，留余量）
MAX_THUMB = 64 * 1024           # 界面缩略图 data URL 上限


class AttachmentError(ValueError):
    pass


def sniff(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


EXT = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}


def attachments_dir(home: Path, thread_id: str) -> Path:
    return home / "sessions" / thread_id / "attachments"


def _decode(b64: str) -> bytes:
    s = (b64 or "").strip()
    if s.startswith("data:"):
        s = s.split(",", 1)[1] if "," in s else ""
    try:
        return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        raise AttachmentError("图片数据不是有效的 base64") from None


def save_all(home: Path, thread_id: str, items: list[dict]) -> tuple[list[str], list[dict]]:
    """保存附件。items: [{name, data(base64 或 data URL), thumb?}]。返回 (路径列表, 界面元数据列表)。"""
    if not items:
        return [], []
    if len(items) > MAX_IMAGES:
        raise AttachmentError(f"一条消息最多 {MAX_IMAGES} 张图片")
    d = attachments_dir(home, thread_id)
    decoded = []
    for it in items:
        if not isinstance(it, dict):
            raise AttachmentError("附件格式不对")
        data = _decode(str(it.get("data") or ""))
        name = str(it.get("name") or "image")[:120]
        if not data:
            raise AttachmentError(f"图片是空的：{name}")
        if len(data) > MAX_BYTES:
            raise AttachmentError(f"图片太大（{len(data) // 2**20} MiB）：{name}，上限 {MAX_BYTES // 2**20} MiB")
        mime = sniff(data)
        if mime is None:
            raise AttachmentError(f"不支持的图片格式：{name}（只支持 JPEG、PNG、GIF、WebP）")
        thumb = str(it.get("thumb") or "")
        if len(thumb) > MAX_THUMB or (thumb and not thumb.startswith("data:image/")):
            thumb = ""
        decoded.append((name, data, mime, thumb))
    d.mkdir(parents=True, exist_ok=True)
    paths, meta = [], []
    for name, data, mime, thumb in decoded:
        aid = uuid.uuid4().hex[:12]
        p = d / f"{aid}.{EXT[mime]}"
        p.write_bytes(data)
        paths.append(str(p))
        meta.append({"id": aid, "name": name, "mime": mime, "size": len(data), "thumb": thumb})
    return paths, meta


def data_url(path: str) -> str | None:
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    mime = sniff(data) or "image/png"
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def no_vision_note(n: int) -> str:
    return f"[用户附了 {n} 张图片，但当前模型不支持看图，你看不到图片内容；需要时请让用户换支持图像的模型或用文字描述]"


def wire_content(text: str, images: list[str], vision: bool):
    """用户消息 content：vision 时为分块数组，否则为文字。"""
    if not images:
        return text
    if not vision:
        return text + "\n\n" + no_vision_note(len(images))
    parts: list[dict] = [{"type": "text", "text": text}]
    missing = 0
    for p in images:
        url = data_url(p)
        if url is None:
            missing += 1
            continue
        parts.append({"type": "image_url", "image_url": {"url": url}})
    if missing:
        parts[0]["text"] = text + f"\n\n[有 {missing} 张图片文件已被删除，无法发送]"
    return parts


def rebase(images: list[str], old_dir: Path, new_dir: Path) -> list[str]:
    """分叉时把路径从原会话附件目录换到新会话目录。"""
    out = []
    for p in images:
        pp = Path(p)
        try:
            rel = pp.relative_to(old_dir)
            out.append(str(new_dir / rel))
        except ValueError:
            out.append(p)
    return out
