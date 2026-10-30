"""证据缓存判据接线（Wave 5 / T17，对应 ACC-40/41/42）——用真实 ReadTool 复现。

现状缺陷（不是测试造出来的）：

`ToolState.mark_read` 用 `mtime_ns + size` 判"文件变没变"（ee/tools/base.py:55）。
`evidence.py` 已实现 `content_identity`（内容哈希），但**零生产消费方**。

后果是两个方向的假象：

1. **假失效（ACC-40）**：文件被 touch / 编辑器原子替换（内容没变、mtime 变），
   `mark_read` 会新建 FileRecord、清掉 reads 计数——本该命中"与上次相同"的引用
   被丢掉，白读一遍；
2. **假命中（ACC-41）**：内容变了但 mtime/size 恰好还原（复制、还原快照），
   `mark_read` 仍当它"没变"——返回过期内容的引用，模型读到的是旧正文。

本文件锁的不变量：**判"文件是否变化"必须用内容身份（哈希），不能用 mtime+size。**
"""
from __future__ import annotations

import asyncio
import os

import pytest

from ee.tools.base import ToolContext, ToolState
from ee.tools.read import ReadTool


def _ctx(tmp_path, state: ToolState | None = None) -> ToolContext:
    return ToolContext(cwd=tmp_path, state=state or ToolState(), output_dir=tmp_path / ".out")


def _read(tmp_path, ctx, name="big.py", **args):
    tool = ReadTool()
    return asyncio.run(tool.run({"path": name, **args}, ctx))


def test_touch_does_not_invalidate_read_ref(tmp_path):
    """ACC-40 假失效：内容不变、mtime 变了（touch / 原子替换）→ 仍应命中引用。

    当前实现只看 mtime_ns + size，touch 会让它误判"文件变了"，丢掉已读记账。
    """
    (tmp_path / "big.py").write_text("x = 1\n" * 800, encoding="utf-8")   # > REF_MIN_BYTES
    ctx = _ctx(tmp_path)

    first = _read(tmp_path, ctx)
    assert "x = 1" in first.output

    # touch：只改 mtime，内容一字不动
    os.utime(tmp_path / "big.py", None)

    second = _read(tmp_path, ctx)
    assert "与上次读取相同" in second.output, (
        f"内容没变，仅 mtime 变，却丢了引用——假失效。实际：{second.output[:120]}"
    )


def test_content_change_with_restored_mtime_size_must_invalidate(tmp_path):
    """ACC-41 假命中：内容变了、mtime/size 被还原 → 必须失效，不能返回过期引用。

    这是 mtime+size 判据的死角：复制/还原快照会让 mtime 和 size 都回到原值，
    但内容已经不同。必须用内容身份抓住它。
    """
    p = tmp_path / "big.py"
    p.write_text("A" * 4000, encoding="utf-8")   # 4000 字节，> REF_MIN_BYTES
    ctx = _ctx(tmp_path)

    first = _read(tmp_path, ctx)
    assert "A" in first.output

    # 记录原始 mtime，然后改内容、再把 mtime 还原（size 相同 → mtime+size 都骗过）
    st = p.stat()
    orig_mtime_ns = st.st_mtime_ns
    orig_size = st.st_size
    p.write_text("B" * 4000, encoding="utf-8")   # 同长度，内容不同
    assert p.stat().st_size == orig_size, "测试前提：size 必须相同，否则不是最小反例"
    os.utime(p, ns=(orig_mtime_ns, orig_mtime_ns))   # mtime 还原

    second = _read(tmp_path, ctx)
    assert "与上次读取相同" not in second.output, (
        "内容变了但 mtime/size 还原，却仍返回旧内容的引用——假命中。实际：{second.output[:120]}"
    )
    assert "B" in second.output, "第二次读必须返回新正文，而不是旧引用"
