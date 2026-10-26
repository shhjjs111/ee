"""文件面板：按目录懒加载的文件树（尊重 .gitignore）+ 文件读取（大小上限、二进制检测、路径不许逃出工作区）。"""
from __future__ import annotations

import base64
import os
from pathlib import Path

from ee.core import gitinfo
from ee.proc import run_capture
from ee.tools.search import SKIP_DIRS

MAX_ENTRIES = 2000           # 一个目录最多列这么多项
MAX_TEXT = 512 * 1024        # 文本最多返回 512KB（超出截断）
MAX_IMAGE = 8 * 1024 * 1024  # 图片最多 8MB
SNIFF = 8192

IMAGE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".bmp": "image/bmp", ".ico": "image/x-icon", ".svg": "image/svg+xml",
    ".avif": "image/avif",
}
# 一眼就知道是二进制的扩展名，省得读文件
BINARY_EXT = {".exe", ".dll", ".so", ".dylib", ".bin", ".obj", ".o", ".a", ".lib", ".pyc", ".pyd", ".class",
              ".jar", ".zip", ".gz", ".tgz", ".7z", ".rar", ".xz", ".bz2", ".pdf", ".woff", ".woff2", ".ttf",
              ".otf", ".eot", ".mp3", ".mp4", ".mov", ".avi", ".mkv", ".wav", ".flac", ".ogg", ".webm",
              ".sqlite", ".db", ".pdb", ".node", ".wasm", ".psd", ".docx", ".xlsx", ".pptx", ".msi"}


class FilesError(ValueError):
    pass


def _root(cwd: str) -> Path:
    p = Path(cwd).expanduser()
    if not p.is_dir():
        raise FilesError(f"目录不存在：{cwd}")
    return p.resolve()


def resolve_inside(root: Path, rel: str) -> Path:
    """把相对（或绝对）路径解析成工作区内的真实路径；符号链接解析后也必须在工作区内。"""
    rel = (rel or "").strip()
    raw = Path(rel).expanduser()
    target = (raw if raw.is_absolute() else root / raw).resolve()
    if target != root and root not in target.parents:
        raise FilesError(f"路径不在工作区内：{rel}")
    return target


def _rel(root: Path, p: Path) -> str:
    r = p.relative_to(root).as_posix()
    return "" if r == "." else r


def _git_ignored(root: Path, rels: list[str]) -> set[str] | None:
    """返回被 .gitignore 忽略的那些相对路径（目录带 / 结尾传入）；不是 git 仓库或没有 git 时返回 None。"""
    exe = gitinfo.git_exe()
    if not exe or not rels:
        return None if not exe else set()
    code, out, _ = run_capture([exe, "check-ignore", "-z", "--stdin"], cwd=root,
                               input=("\0".join(rels) + "\0").encode("utf-8"), timeout=10)
    if code not in (0, 1):   # 128：不是仓库；124：超时；127：没有 git
        return None
    return {s for s in out.decode("utf-8", "replace").split("\0") if s}


def tree(cwd: str, path: str = "", show_ignored: bool = False) -> dict:
    root = _root(cwd)
    d = resolve_inside(root, path)
    if not d.is_dir():
        raise FilesError(f"不是目录：{path}")
    entries: list[dict] = []
    try:
        it = list(os.scandir(d))
    except OSError as e:
        raise FilesError(f"无法读取目录：{e}")
    for e in it:
        name = e.name
        if name == ".git":
            continue
        try:
            is_dir = e.is_dir()
        except OSError:
            continue
        rel = _rel(root, Path(e.path))
        ent = {"name": name, "path": rel, "type": "dir" if is_dir else "file"}
        if not is_dir:
            try:
                ent["size"] = e.stat().st_size
            except OSError:
                ent["size"] = None
        entries.append(ent)
    ignored = _git_ignored(root, [x["path"] + ("/" if x["type"] == "dir" else "") for x in entries])
    git = ignored is not None
    out = []
    for x in entries:
        key = x["path"] + ("/" if x["type"] == "dir" else "")
        if git:
            ig = key in ignored or x["path"] in ignored
        else:
            ig = x["type"] == "dir" and x["name"] in SKIP_DIRS
        if ig and not show_ignored:
            continue
        if ig:
            x["ignored"] = True
        out.append(x)
    out.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
    truncated = len(out) > MAX_ENTRIES
    return {"root": str(root), "path": _rel(root, d), "entries": out[:MAX_ENTRIES], "truncated": truncated,
            "gitignore": git}


def _looks_binary(chunk: bytes) -> bool:
    if b"\0" in chunk:
        return True
    if not chunk:
        return False
    try:
        chunk.decode("utf-8")
        return False
    except UnicodeDecodeError as e:
        # 截在多字节字符中间不算
        if e.start >= len(chunk) - 3:
            return False
    ctrl = sum(1 for b in chunk if b < 9 or 13 < b < 32)
    return ctrl / len(chunk) > 0.1


def _decode(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace"), "utf-16"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError as e:
        if e.start >= len(data) - 3:   # 截断造成的半个字符
            return data[: e.start].decode("utf-8"), "utf-8"
    try:
        return data.decode("gbk"), "gbk"   # Windows 中文环境的老文件
    except UnicodeDecodeError:
        return data.decode("utf-8", "replace"), "utf-8"


def read(cwd: str, path: str, max_bytes: int = MAX_TEXT) -> dict:
    root = _root(cwd)
    f = resolve_inside(root, path)
    if not f.exists():
        raise FilesError(f"文件不存在：{path}")
    if f.is_dir():
        raise FilesError(f"是目录不是文件：{path}")
    size = f.stat().st_size
    rel = _rel(root, f)
    base = {"path": rel, "abs": str(f), "name": f.name, "size": size}
    ext = f.suffix.lower()
    if ext in IMAGE_MIME:
        if size > MAX_IMAGE:
            return {**base, "kind": "binary", "reason": "too_large"}
        data = f.read_bytes()
        return {**base, "kind": "image", "mime": IMAGE_MIME[ext],
                "data_url": f"data:{IMAGE_MIME[ext]};base64,{base64.b64encode(data).decode('ascii')}"}
    from ee.core.docread import DOC_EXTS, DocError, extract
    if ext in DOC_EXTS:               # PDF / Word / Excel / PPT 转文字预览（D4-7）
        try:
            text, info = extract(f)
        except DocError as e:
            return {**base, "kind": "binary", "reason": "doc_error", "message": str(e)}
        truncated = len(text) > MAX_TEXT * 2
        text = text[: MAX_TEXT * 2]
        return {**base, "kind": "text", "text": text, "encoding": "utf-8", "truncated": truncated,
                "doc": info.get("kind"), "lines": text.count("\n") + 1}
    if ext in BINARY_EXT:
        return {**base, "kind": "binary", "reason": "binary"}
    max_bytes = max(1024, min(int(max_bytes or MAX_TEXT), 4 * MAX_TEXT))
    with f.open("rb") as fh:
        data = fh.read(max_bytes + 1)
    truncated = len(data) > max_bytes
    data = data[:max_bytes]
    utf16 = data.startswith((b"\xff\xfe", b"\xfe\xff"))
    if not utf16 and _looks_binary(data[:SNIFF]):
        return {**base, "kind": "binary", "reason": "binary"}
    text, enc = _decode(data)
    text = text.replace("\r\n", "\n")
    return {**base, "kind": "text", "text": text, "encoding": enc, "truncated": truncated,
            "lines": text.count("\n") + (0 if text.endswith("\n") or not text else 1)}
