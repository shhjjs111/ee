"""逐用例结果与进展判定（2026-11 第三批）。

**为什么要单独一层**：判据的 `tests` 只是一个数字。数字变大可能是真的多修好了，
也可能是少跑了；同一条命令把断言改松、把用例 skip 掉，数字同样会变好看。
所以"值得继续修"必须看**同一批用例的身份与状态变化**，而不是总数。

**这一版的诚实边界**（写进每个判定结果里，不让上层误用）：
- 只认 pytest 与 go 的常见输出；其它 runner 返回 `kind=""`、`complete=False`。
- pytest 不加 `-v` 时只能看到**失败**用例的身份，看不到通过列表。所以"某条原先失败的
  用例现在通过了"是**推断**出来的（失败集合里消失 + 失败数正好少这么多 + skip/xfail/
  deselected 没变），不是直接观测。任一条件不满足就判 `unknown`，绝不猜。
- go 不加 `-v` 只有包级结果：能说"包从红变绿"，不能说"逐用例没有回归"。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 用例状态。error 带阶段：error:setup / error:call / error:teardown（pytest 会区分）。
PASSED, FAILED, SKIPPED, ERROR, XFAIL, XPASS = "passed", "failed", "skipped", "error", "xfail", "xpass"
_NOT_PASSING = (FAILED, ERROR, XFAIL)


@dataclass
class Report:
    """一次判据运行里能读出来的逐用例事实。"""
    kind: str = ""                      # pytest | go | ""（不认识）
    cases: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)   # 汇总行的计数（passed/failed/skipped/...）
    complete: bool = False              # 能否确认"本次应执行的集合"被完整识别
    note: str = ""                      # 不完整/不确定的原因（给人看）

    @property
    def failed(self) -> set[str]:
        return {k for k, v in self.cases.items() if v.startswith(FAILED) or v.startswith(ERROR)}

    def status(self, cid: str) -> str | None:
        return self.cases.get(cid)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "complete": self.complete, "note": self.note,
                "counts": dict(self.counts), "cases": dict(self.cases)}


# ---------------------------------------------------------------- pytest

_PY_FAILED = re.compile(r"^FAILED\s+(\S+?)(?:\s+-{1,}\s+.*)?$", re.M)
_PY_ERROR = re.compile(r"^ERROR\s+(\S+?)(?:\s+-{1,}\s+.*)?$", re.M)
_PY_ERROR_PHASE = re.compile(r"^ERROR at (setup|teardown) of\s+(\S+)", re.M)
_PY_PASSED = re.compile(r"^(\S+?::\S+?)\s+PASSED\b", re.M)
_PY_SKIP_LINE = re.compile(r"^(\S+?::\S+?)\s+SKIPPED\b", re.M)
_COUNT = r"(\d+)\s+{word}\b"


def _counts(output: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, word in (("passed", "passed"), ("failed", "failed"), ("skipped", "skipped"),
                      ("errors", "errors?"), ("deselected", "deselected"),
                      ("xfailed", "xfailed"), ("xpassed", "xpassed")):
        m = re.search(_COUNT.format(word=word), output, re.I)
        if m:
            out[key] = int(m.group(1))
    return out


def _parse_pytest(output: str) -> Report:
    counts = _counts(output)
    rep = Report(kind="pytest", counts=counts)
    for cid in _PY_FAILED.findall(output):
        rep.cases[cid] = FAILED
    for cid in _PY_ERROR.findall(output):
        rep.cases[cid] = ERROR
    for phase, cid in _PY_ERROR_PHASE.findall(output):
        rep.cases[cid] = f"{ERROR}:{phase}"          # 阶段保留：setup 失败 ≠ 用例断言失败
    for cid in _PY_PASSED.findall(output):
        rep.cases.setdefault(cid, PASSED)
    for cid in _PY_SKIP_LINE.findall(output):
        rep.cases.setdefault(cid, SKIPPED)

    if not counts:
        rep.note = "没有汇总行（命令可能没跑测试或输出被截断）"
        return rep
    failed_cnt = counts.get("failed", 0)
    err_cnt = counts.get("errors", 0)
    seen_failed = sum(1 for v in rep.cases.values() if v == FAILED)
    seen_err = sum(1 for v in rep.cases.values() if v.startswith(ERROR))
    if seen_failed < failed_cnt or seen_err < err_cnt:
        rep.note = "汇总行与失败的用例数对不上（输出被截断？）"
        return rep
    if seen_failed > failed_cnt or seen_err > err_cnt:
        rep.note = "识别到的失败用例比汇总行还多（输出里有上一次的残留？）"
        return rep
    rep.complete = True
    return rep


# ---------------------------------------------------------------- go

_GO_TEST = re.compile(r"^\s*--- (PASS|FAIL|SKIP):\s+(\S+)", re.M)
_GO_PKG = re.compile(r"^(?:ok|FAIL|---\s*FAIL)\s+(\S+)", re.M)


def _parse_go(output: str) -> Report:
    rep = Report(kind="go", counts=_counts(output))
    hits = _GO_TEST.findall(output)
    if not hits:
        rep.note = "没有逐用例行（go test 不加 -v 只有包级结果）"
        return rep
    # 包汇总行 `ok pkg` / `FAIL pkg` 出现在**该包用例块之后**：正向扫，遇到汇总行就把
    # 当前块里的用例归到它名下（倒着扫会把上一包的行并给下一个包）。
    block: list[tuple[str, str]] = []
    for line in output.splitlines():
        m = _GO_TEST.match(line)
        if m:
            block.append((m.group(2), m.group(1).lower()))
            continue
        pk = _GO_PKG.match(line)
        if pk and block:
            pkg = pk.group(1)
            for name, st in block:
                rep.cases[f"{pkg}::{name}"] = (PASSED if st == "pass"
                                               else (FAILED if st == "fail" else SKIPPED))
            block = []
    if block:
        rep.note = "有用例行但找不到所属包（输出不完整），用例身份不可靠"
        return rep
    rep.complete = True
    return rep


def parse(output: str) -> Report:
    """尽力读逐用例结果。读不出来就返回 complete=False，绝不假装完整。"""
    out = output or ""
    py = _parse_pytest(out)
    if py.cases or py.kind == "pytest" and py.counts:
        return py
    go = _parse_go(out)
    if go.cases or "--- PASS:" in out or "--- FAIL:" in out or re.search(r"^(?:ok|FAIL)\s+\S+", out, re.M):
        return go
    return Report(kind="", note="不是认识的测试输出格式")


# ---------------------------------------------------------------- 进展判定

def progress(before: Report | None, after: Report | None) -> dict:
    """用同一批用例的状态变化判断"这轮修复有没有进展"。

    返回 `{state: improved|regressed|stalled|unknown, why, fixed, new_failures, evidence}`。
    `state=unknown` 时调用方**不得**据此触发停滞换路，也不得据此保留中间成果。
    """
    if not before or not after:
        return {"state": "unknown", "why": "没有可比的判据结果", "fixed": [], "new_failures": []}
    if not (before.complete and after.complete):
        return {"state": "unknown", "why": before.note or after.note or "两次结果有一侧不完整",
                "fixed": [], "new_failures": []}
    # 失败面清零是可确认的进展：after 的失败数是 0，就不存在"没看见的新失败"。
    # （只看"通过数涨了"不行——那可能是少跑；这里用 skip/deselect/xfail 不变 + 失败数为 0 兜底。）
    if (before.failed and not after.failed
            and after.counts.get("failed", 0) + after.counts.get("errors", 0) == 0
            and before.counts.get("skipped", 0) == after.counts.get("skipped", 0)
            and before.counts.get("deselected", 0) == after.counts.get("deselected", 0)
            and before.counts.get("xfailed", 0) == after.counts.get("xfailed", 0)):
        return {"state": "improved", "why": f"原来的 {len(before.failed)} 条失败全部转通过",
                "fixed": sorted(before.failed), "new_failures": []}
    if not before.cases or not after.cases:
        return {"state": "unknown", "why": "没有可比的逐用例结果", "fixed": [], "new_failures": []}

    b_fail, a_fail = before.failed, after.failed
    new_failures = sorted(a_fail - b_fail)
    vanished = sorted(b_fail - a_fail)
    if new_failures:
        return {"state": "regressed", "why": f"出现新的失败用例：{'、'.join(new_failures[:3])}",
                "fixed": [], "new_failures": new_failures}

    # 消失的失败用例：必须能被"失败数减少 + skip/xfail/deselected 不变"解释，
    # 否则可能是被 skip 掉、被 deselected 掉、或者干脆没被收集——那些都不算修好。
    b_skip = before.counts.get("skipped", 0)
    a_skip = after.counts.get("skipped", 0)
    b_desel = before.counts.get("deselected", 0)
    a_desel = after.counts.get("deselected", 0)
    b_xf = before.counts.get("xfailed", 0)
    a_xf = after.counts.get("xfailed", 0)
    if a_skip != b_skip or a_desel != b_desel or a_xf != b_xf:
        return {"state": "unknown",
                "why": "skip/xfail/deselected 数量变了：失败用例可能是被跳过而不是修好",
                "fixed": [], "new_failures": []}

    b_failed_cnt = before.counts.get("failed", 0) + before.counts.get("errors", 0)
    a_failed_cnt = after.counts.get("failed", 0) + after.counts.get("errors", 0)
    if vanished:
        if a_failed_cnt != b_failed_cnt - len(vanished):
            return {"state": "unknown", "why": "失败数变化与消失的用例数量对不上", "fixed": [], "new_failures": []}
        return {"state": "improved", "why": f"{len(vanished)} 条失败用例转通过，且无新增失败",
                "fixed": vanished, "new_failures": []}
    if a_failed_cnt < b_failed_cnt:
        return {"state": "unknown", "why": "失败数变少但没有可识别的用例转通过", "fixed": [], "new_failures": []}
    return {"state": "stalled", "why": "失败集合没有变化", "fixed": [], "new_failures": []}
