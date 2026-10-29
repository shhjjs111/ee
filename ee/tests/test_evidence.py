"""证据身份与缓存准入的反证（Wave 5 / T17，对应 ACC-40/41/42）。

三条必须同时成立，否则缓存比不缓存更危险：

1. 内容没变、mtime 变了（touch / 编辑器原子替换）→ 仍可复用；
2. 内容变了、mtime/size 被还原 → 必须失效（不能返回过期内容）；
3. 权限收紧（只读 / .eeignore / 沙箱）→ 即使有缓存也必须拒绝。
"""
from __future__ import annotations

import pytest

from ee.core import evidence as E


class TestContentIdentity:
    def test_same_bytes_same_identity(self):
        a, s1 = E.content_identity(b"hello world")
        b, s2 = E.content_identity(b"hello world")
        assert a == b and s1 == s2 == "strong"

    def test_different_bytes_different_identity(self):
        a, _ = E.content_identity(b"hello world")
        b, _ = E.content_identity(b"hello worlD")
        assert a != b

    def test_same_size_different_content_is_detected(self):
        """同长度的不同内容必须区分开——这正是 mtime+size 会漏掉的情况。"""
        a, _ = E.content_identity(b"AAAA")
        b, _ = E.content_identity(b"BBBB")
        assert a != b

    def test_large_file_uses_weak_identity(self, monkeypatch):
        monkeypatch.setattr(E, "STRONG_MAX_BYTES", 1024)
        _, strength = E.content_identity(b"x" * 4096)
        assert strength == "weak", "大文件不该逐字节哈希，但必须标明是弱身份"

    def test_weak_identity_still_detects_tail_change(self, monkeypatch):
        monkeypatch.setattr(E, "STRONG_MAX_BYTES", 1024)
        big = bytearray(b"x" * 8192)
        a, _ = E.content_identity(bytes(big))
        big[-100] = ord("y")                    # 改尾部：必须能发现
        b, _ = E.content_identity(bytes(big))
        assert a != b


class TestEvidenceKey:
    def test_key_stable_for_same_inputs(self):
        k1 = E.make_key(tool="read", root="/w", rel_path="a.py", data_or_identity=b"x",
                        transform=(1, 100))
        k2 = E.make_key(tool="read", root="/w", rel_path="a.py", data_or_identity=b"x",
                        transform=(1, 100))
        assert k1.digest() == k2.digest()

    def test_different_range_is_different_key(self):
        """读同一文件的不同区间不能共用一个键。"""
        k1 = E.make_key(tool="read", root="/w", rel_path="a.py", data_or_identity=b"x", transform=(1, 100))
        k2 = E.make_key(tool="read", root="/w", rel_path="a.py", data_or_identity=b"x", transform=(101, 200))
        assert k1.digest() != k2.digest()

    def test_different_permission_scope_is_different_key(self):
        """权限范围变了必须是另一个键——曾经可读不能推出现在可读。"""
        k1 = E.make_key(tool="read", root="/w", rel_path="a.py", data_or_identity=b"x",
                        transform=(1, 10), permission_scope="default")
        k2 = E.make_key(tool="read", root="/w", rel_path="a.py", data_or_identity=b"x",
                        transform=(1, 10), permission_scope="readonly")
        assert k1.digest() != k2.digest()

    def test_case_and_separator_normalized_on_windows(self):
        """Windows 上大小写与分隔符不同的同一路径必须归到同一个键。"""
        k1 = E.make_key(tool="read", root=r"C:\Work", rel_path=r"src\A.py", data_or_identity=b"x",
                        transform=(1, 10))
        k2 = E.make_key(tool="read", root="c:/work", rel_path="src/A.py", data_or_identity=b"x",
                        transform=(1, 10))
        assert k1.digest() == k2.digest()

    def test_content_change_changes_key(self):
        """内容变了键就变——mtime/size 还原也骗不过内容身份。"""
        k1 = E.make_key(tool="read", root="/w", rel_path="a.py", data_or_identity=b"v1", transform=(1, 10))
        k2 = E.make_key(tool="read", root="/w", rel_path="a.py", data_or_identity=b"v2", transform=(1, 10))
        assert k1.digest() != k2.digest()


class TestNotCacheable:
    @pytest.mark.parametrize("tool", ["shell", "web_fetch", "web_search", "write", "edit", "apply_patch", "task"])
    def test_mutating_or_environment_dependent_tools_refused(self, tool):
        """shell 输出、网络抓取、写入类工具、子代理结论都不能缓存。"""
        with pytest.raises(E.NotCacheable):
            E.make_key(tool=tool, root="/w", rel_path="x", data_or_identity=b"v", transform=())

    def test_read_is_allowed(self):
        k = E.make_key(tool="read", root="/w", rel_path="a.py", data_or_identity=b"v", transform=(1, 10))
        assert k.tool == "read"


class TestPermissionGate:
    def test_permission_denied_blocks_cache_even_if_cached(self):
        """权限不通过时，无论有没有缓存都不能用。"""
        assert E.may_use_cache("read", permission_ok=False) is False

    def test_permission_ok_allows_cacheable_tool(self):
        assert E.may_use_cache("read", permission_ok=True) is True

    def test_permission_ok_does_not_allow_non_cacheable_tool(self):
        assert E.may_use_cache("shell", permission_ok=True) is False
