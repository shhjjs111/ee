"""改文件引擎（edit / write / apply_patch 共用）：读写保留编码细节、容错匹配链、diff。

- 换行：读入时统一成 \\n 处理，写回时沿用原文件的换行风格（Windows 下 CRLF 文件不会被改成 LF）。
- BOM、编码：保留（GBK 老文件写回仍是 GBK；新内容 GBK 表示不了时才改存 UTF-8）。
- 容错链：精确 → 去行尾空白 → 符号归一（弯引号/破折号/特殊空格/全半角）→ 逐行 trim → 缩进无关
  → 首尾锚点 + 相似度 ≥0.65。
- 多处编辑（apply_edits）：每处都对照原文件定位，检查重叠，全部成功才返回新内容（调用方一次写入）。
- 找不到时给出最接近的窗口（行号 + 原文 + 第一处不一致）；不唯一时给出每处的行号。
"""
from __future__ import annotations

import asyncio
import difflib
import heapq
import json
import os
import re
import weakref
from dataclasses import dataclass
from pathlib import Path

from ee.tools.base import ToolError

BOM = "\ufeff"
MAX_TEXT_BYTES = 32 * 1024 * 1024     # 整体按文本读入（编辑、匹配）的文件大小上限


@dataclass
class TextFile:
    text: str        # 已统一成 \n
    eol: str         # "\n" | "\r\n"
    bom: bool
    existed: bool
    encoding: str = "utf-8"
    lossy: bool = False   # 既不是 UTF-8 也不是 GBK，按替换字符解码：局部编辑会损坏原字节


def read_text(path: Path) -> TextFile:
    if not path.exists():
        return TextFile("", "\n", False, False)
    if not path.is_file():   # FIFO / 设备文件：打开或读取会一直阻塞（或读不完）
        raise ToolError(f"{path} 不是普通文件（可能是管道或设备），不能按文本读写")
    size = path.stat().st_size
    if size > MAX_TEXT_BYTES:
        raise ToolError(f"{path} 太大（{size // (1024 * 1024)}MB，上限 {MAX_TEXT_BYTES // (1024 * 1024)}MB），"
                        "不能整体按文本处理；用 read 的 offset/limit 分段看，或用 shell 处理")
    raw = path.read_bytes()
    if b"\x00" in raw[:8192]:
        raise ToolError(f"{path} 看起来是二进制文件，不能按文本编辑")
    enc, lossy = "utf-8", False
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            s, enc = raw.decode("gbk"), "gbk"  # Windows 中文老文件
        except UnicodeDecodeError:
            s, lossy = raw.decode("utf-8", errors="replace"), True
    bom = s.startswith(BOM)
    if bom:
        s = s[1:]
    eol = "\r\n" if "\r\n" in s else "\n"
    return TextFile(s.replace("\r\n", "\n"), eol, bom, True, enc, lossy)


def _render(tf: TextFile, new_text: str) -> str:
    out = new_text.replace("\r\n", "\n")
    if tf.eol == "\r\n":
        out = out.replace("\n", "\r\n")
    if tf.bom:
        out = BOM + out
    return out


def unencodable(tf: TextFile, new_text: str) -> str | None:
    """新内容里原编码表示不了的第一个字符（UTF-8 总能表示，返回 None）。"""
    if tf.encoding == "utf-8":
        return None
    for ch in new_text:
        try:
            ch.encode(tf.encoding)
        except UnicodeEncodeError:
            return ch
    return None


def write_text(path: Path, tf: TextFile, new_text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = _render(tf, new_text)
    try:
        data = out.encode(tf.encoding)
    except UnicodeEncodeError:
        data = out.encode("utf-8")   # 原编码表示不了（如 GBK 里写 emoji）：改存 UTF-8，不丢字
    with open(path, "wb") as f:
        f.write(data)


# ------------------------------------------------------------------ 同一文件的写入排队

_LOCKS: "weakref.WeakValueDictionary[tuple[int, str], asyncio.Lock]" = weakref.WeakValueDictionary()


def path_lock(path: Path) -> asyncio.Lock:
    """按路径的写锁：读-算-写之间有 await 时，同一文件的并发修改（另一个对话、子代理）排队执行，不会互相覆盖。"""
    key = (id(asyncio.get_running_loop()), os.path.normcase(os.path.realpath(path)))
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock


def make_diff(old: str, new: str, name: str) -> tuple[str, int, int]:
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                      fromfile=f"a/{name}", tofile=f"b/{name}", lineterm="", n=3))
    add = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
    rem = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
    return "\n".join(lines), add, rem


