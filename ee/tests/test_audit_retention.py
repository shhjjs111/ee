"""工具输出保留策略的覆盖面（2026-11 审计修复）。

retention 的清理白名单只认 `[a-z_]+` 的工具名段，而 MCP 工具名经 safe_name 后保留
大小写字母、数字和连字符（如 `mcp__fs2__ReadFile`）——这类归档永远匹配不上，
在 ~/.ee/tool-output 里只进不出。另外 MCP 的图片/音频写在 `tool-output/mcp-images/`
子目录，gc 只 iterdir() 顶层且 is_file() 跳过目录，整棵子树从不清理。
"""
import os
import time

from ee.core.retention import gc


def _old(p, days: float = 40.0):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x", encoding="utf-8")
    t = time.time() - days * 86400
    os.utime(p, (t, t))
    return p


def test_archive_from_mcp_tool_is_cleaned(tmp_path):
    """MCP 工具名带大写/数字，归档名必须照样能清。"""
    out = tmp_path / "tool-output"
    mcp = _old(out / "20260101-000000-mcp__fs2__ReadFile-abcdef.txt")
    plain = _old(out / "20260101-000000-bash-abcdef.txt")      # 对照：一直是能清的

    gc(tmp_path, keep_days=30, force=True)

    assert not mcp.exists(), "含大写/数字的 MCP 归档永不清理"
    assert not plain.exists()


def test_mcp_images_subdir_is_cleaned(tmp_path):
    img = _old(tmp_path / "tool-output" / "mcp-images" / "a1b2c3d4e5.png")

    gc(tmp_path, keep_days=30, force=True)

    assert not img.exists(), "mcp-images 子目录不在清理范围内"


def test_fresh_files_are_kept(tmp_path):
    out = tmp_path / "tool-output"
    fresh = _old(out / "20260101-000000-bash-abcdef.txt", days=1)

    gc(tmp_path, keep_days=30, force=True)

    assert fresh.exists(), "没到保留期的文件被删了"


def test_files_outside_the_whitelist_are_never_touched(tmp_path):
    """目录在用户 home 下：白名单之外的任何文件都不能碰。"""
    out = tmp_path / "tool-output"
    out.mkdir()
    mine = out / "我的笔记.txt"
    _old(mine)
    images = tmp_path / "tool-output" / "mcp-images"
    images.mkdir(parents=True)
    weird = images / "important.dat"
    _old(weird)

    gc(tmp_path, keep_days=30, force=True)

    assert mine.exists(), "白名单之外的文件被删了"
    assert weird.exists(), "mcp-images 里非我方命名的文件被删了"
