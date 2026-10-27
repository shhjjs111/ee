"""按块接受/拒绝（D5-7）的位置安全：拒绝必须改在 hunk 记录的那一处。

缺陷（已用探针复现）：`hunks._find()` 只是从 hunk 的行号锚点向两侧找**最近的**命中，
不判断“最近的那处是不是同一处”。0 上下文（纯增删）的 hunk 只有一行内容，一旦文件里
另有相同内容，就会改到无关的那一处：

    文件第 11 行 return 1（用户后来手改成 return 2）
    文件第 22 行 return 1（正是 hunk 记录的那一处，diff 后又被还原了）
    -> 拒绝这一块时改的是第 11 行，把用户的手改抹掉了。

修法不是“多处命中就报错”（正常拒绝也可能命中多处，那样等于把功能修坏），而是
**给搜索加距离上限**：离开锚点太远的命中不算，宁可报错让用户刷新。
"""
import difflib

import pytest

from ee.core import hunks
from ee.core.hunks import HunkError


def patch_of(old: str, new: str, n: int | None = None) -> str:
    kw = {} if n is None else {"n": n}
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        fromfile="a/f", tofile="b/f", **kw))


def pad(count: int, tag: str = "line") -> str:
    return "".join(f"{tag}{i}\n" for i in range(1, count + 1))


def far_duplicate_case():
    """构造：第 11 行与第 22 行内容相同，diff 只改第 22 行（0 上下文）。

    返回 (原文, 改后, 第 11 行已被手改的当前文件, hunk)。
    """
    old = pad(10) + "    return 1\n" + pad(10, "mid") + "    return 1\n"
    lines = old.split("\n")
    hits = [i for i, line in enumerate(lines) if line == "    return 1"]
    assert hits == [10, 21], hits                  # 0 基：第 11 行、第 22 行
    newlines = list(lines)
    newlines[21] = "    return 2"                  # 只改第 22 行
    new = "\n".join(newlines)
    h = hunks.parse_hunks(patch_of(old, new, n=0))[0]
    assert h.new_start == 22, h.new_start
    cur = old.replace("    return 1\n", "    return 2\n", 1)   # 用户手改第 11 行
    return old, new, cur, h


# ---------------------------------------------------------------- 缺陷复现


def test_reject_does_not_touch_far_duplicate():
    """锚点在第 22 行、第 11 行另有同样内容（用户手改过）→ 不能改第 11 行，必须报错。"""
    _old, _new, cur, h = far_duplicate_case()
    assert cur.split("\n")[10] == "    return 2"   # 第 11 行是用户手改的
    assert cur.split("\n")[21] == "    return 1"
    with pytest.raises(HunkError):
        hunks.reverse_apply(cur, h)


def test_apply_finds_anchor_when_duplicate_already_changed():
    """apply 找的是『旧侧』内容：远处那处已被改成新侧时不会被吸引，仍改锚点这处。"""
    old, new, cur, h = far_duplicate_case()
    out = hunks.apply(cur, h)
    lines = out.split("\n")
    assert lines[10] == "    return 2"      # 用户手改的第 11 行保持不变
    assert lines[21] == "    return 2"      # 锚点那处被正确应用


# ---------------------------------------------------------------- 不能修坏正常功能


def test_reject_still_works_when_anchor_matches():
    """锚点就是那一处 → 正常拒绝。"""
    old, new, _cur, h = far_duplicate_case()
    assert hunks.reverse_apply(new, h) == old


def test_small_drift_still_matches():
    """锚点整体下移几行（用户在文件开头加了几行）→ 仍应拒绝成功，不能太严。"""
    old, new, _cur, h = far_duplicate_case()
    shifted = pad(5, "header") + new
    assert hunks.reverse_apply(shifted, h) == pad(5, "header") + old


@pytest.mark.parametrize("ctx", [0, 1, 3])
def test_roundtrip_invariant(ctx):
    """reverse_apply(apply(old, h), h) == old —— 加了距离限制后依然成立。"""
    old, new, _cur, _h0 = far_duplicate_case()
    h = hunks.parse_hunks(patch_of(old, new, n=ctx))[0]
    assert hunks.apply(old, h) == new
    assert hunks.reverse_apply(new, h) == old


def test_roundtrip_with_duplicate_blocks_and_context():
    """带上下文时重复块也能正确往返（上下文让命中唯一）。"""
    block = "def f():\n    log(x)\n    return 1\n"
    old = block * 3
    new = block.replace("return 1", "return 2") + block + block
    h = hunks.parse_hunks(patch_of(old, new))[0]
    assert hunks.apply(old, h) == new
    assert hunks.reverse_apply(new, h) == old
