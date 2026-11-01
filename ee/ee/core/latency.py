"""模型接口首包延迟的进程内统计（按 base_url）。

**为什么要有它**：首包慢的时候，用户只看到一个不动的转圈，分不清是「这次特别慢」
还是「这个接口一直这么慢」。实测本机某中转站首包中位 18.8 秒、p90 35.9 秒，而同一天
另一些请求只有 3.8 秒——同一个接口的抖动比接口之间的差距还大。不给参照的数字
（「已等待 15 秒」）只会让人更焦虑，给了中位数才知道该等还是该换接口。

**为什么不自己落盘**：

- 落盘意味着每个测试用例都可能去写用户真实的 `~/.ee`（EE_HOME 没隔离时），
  而这条统计的全部价值只体现在提示文案里，不值得引入这个污染面；
- 但纯内存会让统计在每次启动时清零，而慢接口往往就是**刚开工时**慢——最该看到
  参照的前几次恰好没样本。所以启动时从后端日志回填 `首包=Nms`：日志本来就在写，
  读一遍尾部即可，既不新增写入面，也不依赖额外文件。
"""
from __future__ import annotations

import re
from pathlib import Path

KEEP = 20          # 每个接口只留最近这么多次
MIN_SAMPLES = 3    # 少于这么多次不给中位：2 个样本算出来的“中位”是误导
BACKFILL_BYTES = 1_000_000   # 回填只读日志尾部这么多字节，够攒满 KEEP 条
BACKFILL_FILES = 2           # 当前文件不够时往前翻一个轮转备份
BACKFILL_HITS = 200          # 只回填最近的这么多次请求：太旧的记录不代表“现在这个接口”

_data: dict[str, list[int]] = {}
_backfilled = False


def key_for(base_url: str | None, model: str = "") -> str:
    """同一个 base_url 的不同模型共用一份统计：排队/网络慢通常是接口级现象。"""
    return (base_url or "").strip() or f"(model){model}"


def record(key: str, ms: int | None) -> None:
    """记一次首包耗时（毫秒）。None / 负数（没测到）不记，避免把“没数据”当成“很快”。"""
    if ms is None or ms < 0:
        return
    bucket = _data.setdefault(key, [])
    bucket.append(int(ms))
    del bucket[:-KEEP]


def samples(key: str) -> int:
    return len(_data.get(key) or [])


def median_ms(key: str) -> int | None:
    """该接口最近的首包中位数；样本不足时返回 None（宁可不给，也不给假参照）。"""
    xs = sorted(_data.get(key) or [])
    if len(xs) < MIN_SAMPLES:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) // 2


def reset() -> None:
    """清空统计（测试用）。回填标记一起清掉，否则重置后无法再测回填。"""
    global _backfilled
    _data.clear()
    _backfilled = False


# 日志行格式见 providers/openai_chat.py：
#   模型请求开始：<alias>（<base_url>，<代理说明>）messages=N tools=N 第 N 次
#   模型请求结束：<alias> 用时 X.Xs … 首包=Nms 首字=Nms
# base_url 取到第一个全角逗号为止：后面接的是代理说明，里面还可能带括号。
_START = re.compile(r"模型请求开始：(?P<alias>.+?)（(?P<where>.+?)）messages=\d+")
_END = re.compile(r"模型请求结束：(?P<alias>.+?) 用时 [\d.]+s .*?首包=(?P<ms>\d+)ms")


def backfill_from_log(path: Path | None, *, tail_bytes: int = BACKFILL_BYTES,
                      files: int = BACKFILL_FILES, hits_limit: int = BACKFILL_HITS) -> int:
    """启动时从后端日志回填首包统计，返回回填的条数。只做一次，失败静默。

    key 用日志里打印的 base_url，与运行时 `key_for(spec.base_url)` 一致；alias 只是
    用来把「开始」行的接口地址和「结束」行的首包值配成一对——并发请求交错也不影响，
    因为同一个 alias 只会对应一个 base_url。
    """
    global _backfilled
    if _backfilled or path is None:
        return 0
    lines: list[str] = []
    for p in [path] + [path.with_name(f"{path.name}.{i}") for i in range(1, files)]:
        try:
            size = p.stat().st_size
            with p.open("rb") as f:
                if size > tail_bytes:
                    f.seek(size - tail_bytes)
                    chunk = f.read().split(b"\n", 1)[-1]     # 丢掉被截断的首行
                else:
                    chunk = f.read()
        except OSError:
            continue
        lines = chunk.decode("utf-8", "replace").splitlines() + lines   # 旧的接在前面
    if not lines:
        return 0                     # 日志还不存在（首次启动）：不算“已经回填过”
    _backfilled = True
    urls: dict[str, str] = {}
    hits: list[tuple[str, int]] = []
    for line in lines:
        m = _START.search(line)
        if m:
            url = m.group("where").split("，", 1)[0].strip()
            if "://" in url:          # 默认接口（没配 base_url）对不上运行时的 key，跳过
                urls[m.group("alias")] = url
            continue
        m = _END.search(line)
        if m:
            hits.append((m.group("alias"), int(m.group("ms"))))
    n = 0
    for alias, ms in hits[-hits_limit:] if hits_limit > 0 else hits:
        url = urls.get(alias)
        if url is None:
            continue
        record(key_for(url), ms)
        n += 1
    return n


def heartbeat_text(alias: str, waited: float, median: int | None) -> str:
    """等待首包期间的状态行。有中位数就带上参照，没有就只说等了多久。"""
    if median:
        return f"{alias} 还没有返回首包，已等待 {waited:.0f} 秒（该接口最近中位 {median / 1000:.1f} 秒）…"
    return f"{alias} 还没有返回首包，已等待 {waited:.0f} 秒…"
