"""没有 rg 时的回退搜索不能跳过 GBK 文件（2026-11 审计 P2#25）。

`search.py` 开头就写着「优先调 rg，没有 rg（Windows 常见）时用纯 Python 回退」——
也就是说 Windows 用户走的就是回退路径。而回退原来用

    open(p, encoding="utf-8", errors="strict")

读文件，`UnicodeDecodeError` 落到 `except: continue`：中文 Windows 上的 GBK 文件
（老项目源码、日志、导出的 CSV）整份跳过，用户搜不到内容，也没有任何提示。

这在本产品的主平台上不算边角：默认编码是 cp936，很多既有文件根本不是 UTF-8。
"""
import re

from ee.tools.search import _py_grep


def test_grep_fallback_finds_gbk_file(tmp_path):
    f = tmp_path / "老项目日志.txt"
    f.write_bytes("第一行\n这里是中文关键词\n最后一行\n".encode("gbk"))

    per_file = _py_grep(tmp_path, re.compile("中文关键词"), None)

    assert str(f) in per_file, f"GBK 文件被整份跳过了：{per_file}"
    assert "中文关键词" in per_file[str(f)][0]


def test_grep_fallback_still_finds_utf8(tmp_path):
    f = tmp_path / "utf8.txt"
    f.write_text("hello\n中文关键词\n", encoding="utf-8")

    per_file = _py_grep(tmp_path, re.compile("中文关键词"), None)

    assert str(f) in per_file


def test_grep_fallback_skips_binary(tmp_path):
    """两种编码都读不出来时按二进制跳过——不能把乱码当文本返回。"""
    f = tmp_path / "blob.bin"
    f.write_bytes(bytes([0x81, 0x30, 0xFF, 0xFE, 0x00]))

    per_file = _py_grep(tmp_path, re.compile("."), None)

    assert str(f) not in per_file
