"""改文件引擎（edit / write / apply_patch 共用）：读写保留编码细节、容错匹配链、diff。

- 换行：读入时统一成 \\n 处理，写回时沿用原文件的换行风格（Windows 下 CRLF 文件不会被改成 LF）。
- BOM：保留。
- 容错链：精确 → 去行尾空白 → 逐行 trim → 缩进无关 → 首尾锚点 + 相似度 ≥0.65。
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path

from ee.tools.base import ToolError

BOM = "\ufeff"
SIMILARITY = 0.65


@dataclass
class TextFile:
    text: str        # 已统一成 \n
    eol: str         # "\n" | "\r\n"
    bom: bool
    existed: bool


def read_text(path: Path) -> TextFile:
    if not path.exists():
        return TextFile("", "\n", False, False)
    if not path.is_file():   # FIFO / 设备文件：打开或读取会一直阻塞（或读不完）
        raise ToolError(f"{path} 不是普通文件（可能是管道或设备），不能按文本读写")
    raw = path.read_bytes()
    if b"\x00" in raw[:8192]:
        raise ToolError(f"{path} 看起来是二进制文件，不能按文本编辑")
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            s = raw.decode("gbk")  # Windows 中文老文件
        except UnicodeDecodeError:
            s = raw.decode("utf-8", errors="replace")
    bom = s.startswith(BOM)
    if bom:
        s = s[1:]
    eol = "\r\n" if "\r\n" in s else "\n"
    return TextFile(s.replace("\r\n", "\n"), eol, bom, True)


def write_text(path: Path, tf: TextFile, new_text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = new_text.replace("\r\n", "\n")
    if tf.eol == "\r\n":
        out = out.replace("\n", "\r\n")
    if tf.bom:
        out = BOM + out
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(out)


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


def _dedent(lines: list[str]) -> list[str]:
    ind = [len(l) - len(l.lstrip()) for l in lines if l.strip()]
    m = min(ind) if ind else 0
    return [l[m:] if l.strip() else "" for l in lines]


def _indent_flexible(content: str, find: str) -> list[tuple[int, int]]:
    clines = content.split("\n")
    flines = find.split("\n")
    if flines and flines[-1] == "":
        flines = flines[:-1]
    n = len(flines)
    if not n:
        return []
    target = _dedent(flines)
    spans = _line_spans(content)
    out = []
    for i in range(len(clines) - n + 1):
        if [x.rstrip() for x in _dedent(clines[i:i + n])] == [x.rstrip() for x in target]:
            out.append((spans[i][0], spans[i + n - 1][1]))
    return out


def _lev_ratio(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _block_anchor(content: str, find: str) -> list[tuple[int, int]]:
    """首尾两行（trim 后）当锚点，中间行平均相似度 ≥0.65 时接受，取最相似的一个。"""
    flines = find.split("\n")
    if flines and flines[-1] == "":
        flines = flines[:-1]
    if len(flines) < 3:
        return []
    clines = content.split("\n")
    spans = _line_spans(content)
    first, last = flines[0].strip(), flines[-1].strip()
    n = len(flines)
    max_delta = max(1, n // 4)
    best, best_score = None, -1.0
    for i, l in enumerate(clines):
        if l.strip() != first:
            continue
        for j in range(i + 2, min(len(clines), i + n + max_delta)):
            if clines[j].strip() != last:
                continue
            size = j - i + 1
            if abs(size - n) > max_delta:
                continue
            mid_c = [x.strip() for x in clines[i + 1:j]]
            mid_f = [x.strip() for x in flines[1:-1]]
            k = max(len(mid_c), len(mid_f)) or 1
            score = sum(_lev_ratio(a, b) for a, b in zip(mid_c, mid_f)) / k
            if score > best_score:
                best, best_score = (spans[i][0], spans[j][1]), score
            break
    if best and best_score >= SIMILARITY:
        return [best]
    return []


STRATEGIES = [
    ("exact", None),
    ("rstrip", lambda c, f: _block_matches(c, f, str.rstrip)),
    ("trim", lambda c, f: _block_matches(c, f, str.strip)),
    ("indent", _indent_flexible),
    ("anchor", _block_anchor),
]


def _exact(content: str, find: str) -> list[tuple[int, int]]:
    out, start = [], 0
    while True:
        i = content.find(find, start)
        if i < 0:
            return out
        out.append((i, i + len(find)))
        start = i + max(1, len(find))


def replace(content: str, old: str, new: str, replace_all: bool = False) -> tuple[str, str, int]:
    """返回 (新内容, 用到的策略, 替换次数)。失败抛 ToolError（信息要具体，便于模型改正）。"""
    old = old.replace("\r\n", "\n")
    new = new.replace("\r\n", "\n")
    if old == "":
        raise ToolError("old_string 为空。新建文件请用 write 工具；在空文件里写内容也用 write。")
    if old == new:
        raise ToolError("old_string 与 new_string 相同，没有任何改动。")
    for name, fn in STRATEGIES:
        hits = _exact(content, old) if fn is None else fn(content, old)
        if not hits:
            continue
        if len(hits) > 1 and not replace_all:
            raise ToolError(f"old_string 在文件中匹配到 {len(hits)} 处（策略 {name}）。请在 old_string 里多带几行上下文"
                            "让它唯一，或者确实要全部替换时设 replace_all=true。")
        old_lines = old.rstrip("\n").count("\n") + 1
        for a, b in hits:
            got = content[a:b].count("\n") + 1
            if got > old_lines * 1.5 + 3:
                raise ToolError(f"模糊匹配到的块（{got} 行）比 old_string（{old_lines} 行）大太多，已拒绝。请先 read 确认原文再精确复制。")
        new_eff = new
        if fn is not None:
            # 行块匹配不含末尾换行；old/new 若以 \n 结尾，去掉一个保持对齐
            if old.endswith("\n") and new_eff.endswith("\n"):
                new_eff = new_eff[:-1]
        out = content
        for a, b in sorted(hits, reverse=True):
            out = out[:a] + new_eff + out[b:]
        return out, name, len(hits)
    raise ToolError("在文件中找不到 old_string（已尝试：精确、去行尾空白、逐行 trim、缩进无关、首尾锚点相似度）。"
                    "请重新 read 该文件，按行号后面的原文逐字复制（不要带行号前缀）。")
