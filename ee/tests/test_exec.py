"""D4-9 无界面执行 `ee exec`：纯文本、JSONL 事件、权限与自动审批、-o、stdin、续接、退出码、中断、后台命令清理。全部用假模型。"""
import io
import json
import signal
import sys

from conftest import FakeClient, FakeError, finish, text_chunks, tool_chunks

from ee import headless
from ee.cli import build_parser
from ee.core.providers.openai_chat import ChatClient
from ee.core.store import SessionStore


def run_exec(home, script, argv, stdin=None, delay=0.0, fake=None):
    fake = fake or FakeClient(script, delay=delay)
    args = build_parser().parse_args(["exec", *argv])
    out, err = io.StringIO(), io.StringIO()
    code = headless.run(args, home=home, client_factory=lambda spec: ChatClient(spec, client=fake),
                        stdin=stdin, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue(), fake


def events_of(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def wire_text(call: dict) -> str:
    return json.dumps(call.get("messages"), ensure_ascii=False)


def test_plain_answer_and_session_visible_to_desktop(ee_home, workspace):
    code, out, err, fake = run_exec(ee_home, [text_chunks("答案是 42") + finish("stop")],
                                    ["-C", str(workspace), "生命的意义？"])
    assert code == 0
    assert out == "答案是 42\n"                      # stdout 只有最终回答
    assert "会话" in err and "只读" in err          # 进度在 stderr
    paths = SessionStore.list_sessions(ee_home)
    assert len(paths) == 1
    d = SessionStore.load(paths[0])
    assert d["meta"]["source"] == "exec" and d["meta"]["cwd"] == str(workspace.resolve())
    assert [it["type"] for it in d["items"]][:2] == ["user", "assistant"]
    assert d["items"][-1]["type"] == "turn_end" and d["items"][-1]["reason"] == "completed"
    # 桌面端后端列得出来，并带 source
    import asyncio

    from ee.server.app import Backend
    lst = asyncio.run(Backend(home=ee_home).call("thread/list", {}))["threads"]
    assert lst[0]["source"] == "exec" and lst[0]["title"].startswith("生命的意义")
    # 新会话请求里 ask_user 被关掉：exec 下没有 ask_user 工具
    names = [t["function"]["name"] for t in fake.calls[0].get("tools") or []]
    assert "ask_user" not in names


def test_json_event_sequence_with_tool(ee_home, workspace):
    (workspace / "a.txt").write_text("hello\n", encoding="utf-8")
    script = [
        tool_chunks(0, "c1", "read", {"path": "a.txt"}) + finish("tool_calls"),
        text_chunks("文件内容是 hello") + finish("stop"),
    ]
    code, out, err, _ = run_exec(ee_home, script, ["-C", str(workspace), "--json", "--stream", "读 a.txt"])
    assert code == 0 and err == ""                  # --json 时 stderr 不打进度
    evs = events_of(out)
    types = [e["type"] for e in evs]
    assert types[0] == "session" and types[1] == "turn_start" and types[-1] == "turn_done"
    assert evs[0]["mode"] == "read-only" and evs[0]["resumed"] is False and evs[0]["persist"] is True
    ts = next(e for e in evs if e["type"] == "tool_start")
    te = next(e for e in evs if e["type"] == "tool_end")
    assert ts["name"] == "read" and te["ok"] is True and te["status"] == "ok" and "hello" in te["output"]
    assert types.index("tool_start") < types.index("tool_end") < types.index("turn_done")
    assert "usage" in types and "assistant_delta" in types
    assistants = [e for e in evs if e["type"] == "assistant"]
    assert assistants[-1]["text"] == "文件内容是 hello"
    done = evs[-1]
    assert done["reason"] == "completed" and done["exit_code"] == 0 and done["text"] == "文件内容是 hello"
    assert done["session_id"] == evs[0]["id"]


def test_readonly_denies_write(ee_home, workspace):
    script = [
        tool_chunks(0, "c1", "write", {"path": "x.txt", "content": "hi"}) + finish("tool_calls"),
        text_chunks("只读模式写不了") + finish("stop"),
    ]
    code, out, _, fake = run_exec(ee_home, script, ["-C", str(workspace), "--json", "写个文件"])
    assert code == 0
    evs = events_of(out)
    te = next(e for e in evs if e["type"] == "tool_end")
    assert te["status"] == "denied" and not (workspace / "x.txt").exists()
    assert not any(e["type"] == "approval" for e in evs)      # 只读是直接拒绝，不走审批
    assert "只读" in wire_text(fake.calls[1])


def test_approve_deny_tells_model(ee_home, workspace, tmp_path):
    outside = tmp_path / "outside.txt"
    script = [
        tool_chunks(0, "c1", "write", {"path": str(outside), "content": "hi"}) + finish("tool_calls"),
        text_chunks("需要你手动写") + finish("stop"),
    ]
    code, out, _, fake = run_exec(ee_home, script, ["-C", str(workspace), "--mode", "default", "--json", "写到外面"])
    assert code == 0 and not outside.exists()
    evs = events_of(out)
    ap = next(e for e in evs if e["type"] == "approval")
    assert ap["decision"] == "denied" and "无人值守" in ap["reason"]
    assert next(e for e in evs if e["type"] == "tool_end")["status"] == "denied"
    assert "无人值守" in wire_text(fake.calls[1])               # 模型收到拒绝原因
    # 审批条目照常落盘，状态是 deny
    d = SessionStore.load(SessionStore.list_sessions(ee_home)[0])
    appr = [it for it in d["items"] if it["type"] == "approval"]
    assert appr and appr[0]["status"] == "deny"


def test_approve_auto_but_never_dangerous(ee_home, workspace, tmp_path):
    outside = tmp_path / "outside.txt"
    script = [
        tool_chunks(0, "c1", "write", {"path": str(outside), "content": "hi"}) + finish("tool_calls"),
        tool_chunks(0, "c2", "shell", {"command": "git reset --hard HEAD"}) + finish("tool_calls"),
        text_chunks("好了") + finish("stop"),
    ]
    code, out, err, fake = run_exec(ee_home, script, ["-C", str(workspace), "--mode", "default", "--approve", "auto",
                                                      "写到外面再硬重置"])
    assert code == 0 and out == "好了\n"
    assert outside.read_text(encoding="utf-8") == "hi"
    assert "自动批准" in err and "自动拒绝" in err
    assert "危险操作" in wire_text(fake.calls[2])


def test_output_last_message_and_stdin(ee_home, workspace, tmp_path):
    target = tmp_path / "sub" / "last.md"
    script = [lambda params: text_chunks("收到：" + params["messages"][-1]["content"].split("\n\n")[0]) + finish("stop")]
    code, out, _, _ = run_exec(ee_home, script, ["-C", str(workspace), "-q", "-o", str(target), "-"],
                               stdin=io.StringIO("从 stdin 来的任务\n"))
    assert code == 0 and out == "收到：从 stdin 来的任务\n"
    assert target.read_bytes().decode("utf-8") == "收到：从 stdin 来的任务"
    # 省略提示词 + 字节流带 BOM（PowerShell 管道常见）
    raw = io.TextIOWrapper(io.BytesIO("\ufeff带 BOM 的中文".encode("utf-8")), encoding="gbk")
    code, out, _, fake = run_exec(ee_home, [text_chunks("ok") + finish("stop")], ["-C", str(workspace), "-q"],
                                  stdin=raw)
    assert code == 0
    assert "带 BOM 的中文" in wire_text(fake.calls[0]) and "\\ufeff" not in wire_text(fake.calls[0])
    # stdin 是空的 → 参数错误
    code, out, err, _ = run_exec(ee_home, [], ["-C", str(workspace), "-"], stdin=io.StringIO("  \n"))
    assert code == 2 and out == "" and "提示词" in err


def test_resume_and_last(ee_home, workspace):
    code, out, _, _ = run_exec(ee_home, [text_chunks("记住了：蓝色") + finish("stop")],
                               ["-C", str(workspace), "--json", "我喜欢蓝色，记住"])
    sid = events_of(out)[0]["id"]
    code, out, _, fake = run_exec(ee_home, [text_chunks("你喜欢蓝色") + finish("stop")],
                                  ["--resume", sid, "--json", "我喜欢什么颜色？"])
    assert code == 0
    evs = events_of(out)
    assert evs[0]["id"] == sid and evs[0]["resumed"] is True
    assert "我喜欢蓝色，记住" in wire_text(fake.calls[0]) and "记住了：蓝色" in wire_text(fake.calls[0])
    # --last：按目录找最近的会话
    code, out, _, fake = run_exec(ee_home, [text_chunks("还是蓝色") + finish("stop")],
                                  ["-C", str(workspace), "--last", "-q", "再说一次"])
    assert code == 0 and out == "还是蓝色\n"
    assert "我喜欢什么颜色？" in wire_text(fake.calls[0])
    assert len(SessionStore.list_sessions(ee_home)) == 1
    users = [it for it in SessionStore.load(SessionStore.list_sessions(ee_home)[0])["items"] if it["type"] == "user"]
    assert len(users) == 3
    # 不存在 / 不合法的会话 id、没有会话的目录
    assert run_exec(ee_home, [], ["--resume", "nope", "x"])[0] == 1
    assert run_exec(ee_home, [], ["--resume", "../evil", "x"])[0] == 1
    other = workspace / "other"
    other.mkdir()
    code, _, err, _ = run_exec(ee_home, [], ["-C", str(other), "--last", "x"])
    assert code == 1 and "没有可续接" in err


def test_no_persist(ee_home, workspace):
    code, out, _, _ = run_exec(ee_home, [text_chunks("ok") + finish("stop")],
                               ["-C", str(workspace), "--no-persist", "--json", "hi"])
    assert code == 0 and events_of(out)[0]["persist"] is False
    assert SessionStore.list_sessions(ee_home) == []


def test_exit_codes(ee_home, workspace):
    # 步数上限 → 3
    script = [tool_chunks(0, "c1", "glob", {"pattern": "*"}) + finish("tool_calls")]
    code, out, _, _ = run_exec(ee_home, script, ["-C", str(workspace), "--max-steps", "1", "--json", "列文件"])
    assert code == 3 and events_of(out)[-1]["reason"] == "max_steps"
    # 模型出错 → 1，error 事件
    code, out, _, _ = run_exec(ee_home, [FakeError(401, "bad key")], ["-C", str(workspace), "--json", "hi"])
    evs = events_of(out)
    assert code == 1 and evs[-1]["reason"] == "error" and any(e["type"] == "error" for e in evs)
    # 目录不存在 → 1
    assert run_exec(ee_home, [], ["-C", str(workspace / "nope"), "hi"])[0] == 1


def test_ctrl_c_interrupts_gracefully(ee_home, workspace):
    def press_ctrl_c(params):
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler) and getattr(handler, "__module__", "") == "ee.headless"   # exec 装了自己的处理器
        handler(signal.SIGINT, None)
        return text_chunks("这段很长的回答会被打断" * 20, n=2) + finish("stop")

    before = signal.getsignal(signal.SIGINT)
    code, out, _, _ = run_exec(ee_home, [press_ctrl_c], ["-C", str(workspace), "--json", "hi"], delay=0.01)
    assert code == 130
    assert events_of(out)[-1]["reason"] == "interrupted"
    assert signal.getsignal(signal.SIGINT) is before                               # 结束后恢复
    d = SessionStore.load(SessionStore.list_sessions(ee_home)[0])
    assert d["items"][-1]["reason"] == "interrupted"


def test_background_commands_killed_at_exit(ee_home, workspace):
    from ee.config import load_config
    from ee.core import bgproc
    from ee.tools.shellenv import resolve_shell
    sh = resolve_shell(load_config(workspace).data.get("shell", "auto"))
    exe = sys.executable
    cmd = (f"& '{exe}' -c 'import time; time.sleep(60)'" if sh.syntax == "powershell"
           else f'"{exe}" -c "import time; time.sleep(60)"')
    script = [
        tool_chunks(0, "c1", "shell", {"command": cmd, "background": True}) + finish("tool_calls"),
        text_chunks("已在后台启动") + finish("stop"),
    ]
    code, out, _, _ = run_exec(ee_home, script, ["-C", str(workspace), "--mode", "auto", "--json", "后台跑"])
    assert code == 0
    te = next(e for e in events_of(out) if e["type"] == "tool_end")
    assert te["ok"], te
    procs = [p for m in list(bgproc._ALL) for p in m.procs.values() if p.command == cmd]
    assert procs and all(not p.running for p in procs)
