"""前端 rpc.call("方法", {参数}) 与后端处理函数签名对齐：方法要存在、参数名要被接受、必填参数不能漏。"""
import inspect
import re
from pathlib import Path

import pytest

from ee.server.app import Backend

SRC = Path(__file__).resolve().parents[2] / "desktop" / "src"
CALL = re.compile(r'\.call(?:<[^>]*>)?\(\s*"([a-z_]+/[a-z_]+)"\s*(?:,\s*\{([^{}]*)\})?', re.S)


def _keys(body: str) -> list[str]:
    out = []
    for part in re.split(r",(?![^()\[\]]*[)\]])", body):
        part = part.strip()
        if part.startswith("..."):
            out.append("...")
            continue
        m = re.match(r"([A-Za-z_]\w*)", part)
        if m:
            out.append(m.group(1))
    return out


def _calls():
    for f in sorted(SRC.rglob("*.ts*")):
        if ".test." in f.name:
            continue
        text = f.read_text(encoding="utf-8")
        for m in CALL.finditer(text):
            yield f"{f.relative_to(SRC)}:{text[:m.start()].count(chr(10)) + 1}", m.group(1), m.group(2)


@pytest.mark.skipif(not SRC.is_dir(), reason="没有前端源码")
def test_frontend_rpc_calls_match_backend_signatures(tmp_path):
    b = Backend(home=tmp_path, persist=False)
    problems, seen = [], 0
    for where, method, body in _calls():
        seen += 1
        fn = b.methods.get(method)
        if fn is None:
            problems.append(f"{where} 未知方法 {method}")
            continue
        if body is None:
            continue
        try:
            ps = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            continue
        if any(p.kind == p.VAR_KEYWORD for p in ps.values()):
            continue
        keys = _keys(body)
        extra = [k for k in keys if k != "..." and k not in ps]
        missing = [] if "..." in keys else [n for n, p in ps.items() if p.default is p.empty and n not in keys]
        if extra or missing:
            problems.append(f"{where} {method} 多余={extra} 缺少={missing}")
    assert seen > 50
    assert not problems, "\n".join(problems)
