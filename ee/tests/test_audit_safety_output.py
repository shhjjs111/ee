"""输出读取上限（Hook stdout、read 大文件、shell 输出）与 GBK/cp936 解码回退。"""
import asyncio
import sys

import pytest
from conftest import run

from ee.core import hooks as H
from ee.core.bgproc import BgManager
from ee.tools import ToolContext, ToolError, ToolState
from ee.tools import fileops, read as R, shell as S, textdecode as TD
from ee.tools.shellenv import resolve_shell

GBK_TEXT = "中文输出：编译成功"


@pytest.fixture
def cp936(monkeypatch):
    monkeypatch.setattr(TD, "fallback_encoding", lambda: "cp936")


# ---------------------------------------------------------------- 解码
def test_smart_decode():
    assert TD.smart_decode("你好".encode("utf-8")) == "你好"
    assert TD.smart_decode(GBK_TEXT.encode("gbk"), "cp936") == GBK_TEXT
    assert TD.smart_decode("ab你".encode("utf-8")[:-1], "cp936") == "ab\ufffd"   # 尾部被截断的 UTF-8 不改用 GBK
    assert TD.smart_decode(b"") == ""


def test_stream_decoder_utf8_split_and_gbk_switch():
    d = TD.StreamDecoder("cp936")
    b = "中文".encode("utf-8")
    assert d.decode(b[:1]) + d.decode(b[1:4]) + d.decode(b[4:]) + d.decode(b"", final=True) == "中文"
    d = TD.StreamDecoder("cp936")
    g = ("ok " + GBK_TEXT).encode("gbk")
    out = "".join(d.decode(g[i:i + 3]) for i in range(0, len(g), 3)) + d.decode(b"", final=True)
    assert out == "ok " + GBK_TEXT


def test_fallback_encoding_never_utf8():
    assert TD.fallback_encoding().replace("-", "").lower() not in ("utf8", "ascii")


# ---------------------------------------------------------------- shell
def _py(sh, code: str) -> str:
    exe = sys.executable
    if sh.syntax == "powershell":
        return f"& '{exe}' -c \"{code}\""
    return f'"{exe}" -c "{code}"'


def test_shell_gbk_output(workspace, cp936):
    sh = resolve_shell("auto")
    code = "import sys; sys.stdout.buffer.write('" + GBK_TEXT + "'.encode('gbk')); sys.stdout.flush()"
    rc, out, _ = run(S.run_command(_py(sh, code), sh, workspace, 60))
    assert GBK_TEXT in out and "\ufffd" not in out


def test_shell_capture_capped(workspace, monkeypatch):
    monkeypatch.setattr(S, "CAPTURE_CHARS", 20_000)
    sh = resolve_shell("auto")
    code = "import sys; [sys.stdout.write('L%07d\\n' % i) for i in range(200000)]"
    rc, out, _ = run(S.run_command(_py(sh, code), sh, workspace, 60))
    assert rc == 0
    assert len(out) < 60_000
    assert "L0000000" in out and "L0199999" in out and "省略" in out


# ---------------------------------------------------------------- 后台命令
def test_bgproc_gbk_output(workspace, tmp_path, cp936):
    sh = resolve_shell("auto")
    mgr = BgManager(tmp_path / "logs")
    code = "import sys; sys.stdout.buffer.write('" + GBK_TEXT + "'.encode('gbk')); sys.stdout.flush()"

    async def main():
        p = await mgr.start(_py(sh, code), sh, workspace, S.child_env())
        for _ in range(100):
            if not p.running:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.1)
        text, _ = mgr.read_new(p)
        await mgr.close()
        return text

    assert GBK_TEXT in run(main())


# ---------------------------------------------------------------- Hook
def test_hook_stdout_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "MAX_HOOK_OUTPUT", 10_000)
    code = "import sys; sys.stdout.write('x' * 3000000); sys.stderr.write('e' * 3000000)"
    hook = H.HookDef(event="PreToolUse", command=[sys.executable, "-c", code], timeout=60)
    r = run(H.run_hook(hook, {}, tmp_path))
    assert r.code == 0 and not r.timed_out
    assert len(r.stdout) < 12_000 and len(r.stderr) < 12_000 and "截断" in r.stdout


def test_hook_gbk_stdout(tmp_path, cp936):
    code = "import sys; sys.stdout.buffer.write('" + GBK_TEXT + "'.encode('gbk'))"
    hook = H.HookDef(event="UserPromptSubmit", command=[sys.executable, "-c", code], timeout=60)
    r = run(H.run_hook(hook, {}, tmp_path))
    assert r.stdout == GBK_TEXT


# ---------------------------------------------------------------- read 大文件
def _ctx(ws):
    return ToolContext(cwd=ws, state=ToolState(), output_dir=ws / ".out")


def test_read_large_file_streams_window(workspace, monkeypatch):
    monkeypatch.setattr(R, "STREAM_BYTES", 1000)
    lines = [f"第{i}行" for i in range(1, 5001)]
    lines[4995] = "长" * 300_000
    (workspace / "big.log").write_bytes(("\r\n".join(lines) + "\r\n").encode("gbk"))
    monkeypatch.setattr(R, "read_text", lambda p: (_ for _ in ()).throw(AssertionError("不应整体读入")))
    res = run(R.ReadTool().run({"path": "big.log", "offset": 4990, "limit": 8}, _ctx(workspace)))
    out = res.output
    assert "第4990行" in out and "第4997行" in out and "第4998行" not in out
    assert "本行过长" in out and "共 5000 行" in out and "offset=4998" in out
    assert len(out) < 20_000


def test_read_large_file_last_line_without_newline(workspace, monkeypatch):
    monkeypatch.setattr(R, "STREAM_BYTES", 10)
    (workspace / "a.txt").write_text("一\n二\n三", encoding="utf-8")
    res = run(R.ReadTool().run({"path": "a.txt"}, _ctx(workspace)))
    assert "1\t一" in res.output and "3\t三" in res.output


def test_read_text_size_cap(workspace, monkeypatch):
    monkeypatch.setattr(fileops, "MAX_TEXT_BYTES", 1000)
    p = workspace / "huge.txt"
    p.write_text("x" * 5000)
    with pytest.raises(ToolError, match="太大"):
        fileops.read_text(p)
