"""版本化只读证据（Wave 5 / T17，对应 ACC-40/41/42）。

**要解决的问题**：`read` 判断"文件有没有变"只看 mtime + size。这会两头错：

1. **假失效**：文件被 touch 或编辑器原子替换（内容没变、mtime 变了），
   缓存被丢掉，白读一遍；
2. **假命中**：内容变了但 mtime/size 恰好还原（复制、还原快照、时钟回溯），
   仍被当成"没变"，返回过期内容。

mtime/size 只能说明"可能变了"，**内容身份才是最终判据**。

**分层**（这里只做纯计算与键构造，不碰磁盘 IO）：
- 小文件：完整内容的 sha256 —— 强身份，可以断言"完全没变"
- 大文件：分块摘要 + 首尾 —— 弱身份，不确定时必须重读（调用方负责）

**绝不缓存**：shell 输出、测试结果、网络抓取、写入类工具、审批结论。
这些依赖环境或副作用，缓存即是制造假证据。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

SCHEMA_VERSION = 1

# 分块弱身份的参数：块大小与采样块数。超过这个规模的文件不逐字节哈希。
CHUNK = 64 * 1024
# 小到可以整文件哈希的上限（16MB 以内逐字节，足够覆盖绝大多数源码文件）
STRONG_MAX_BYTES = 16 * 1024 * 1024

# 允许缓存的只读工具白名单。新增工具必须在这里显式登记，
# 默认拒绝——避免"顺手给 shell 也加上缓存"这类事故。
CACHEABLE_TOOLS = frozenset({"read", "repo_map"})


class NotCacheable(ValueError):
    """这个工具/这次调用不允许走证据缓存。"""


def content_identity(data: bytes) -> tuple[str, str]:
    """返回 (identity, strength)。strength ∈ {"strong", "weak"}。

    strong：整段内容的 sha256，可断言完全一致；
    weak：分块摘要 + 长度，只能说明"很可能没变"，命中要保守。
    """
    n = len(data)
    if n <= STRONG_MAX_BYTES:
        return "sha256:" + hashlib.sha256(data).hexdigest(), "strong"

    h = hashlib.sha256()
    h.update(str(n).encode())
    # 头部、尾部各取一块，中间按固定间隔取块——覆盖编辑最常见的两端与散点
    takes = [data[:CHUNK], data[-CHUNK:]] if n > 2 * CHUNK else [data]
    step = max(CHUNK, n // 8)
    for off in range(CHUNK, n - CHUNK, step):
        takes.append(data[off:off + CHUNK])
    for t in takes:
        h.update(hashlib.sha256(t).digest())
    return "weak:" + h.hexdigest(), "weak"


@dataclass(frozen=True)
class EvidenceKey:
    """一次只读调用的完整身份。

    任何一项变化都必须视为不同证据——尤其是 permission_scope：
    曾经可读不能推出现在可读。
    """
    tool: str
    tool_version: int
    root: str
    rel_path: str
    content_identity: str
    transform: tuple
    permission_scope: str
    readonly: bool

    def as_tuple(self) -> tuple:
        return (SCHEMA_VERSION, self.tool, self.tool_version, self.root, self.rel_path,
                self.content_identity, self.transform, self.permission_scope, self.readonly)

    def digest(self) -> str:
        blob = "\x00".join(str(x) for x in self.as_tuple())
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def make_key(*, tool: str, root: str, rel_path: str, data_or_identity, transform: tuple,
             permission_scope: str = "default", readonly: bool = True,
             tool_version: int = 1) -> EvidenceKey:
    """构造证据键。data_or_identity 可以是 bytes（现算身份）或已算好的身份字符串。"""
    if tool not in CACHEABLE_TOOLS:
        raise NotCacheable(f"{tool} 不允许缓存（只读派生才可缓存）")
    if isinstance(data_or_identity, bytes):
        ident, _ = content_identity(data_or_identity)
    else:
        ident = str(data_or_identity)
    return EvidenceKey(
        tool=tool, tool_version=tool_version,
        root=_norm_root(root), rel_path=_norm_rel(rel_path),
        content_identity=ident, transform=tuple(transform),
        permission_scope=permission_scope, readonly=readonly,
    )


def _norm_root(p: str) -> str:
    return (p or "").replace("\\", "/").rstrip("/").lower()


def _norm_rel(p: str) -> str:
    return (p or "").replace("\\", "/").lstrip("./")


def may_use_cache(tool: str, *, permission_ok: bool) -> bool:
    """能不能用缓存。权限必须先判定：缓存不能成为绕过权限的通道。"""
    return bool(permission_ok) and tool in CACHEABLE_TOOLS