# ------------------------------------------------------------------ 匹配链

def _line_spans(text: str) -> list[tuple[int, int]]:
    """每行 (起点, 终点不含\\n)。"""
    spans, pos = [], 0
    for line in text.split("\n"):
        spans.append((pos, pos + len(line)))
        pos += len(line) + 1
    return spans


def _block_matches(content: str, find: str, norm) -> list[tuple[int, int]]:
    """按行比较（每行经 norm 处理）找出所有匹配块的字符区间。"""
    clines = content.split("\n")
    flines = find.split("\n")
    if flines and flines[-1] == "":
        flines = flines[:-1]
    if not flines:
        return []
    spans = _line_spans(content)
    nf = [norm(l) for l in flines]
    out = []
    for i in range(len(clines) - len(flines) + 1):
        if all(norm(clines[i + j]) == nf[j] for j in range(len(flines))):
            out.append((spans[i][0], spans[i + len(flines) - 1][1]))
    return out


def _lead(line: str) -> str:
    return line[:len(line) - len(line.lstrip(" \t"))]


def _base_indent(lines: list[str]) -> str:
    """非空行里缩进最短的那行的前导空白。"""
    ind = [_lead(l) for l in lines if l.strip()]
    return min(ind, key=len) if ind else ""


def _indent_shift_matches(content: str, find: str) -> list[tuple[int, int]]:
    """整体缩进平移：每个非空行都是「原文基准缩进 R + 余下部分」对「old 基准缩进 O + 同样的余下部分」。
    只接受整块统一平移（相对缩进、缩进字符都和原文一致），这样才能把 new 按同一偏移对齐回原文。"""
    clines = content.split("\n")
    flines = find.split("\n")
    if flines and flines[-1] == "":
        flines = flines[:-1]
    n = len(flines)
    if not n or not any(l.strip() for l in flines):
        return []
    o = _base_indent(flines)
    if any(l.strip() and not l.startswith(o) for l in flines):
        return []
    frest = [l[len(o):].rstrip() if l.strip() else "" for l in flines]
    first = next(k for k, l in enumerate(frest) if l)
    spans = _line_spans(content)
    out = []
    for i in range(len(clines) - n + 1):
        if clines[i + first].strip() != frest[first].strip():
            continue
        win = clines[i:i + n]
        if any(bool(w.strip()) != bool(f) for w, f in zip(win, frest)):
            continue
        r = _base_indent(win)
        if all((not w.strip()) or (w.startswith(r) and w[len(r):].rstrip() == f) for w, f in zip(win, frest)):
            out.append((spans[i][0], spans[i + n - 1][1]))
    return out


def _reindent(text: str, o: str, r: str) -> str:
    """把 text 每个非空行的前导缩进从以 O 为基准改成以 R 为基准；做不到（缩进比 O 浅且字符不一致）就报错。"""
    if o == r:
        return text
    chars = set(o + r)
    out = []
    for line in text.split("\n"):
        if not line.strip():
            out.append(line)
            continue
        if line.startswith(o):
            out.append(r + line[len(o):])
            continue
        lead = _lead(line)
        if len(chars) == 1 and set(lead) <= chars:
            k = len(lead) + len(r) - len(o)
            if k >= 0:
                out.append(next(iter(chars)) * k + line[len(lead):])
                continue
        raise ToolError(f"new_string 的这一行缩进比 old_string 的基准缩进还浅，无法按原文缩进对齐：{_clip(line)}\n"
                        "请照原文缩进重新写 old_string/new_string。")
    return "\n".join(out)


