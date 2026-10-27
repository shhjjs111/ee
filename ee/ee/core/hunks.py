"""unified diff 的按块（hunk）解析与应用（纯函数，不碰磁盘）。

- parse_hunks(patch)：拆出单个文件 patch 里的所有块。行内容保留 `\\r`（只按 `\\n` 切），
  `\\ No newline at end of file` 标记会记到对应那一侧的最后一行上。
- reverse_apply(content, hunk)：在“新”内容里找到这一块的新侧行，换回旧侧行（= 拒绝这块改动）。
- apply(content, hunk)：反过来，在旧内容里把旧侧换成新侧。
- 先按行号精确找，找不到就上下偏移找（前面的块被拒绝过、行号会挪）；再找不到就忽略行尾 \\r 再找一遍
  （git 对未跟踪文件生成的 diff 是归一过换行的），替换进去的行沿用文件里原来的换行风格。
- fingerprint：只由块里的增删/上下文行（去掉 \\r）决定，不含行号，前面的块变了也不变；用来标记“已接受”。
- 二进制安全：调用方用 utf-8 + surrogateescape 解码再编码回去，任意字节都能原样往返。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class HunkError(ValueError):
    pass


@dataclass
class Hunk:
    header: str
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list[tuple[str, str]] = field(default_factory=list)   # (' '|'-'|'+', 行内容，不含 \n，可能含 \r)
    old_nonl: bool = False        # 旧侧最后一行没有换行
    new_nonl: bool = False

    def side(self, which: str) -> list[str]:
        """旧侧（which='-'）或新侧（'+'）的行，带换行符。"""
        out = [t for tag, t in self.lines if tag == " " or tag == which]
        nonl = self.old_nonl if which == "-" else self.new_nonl
        return [t + ("" if (nonl and i == len(out) - 1) else "\n") for i, t in enumerate(out)]

    @property
    def additions(self) -> int:
        return sum(1 for tag, _ in self.lines if tag == "+")

    @property
    def deletions(self) -> int:
        return sum(1 for tag, _ in self.lines if tag == "-")

    @property
    def fingerprint(self) -> str:
        # 按界面上看到的文本算（解码不了的字节 → �），这样界面拿到的 patch 和后端重新算的 patch 指纹一致
        body = display("\n".join(tag + t.rstrip("\r") for tag, t in self.lines))
        return hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]

    def text(self) -> str:
        return "\n".join([self.header, *(tag + t for tag, t in self.lines)])


def parse_hunks(patch: str) -> list[Hunk]:
    hunks: list[Hunk] = []
    cur: Hunk | None = None
    last_tag = ""
    left_old = left_new = 0
    for raw in patch.split("\n"):
        m = _HUNK.match(raw)
        if m:
            cur = Hunk(raw.rstrip("\r"), int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1),
                       int(m.group(3)), int(m.group(4) if m.group(4) is not None else 1))
            left_old, left_new = cur.old_len, cur.new_len
            hunks.append(cur)
            last_tag = ""
            continue
        if cur is None:
            continue
        if raw.startswith("\\"):
            if last_tag in (" ", "-"):
                cur.old_nonl = cur.old_nonl or last_tag == "-" or (last_tag == " " and left_old == 0)
            if last_tag in (" ", "+"):
                cur.new_nonl = cur.new_nonl or last_tag == "+" or (last_tag == " " and left_new == 0)
            continue
        if left_old <= 0 and left_new <= 0:
            cur = None                     # 这一块已经满了，后面是下一个文件的头或者垃圾
            continue
        tag = raw[:1]
        if tag == "" and raw == "":
            tag, body = " ", ""            # 有的工具会把空的上下文行写成真正的空行
        elif tag in (" ", "-", "+"):
            body = raw[1:]
        else:
            cur = None
            continue
        cur.lines.append((tag, body))
        last_tag = tag
        if tag in (" ", "-"):
            left_old -= 1
        if tag in (" ", "+"):
            left_new -= 1
    return hunks


def split_lines(content: str) -> list[str]:
    """只按 \\n 切、保留换行符（不像 str.splitlines 会把 \\r、\\x0b、\\u2028 也当换行）。"""
    if not content:
        return []
    parts = content.split("\n")
    out = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out


def _norm(line: str) -> str:
    return line.rstrip("\n").rstrip("\r")


MAX_DRIFT = 200           # 有上下文行的 hunk：行号锚点允许的偏移上限
CONTEXT_FREE_DRIFT = 8    # 纯增删（0 上下文）的 hunk：只容忍文件头尾小幅增删造成的整体偏移


def _find(lines: list[str], want: list[str], start: int, loose: bool, max_drift: int = MAX_DRIFT) -> int:
    """从行号锚点 start 向两侧找最近的命中，最远 max_drift 行。

    为什么需要这个上限：hunk 只记了行号，文件里出现同样内容时“最近的那处”未必是
    hunk 记录的那一处。0 上下文的纯增删块尤其危险——want 里只有改动行本身、
    没有任何定位信息，一旦文件里另有同样内容就会改错位置，把用户后来手改的行改回去
    （见 tests/test_hunks_drift.py）。这种情况调用方传 max_drift=0：只认锚点原位。
    """
    n, k = len(lines), len(want)
    if k == 0:
        return max(0, min(start, n))
    cmp = [_norm(w) for w in want] if loose else want

    def ok(i: int) -> bool:
        seg = lines[i:i + k]
        if len(seg) != k:
            return False
        if loose:
            return [_norm(x) for x in seg] == cmp
        return seg == cmp

    start = max(0, min(start, n - k))
    limit = min(max(0, max_drift), n)
    for off in range(0, limit + 1):
        for i in (start - off, start + off) if off else (start,):
            if 0 <= i <= n - k and ok(i):
                return i
    return -1


def _swap(content: str, hunk: Hunk, src: str, dst: str) -> str:
    lines = split_lines(content)
    want, repl = hunk.side(src), hunk.side(dst)
    start_no = hunk.new_start if src == "+" else hunk.old_start
    length = hunk.new_len if src == "+" else hunk.old_len
    start = start_no if length == 0 else start_no - 1       # 长度 0 的一侧，行号是“插在它后面”
    # 上下文行（' '）是 hunk 的定位信息：有它才敢在锚点附近大幅浮动找；纯增删只有改动行
    # 本身，另一处内容相同就会改错位置，所以只容忍很小的整体偏移（用户在文件开头加几行）。
    has_context = any(tag == " " for tag, _ in hunk.lines)
    drift = MAX_DRIFT if has_context else CONTEXT_FREE_DRIFT
    i = _find(lines, want, start, loose=False, max_drift=drift)
    loose = False
    if i < 0:
        i = _find(lines, want, start, loose=True, max_drift=drift)
        loose = True
    if i < 0:
        raise HunkError("文件内容已经变了，这块改动对不上（请刷新后再试）")
    if loose:
        seg = lines[i:i + len(want)]
        eol = "\r\n" if any(x.endswith("\r\n") for x in seg) or (not seg and _crlf_file(lines)) else "\n"
        repl = [r.rstrip("\n").rstrip("\r") + eol if r.endswith("\n") else r for r in repl]
    # 被替换的段是文件最后一段且原来最后一行没有换行、替换进来的最后一行却带换行（或反过来）时，以块为准
    out = lines[:i] + repl + lines[i + len(want):]
    if i + len(want) < len(lines) and repl and not repl[-1].endswith("\n"):
        # 块说替换后的最后一行没有换行，但它后面还有行：说明对错位了，补回换行避免两行粘在一起
        out[i + len(repl) - 1] += "\n"
    return "".join(out)


def _crlf_file(lines: list[str]) -> bool:
    crlf = sum(1 for x in lines if x.endswith("\r\n"))
    return crlf > 0 and crlf * 2 >= len(lines)


def reverse_apply(content: str, hunk: Hunk) -> str:
    """拒绝这块：把 content（新内容）里这一块的新侧换回旧侧。"""
    return _swap(content, hunk, "+", "-")


def apply(content: str, hunk: Hunk) -> str:
    """应用这块：把 content（旧内容）里这一块的旧侧换成新侧。"""
    return _swap(content, hunk, "-", "+")


def decode(data: bytes) -> str:
    return data.decode("utf-8", "surrogateescape")


def encode(text: str) -> bytes:
    return text.encode("utf-8", "surrogateescape")


def display(text: str) -> str:
    """给界面看的文本：解码不了的字节换成 �（surrogateescape 的代理字符不能进 JSON）。"""
    return encode(text).decode("utf-8", "replace")