def _merge_lines(region: str, old: str, new: str) -> str:
    """模糊命中后生成替换文本：new 里与 old 相同的行照抄原文；真正改动的行用 new，
    但若那一行的原文与 old 不完全一致（只差行尾空白除外），说明不知道模型想保留哪种写法，宁可报错。"""
    r_lines, o_lines, n_lines = region.split("\n"), old.split("\n"), new.split("\n")
    if len(r_lines) != len(o_lines):
        raise ToolError("old_string 与原文行数对不上，无法安全替换。请先 read 再逐字复制原文。")
    out: list[str] = []
    sm = difflib.SequenceMatcher(None, o_lines, n_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out += r_lines[i1:i2]
            continue
        if tag == "replace":
            for k in range(i1, i2):
                if r_lines[k].rstrip() != o_lines[k].rstrip():
                    raise ToolError("要改的这一行原文和 old_string 有符号差异（弯引号/全角符号/特殊空格等），"
                                    "无法确定改后该保留哪种写法，已拒绝，文件未改动。原文是：\n"
                                    f"{_clip(r_lines[k])}\n请把这一行照原文逐字复制到 old_string，new_string 也按原文写法改。")
        if tag in ("replace", "insert"):
            out += n_lines[j1:j2]
    return "\n".join(out)


# 符号归一（只用于定位，不写回）：弯引号 → 直引号，各种破折号/连字符 → -，特殊空格 → 空格，零宽字符去掉，
# 全角 ASCII（！到～）→ 半角。不做 NFKC：上标、连字、兼容汉字等折叠后语义不同。
_CHAR_MAP = {}
for _c in "\u2018\u2019\u201a\u201b\u2032":
    _CHAR_MAP[_c] = "'"
for _c in "\u201c\u201d\u201e\u201f\u2033":
    _CHAR_MAP[_c] = '"'
for _c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212":
    _CHAR_MAP[_c] = "-"
for _c in "\u00a0\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000":
    _CHAR_MAP[_c] = " "
for _c in "\u200b\u200c\u200d\u2060\ufeff":
    _CHAR_MAP[_c] = ""


def _norm_char(c: str) -> str:
    m = _CHAR_MAP.get(c)
    if m is not None:
        return m
    o = ord(c)
    if 0xFF01 <= o <= 0xFF5E:
        return chr(o - 0xFEE0)
    return c


def _norm_map(text: str) -> tuple[str, list[int]]:
    """归一后的文本 + 每个字符在原文里的下标（行尾空白去掉，便于和模型手写的文本比较）。"""
    chars: list[str] = []
    idx: list[int] = []
    for i, c in enumerate(text):
        for d in _norm_char(c):
            chars.append(d)
            idx.append(i)
    out_c: list[str] = []
    out_i: list[int] = []
    pend_c: list[str] = []
    pend_i: list[int] = []
    for c, i in zip(chars, idx):
        if c in " \t":
            pend_c.append(c)
            pend_i.append(i)
            continue
        if c != "\n":
            out_c += pend_c
            out_i += pend_i
        pend_c, pend_i = [], []
        out_c.append(c)
        out_i.append(i)
    return "".join(out_c), out_i


def _unicode_matches(content: str, find: str) -> list[tuple[int, int]]:
    if find.isascii() and content.isascii():
        return []   # 纯 ASCII 没有可归一的符号；行尾空白由 rstrip 策略负责
    nfind, _ = _norm_map(find)
    if not nfind.strip():
        return []
    ncontent, idx = _norm_map(content)
    if nfind == find and ncontent == content:
        return []
    return [(idx[a], idx[b - 1] + 1) for a, b in _exact(ncontent, nfind)]


# (名字, 函数, 是否按整行匹配, 给模型看的说法)
STRATEGIES = [
    ("exact", None, False, "精确"),
    ("rstrip", lambda c, f: _block_matches(c, f, str.rstrip), True, "去行尾空白"),
    ("unicode", _unicode_matches, False, "符号归一（引号/破折号/空格/全半角）"),
    ("indent", _indent_shift_matches, True, "整体缩进平移，已按原文缩进对齐"),
]
# 不再有逐行 trim / 首尾锚点相似度：前者会丢相对缩进，后者会用模型记错的 old_string 覆盖原文。
# 对不上就报错并给出最接近的原文，让模型照原文重试。
STRATEGY_LABEL = {name: label for name, _, _, label in STRATEGIES}


def _exact(content: str, find: str) -> list[tuple[int, int]]:
    out, start = [], 0
    while True:
        i = content.find(find, start)
        if i < 0:
            return out
        out.append((i, i + len(find)))
        start = i + max(1, len(find))


# ------------------------------------------------------------------ 多处编辑

@dataclass
class Edit:
    old: str
    new: str
    replace_all: bool = False


@dataclass
class EditHit:
    index: int          # 第几个 edit
    strategy: str
    spans: list[tuple[int, int]]
    new: str


_OLD_KEYS = ("old_string", "oldText", "old_text", "old_str", "old")
_NEW_KEYS = ("new_string", "newText", "new_text", "new_str", "new")


def _pick(d: dict, keys: tuple[str, ...]):
    for k in keys:
        if k in d:
            return d[k]
    return None


def _as_bool(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return bool(v)


def parse_edits(args: dict) -> list[Edit]:
    """参数容错：edits 可以是数组、JSON 字符串、单个对象；旧的顶层 old_string/new_string/replace_all 也认。"""
    raw = args.get("edits")
    if isinstance(raw, str):
        if raw.strip():
            try:
                raw = json.loads(raw)
            except ValueError:
                raise ToolError("edits 不是合法的 JSON 数组。") from None
        else:
            raw = None
    if isinstance(raw, dict):
        raw = [raw]
    items: list = list(raw) if isinstance(raw, list) else []
    if raw is not None and not isinstance(raw, list):
        raise ToolError("edits 应该是数组：[{old_string, new_string}]。")
    if _pick(args, _OLD_KEYS) is not None:
        items.append({"old_string": _pick(args, _OLD_KEYS), "new_string": _pick(args, _NEW_KEYS),
                      "replace_all": args.get("replace_all", False)})
    if not items:
        raise ToolError("缺少 edits（或 old_string/new_string）。")
    out = []
    for i, it in enumerate(items):
        if isinstance(it, str):
            try:
                it = json.loads(it)
            except ValueError:
                it = None
        if not isinstance(it, dict):
            raise ToolError(f"edits[{i}] 应该是对象 {{old_string, new_string}}。")
        old, new = _pick(it, _OLD_KEYS), _pick(it, _NEW_KEYS)
        if not isinstance(old, str):
            raise ToolError(f"edits[{i}] 缺少 old_string。")
        if not isinstance(new, str):
            raise ToolError(f"edits[{i}] 缺少 new_string（删除就传空字符串）。")
        e = Edit(old.replace("\r\n", "\n"), new.replace("\r\n", "\n"), _as_bool(it.get("replace_all", False)))
        if e not in out:      # 同一处给了两遍（edits 里一份、顶层 old_string 又一份）：只算一次，不报重叠
            out.append(e)
    return out


def _line_no(content: str, pos: int) -> int:
    return content.count("\n", 0, pos) + 1


def _numbered(lines: list[str], first: int) -> list[str]:
    w = len(str(first + len(lines) - 1))
    return [f"{first + k:>{w}}\t{_clip(l)}" for k, l in enumerate(lines)]


def _clip(s: str, n: int = 200) -> str:
    return s if len(s) <= n else s[:n] + "…"


MAX_SHOW = 15
_TOKEN = re.compile(r"[A-Za-z0-9_]+|\S")
_MIN_CLOSE = 0.4


def closest_window(content: str, old: str) -> tuple[int, int, float] | None:
    """在文件里找和 old 最像的连续行（行数相同），返回 (起始行下标, 行数, 相似度)。大文件先粗筛再细算。"""
    clines = content.split("\n")
    flines = old.split("\n")
    if flines and flines[-1] == "":
        flines = flines[:-1]
    if not flines or not content.strip():
        return None
    n = min(len(flines), len(clines))
    starts = range(len(clines) - n + 1)
    fstrip = [l.strip() for l in flines]
    target = "\n".join(fstrip)
    # 粗筛 1：窗口里和 old 某行（trim 后）完全相同的行数
    want: dict[str, int] = {}
    for l in fstrip:
        if len(l) >= 3:    # 跳过 "}"、")" 这类到处都有的短行
            want[l] = want.get(l, 0) + 1
    cs = [l.strip() for l in clines]
    hits = [1 if cs[i] in want else 0 for i in range(len(clines))]
    score, cur = [], sum(hits[:n])
    for s in starts:
        if s:
            cur += hits[s + n - 1] - hits[s - 1]
        score.append(cur)
    best1 = max(score) if score else 0
    if best1 > 0:
        cand = sorted((s for s in starts if score[s] >= best1 - 1), key=lambda s: -score[s])[:300]
    else:
        # 粗筛 2：每行都对不上时，按首行/末行和文件各行的词集合相似度（Jaccard）找起点
        toks = [frozenset(_TOKEN.findall(l)) for l in cs]

        def top(line: str, shift: int) -> list[int]:
            want_t = frozenset(_TOKEN.findall(line))
            if not want_t:
                return []
            sc = []
            for i, t in enumerate(toks):
                inter = len(want_t & t)
                if inter:
                    sc.append((inter / len(want_t | t), i))
            return [min(max(0, i - shift), len(clines) - n) for _, i in heapq.nlargest(40, sc)]
        cand = sorted(set(top(fstrip[0], 0) + top(fstrip[-1], n - 1)))
        if not cand and len(clines) <= 2000:
            cand = list(starts)
    best = None
    sm = difflib.SequenceMatcher(None, autojunk=False)
    sm.set_seq2(target)
    for s in cand:
        sm.set_seq1("\n".join(cs[s:s + n]))
        if best is not None and sm.quick_ratio() <= best[2]:
            continue
        r = sm.ratio()
        if best is None or r > best[2]:
            best = (s, n, r)
    if best is None or best[2] < _MIN_CLOSE:
        return None
    return best


def not_found_message(content: str, old: str) -> str:
    msg = "找不到 old_string（精确匹配和各种容错都没对上）。"
    win = closest_window(content, old)
    if win is None:
        return msg + "文件里也没有相近的内容，请重新 read 确认原文。"
    s, n, r = win
    clines = content.split("\n")
    flines = old.split("\n")
    if flines and flines[-1] == "":
        flines = flines[:-1]
    diff_at = next((k for k in range(n) if clines[s + k].rstrip() != flines[k].rstrip()), None)
    first = s
    if n > MAX_SHOW and diff_at is not None:
        first = s + max(0, min(diff_at - 3, n - MAX_SHOW))
    shown = clines[first:min(s + n, first + MAX_SHOW)]
    where = f"第 {s + 1} 行" if n == 1 else f"第 {s + 1}-{s + n} 行"
    msg += f"最接近的是{where}（相似度 {round(r * 100)}%）"
    if diff_at is not None:
        msg += (f"，从第 {s + diff_at + 1} 行开始不一致：\n你写的：{_clip(flines[diff_at])}\n文件里：{_clip(clines[s + diff_at])}")
    if n > 1:
        msg += "\n原文：\n" + "\n".join(_numbered(shown, first + 1))
        if first + len(shown) < s + n:
            msg += "\n…"
    return msg + "\n请照原文改好 old_string 再试。"


def _locate(content: str, e: Edit) -> tuple[str, list[tuple[int, int]], bool]:
    """按容错链定位一个 edit。返回 (策略, 区间, 是否整行匹配)；失败抛 ToolError（不带 edits[i] 前缀）。"""
    if e.old == "":
        raise ToolError("old_string 为空。新建文件或往空文件写内容请用 write。")
    if e.old == e.new:
        raise ToolError("old_string 与 new_string 相同，没有任何改动。")
    for name, fn, by_line, _label in STRATEGIES:
        hits = _exact(content, e.old) if fn is None else fn(content, e.old)
        if not hits:
            continue
        if len(hits) > 1 and not e.replace_all:
            lines = [_line_no(content, a) for a, _ in hits]
            shown = "、".join(str(x) for x in lines[:20]) + ("…" if len(lines) > 20 else "")
            raise ToolError(f"old_string 匹配到 {len(hits)} 处（第 {shown} 行）。请多带几行上下文让它唯一，"
                            "或者确实要全部替换时设 replace_all=true。")
        old_lines = e.old.rstrip("\n").count("\n") + 1
        for a, b in hits:
            got = content[a:b].count("\n") + 1
            if got > old_lines * 1.5 + 3:
                raise ToolError(f"模糊匹配到的块（第 {_line_no(content, a)} 行起 {got} 行）比 old_string（{old_lines} 行）"
                                "大太多，已拒绝。请先 read 确认原文再精确复制。")
        return name, hits, by_line
    raise ToolError(not_found_message(content, e.old))


def _render_piece(strategy: str, region: str, old: str, new: str) -> str:
    """一处命中的替换文本。精确命中直接用 new；模糊命中只改模型真正改动的行，其余照抄原文。"""
    if strategy == "exact":
        return new
    if strategy == "indent":
        o = _base_indent(old.split("\n"))
        r = _base_indent(region.split("\n"))
        old, new = _reindent(old, o, r), _reindent(new, o, r)
    return _merge_lines(region, old, new)


def apply_edits(content: str, edits: list[Edit]) -> tuple[str, list[EditHit]]:
    """所有 edit 都对照原文件定位；任一失败、互相重叠或改完没变化都抛 ToolError，不返回半成品。"""
    multi = len(edits) > 1

    def tag(i: int) -> str:
        return f"edits[{i}]：" if multi else ""

    results: list[EditHit] = []
    pieces: dict[tuple[int, int], str] = {}
    errors: list[str] = []
    for i, e in enumerate(edits):
        try:
            name, spans, by_line = _locate(content, e)
            fixed = []
            for a, b in spans:
                old, new = e.old, e.new
                if by_line and old.endswith("\n"):
                    if content[b:b + 1] == "\n":
                        b += 1          # 行块匹配不含末尾换行；old 带换行时把它也算进区间，删整行才干净
                    else:
                        old = old[:-1]
                        new = new[:-1] if new.endswith("\n") else new
                pieces[(a, b)] = _render_piece(name, content[a:b], old, new)
                fixed.append((a, b))
        except ToolError as err:
            errors.append(tag(i) + str(err))
            continue
        results.append(EditHit(i, name, fixed, e.new))
    if errors:
        tail = "\n整个调用没有写入任何改动；改好出错的几处后，把全部 edits 一起重发。" if multi else ""
        raise ToolError("\n\n".join(errors) + tail)
    flat = sorted(((a, b, h) for h in results for a, b in h.spans), key=lambda x: (x[0], x[1]))
    for (a1, b1, h1), (a2, b2, h2) in zip(flat, flat[1:]):
        if a2 < b1 or (a2 == a1 and b2 == b1):
            l1 = f"{_line_no(content, a1)}-{_line_no(content, max(a1, b1 - 1))}"
            l2 = f"{_line_no(content, a2)}-{_line_no(content, max(a2, b2 - 1))}"
            who = f"edits[{h1.index}]（第 {l1} 行）与 edits[{h2.index}]（第 {l2} 行）" if h1.index != h2.index else \
                f"edits[{h1.index}] 的两处匹配（第 {l1} 行、第 {l2} 行）"
            raise ToolError(f"{who}重叠。请把重叠或相邻的改动合并成一个 edit。整个调用没有写入任何改动。")
    out = content
    for a, b, h in reversed(flat):
        out = out[:a] + pieces[(a, b)] + out[b:]
    if out == content:
        raise ToolError("替换后文件内容没有变化。")
    return out, results


def replace(content: str, old: str, new: str, replace_all: bool = False) -> tuple[str, str, int]:
    """单处替换（旧接口）。返回 (新内容, 用到的策略, 替换次数)。"""
    out, hits = apply_edits(content, [Edit(old.replace("\r\n", "\n"), new.replace("\r\n", "\n"), replace_all)])
    return out, hits[0].strategy, len(hits[0].spans)
