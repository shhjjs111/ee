"""D5-8 命令沙箱：配置、降级提示、禁网环境变量（各平台）；Windows 上的真实写入隔离（仅 win32）。"""
import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from ee.config import DEFAULTS
from ee.core import sandbox as SB
from ee.core.bgproc import BgManager
from ee.tools import ToolContext, ToolState
from ee.tools.bgshell import ShellOutputTool
from ee.tools.shell import ShellTool
from ee.tools.shellenv import resolve_shell

WIN = sys.platform == "win32"
win_only = pytest.mark.skipif(not WIN, reason="仅 Windows")


def _cfg(enabled=True, network=True):
    return SimpleNamespace(data={"sandbox": {"enabled": enabled, "network": network}})


def _ctx(ws, sh, cfg, mgr=None):
    extra = {"config": cfg}
    if mgr is not None:
        extra["session"] = SimpleNamespace(bg=mgr)
    return ToolContext(cwd=ws, state=ToolState(), output_dir=ws / ".out", shell=sh, extra=extra)


def _py(sh, script: str) -> str:
    exe = sys.executable
    if sh.syntax == "powershell":
        return f"& '{exe}' -u {script}"
    if sh.syntax == "cmd" and " " not in exe:
        # cmd 的 /s /c 参数经 list2cmdline 后内部引号会变成 \"，cmd 不认（与沙箱无关的既有行为），这里不加引号
        return f"{exe} -u {script}"
    return f'"{exe}" -u {script}'


def _run(ws, script_name, body, cfg, sh=None):
    (ws / script_name).write_text(body, encoding="utf-8")
    sh = sh or resolve_shell("auto")
    return asyncio.run(ShellTool().run({"command": _py(sh, script_name), "timeout": 60}, _ctx(ws, sh, cfg)))


# ------------------------------------------------------------------ 各平台

def test_defaults_and_settings_parse():
    assert DEFAULTS["sandbox"] == {"enabled": False, "network": True}
    assert SB.settings({}) == {"enabled": False, "network": True}
    assert SB.settings({"sandbox": {"enabled": 1, "network": 0}}) == {"enabled": True, "network": False}
    assert SB.settings({"sandbox": "bad"}) == {"enabled": False, "network": True}


def test_disabled_plan_is_none(workspace):
    assert SB.plan({"sandbox": {"enabled": False, "network": False}}, workspace) is None
    assert SB.plan({}, workspace) is None


def test_network_env_vars():
    env = SB.network_env({"PATH": "x", "NO_PROXY": "corp"})
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        assert env[k] == SB.DEAD_PROXY
    assert env["PIP_NO_INDEX"] == "1" and env["NPM_CONFIG_OFFLINE"] == "true"
    assert env["CARGO_NET_OFFLINE"] == "true" and env["GIT_SSH_COMMAND"] == "false"
    assert "127.0.0.1" in env["NO_PROXY"] and env["PATH"] == "x"


def test_plan_env_strips_secret_and_applies_network(workspace, ee_home):
    p = SB.SandboxPlan(workspace=workspace, active=False, network=False)
    argv, env = p.wrap(["a", "b"], {"EE_SERVE_TOKEN": "s3cret", "PATH": "x"})
    assert argv == ["a", "b"]
    assert "EE_SERVE_TOKEN" not in env and env["HTTP_PROXY"] == SB.DEAD_PROXY
    p2 = SB.SandboxPlan(workspace=workspace, active=False, network=True)
    _, env2 = p2.wrap(["a"], {"PATH": "x"})
    assert "HTTP_PROXY" not in env2


def test_active_plan_wraps_with_helper(workspace, ee_home, tmp_path):
    p = SB.SandboxPlan(workspace=workspace, active=True, network=True, sid="S-1-5-21-1-2-3-4", tmp=tmp_path)
    argv, env = p.wrap(["cmd.exe", "/c", 'echo "hi"'], {})
    i = argv.index(SB.HELPER_CMD)
    assert argv[i + 1:i + 4] == ["--sid", "S-1-5-21-1-2-3-4", "--cmdline"]
    import base64
    import subprocess
    assert base64.b64decode(argv[i + 4]).decode() == subprocess.list2cmdline(["cmd.exe", "/c", 'echo "hi"'])
    assert env["TEMP"] == str(tmp_path) and env["TMP"] == str(tmp_path)


def test_capability_sid_persisted_per_workspace(ee_home, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    sa = SB.capability_sid(a)
    assert sa.startswith("S-1-5-21-") and len(sa.split("-")) == 8
    assert SB.capability_sid(a) == sa
    assert SB.capability_sid(b) != sa
    data = json.loads((ee_home / "sandbox" / "sandbox.json").read_text(encoding="utf-8"))
    assert data["workspaces"][SB.workspace_id(a)]["sid"] == sa
    assert SB.tmp_dir(a) == ee_home / "sandbox" / "tmp" / SB.workspace_id(a)


def test_helper_bad_args():
    assert SB.helper_main([]) == SB.FAIL_CODE
    assert SB.helper_main(["--sid", "S-1-5-21-1-2-3-4", "--"]) == SB.FAIL_CODE
    assert SB.helper_main(["--sid", "S-1-5-21-1-2-3-4", "--cmdline", "!!"]) == SB.FAIL_CODE
    assert SB.is_helper_failure(SB.FAIL_CODE, f"{SB.FAIL_MARK} x")
    assert not SB.is_helper_failure(0, f"{SB.FAIL_MARK} x")


def test_cli_routes_helper(monkeypatch):
    from ee import cli
    seen = {}
    monkeypatch.setattr(SB, "helper_main", lambda a: seen.setdefault("a", a) and 7)
    assert cli.main(["__sandbox_exec", "--sid", "S", "--", "x"]) == 7
    assert seen["a"] == ["--sid", "S", "--", "x"]


@pytest.mark.skipif(WIN, reason="非 Windows 的降级行为")
def test_non_windows_reports_unsupported(workspace, ee_home):
    p = SB.plan({"sandbox": {"enabled": True}}, workspace)
    assert p is not None and not p.active and p.notice == SB.NOTICE_UNSUPPORTED
    r = _run(workspace, "w.py", "open('x.txt','w').write('1'); print('done')", _cfg(True))
    assert r.ok and "done" in r.output and "未生效" in r.output
    assert (workspace / "x.txt").exists()       # 没有隔离——提示里写明了


@pytest.mark.skipif(WIN, reason="非 Windows")
def test_non_windows_network_env_reaches_command(workspace, ee_home):
    body = "import os; print('P=' + os.environ.get('HTTPS_PROXY','')); print('I=' + os.environ.get('PIP_NO_INDEX',''))"
    r = _run(workspace, "n.py", body, _cfg(True, network=False))
    assert f"P={SB.DEAD_PROXY}" in r.output and "I=1" in r.output


def test_disabled_shell_has_no_notice(workspace, ee_home):
    r = _run(workspace, "d.py", "print('ok')", _cfg(False))
    assert r.ok and "沙箱" not in r.output


def test_settings_roundtrip(ee_home):
    from ee.server import settings as S
    out = S.get_settings()["settings"]["sandbox"]
    assert out == {"enabled": False, "network": True, "supported": WIN}
    st = S.get_settings()["settings"]
    st["sandbox"] = {"enabled": True, "network": False}
    res = S.set_settings(st)["settings"]["sandbox"]
    assert res["enabled"] is True and res["network"] is False
    text = (ee_home / "config.toml").read_text(encoding="utf-8")
    assert "[sandbox]" in text and "enabled = true" in text and "network = false" in text


def test_prepare_failure_is_tool_error(workspace, ee_home, monkeypatch):
    def boom(data, ws):
        raise SB.SandboxError(f"{SB.FAIL_MARK} 准备沙箱失败：测试")
    monkeypatch.setattr(SB, "plan", boom)
    sh = resolve_shell("auto")
    from ee.tools.base import ToolError
    with pytest.raises(ToolError, match="沙箱"):
        asyncio.run(ShellTool().run({"command": "echo hi"}, _ctx(workspace, sh, _cfg(True))))


# ------------------------------------------------------------------ Windows 真实隔离
#
# 不用 pytest 的 tmp_path：Python 3.12.4+ 在 Windows 上按 0o700 建临时目录时会写一份只给
# SYSTEM/Administrators/所有者 的 ACL，CI 的管理员令牌下所有者是 Administrators 组；沙箱令牌
# （LUA_TOKEN 把 Administrators 变成 deny-only）连读都读不了，和真实工作区（继承用户目录 ACL）不一样。
# 所以 Windows 用例在用户目录下自建普通目录。


@pytest.fixture
def wdirs(monkeypatch):
    import os
    import shutil
    import uuid
    from pathlib import Path
    root = Path(os.environ["USERPROFILE"]) / "ee-sbx-tests" / uuid.uuid4().hex[:10]
    ws, home, outside = root / "ws", root / "home", root / "outside"
    for d in (ws, home, outside):
        os.makedirs(d)
    monkeypatch.setenv("EE_HOME", str(home))
    yield SimpleNamespace(root=root, ws=ws, home=home, outside=outside)
    shutil.rmtree(root, ignore_errors=True)

WRITE = """
import sys
p = sys.argv[1] if len(sys.argv) > 1 else {target!r}
try:
    with open(p, 'w') as f:
        f.write('x')
    print('WROTE')
except OSError as e:
    print('DENIED', type(e).__name__, e)
"""


@win_only
@pytest.mark.parametrize("shell_name", ["cmd", "auto"])
def test_win_cwd_preserved(wdirs, shell_name):
    workspace, tmp_path = wdirs.ws, wdirs.root
    sh = resolve_shell(shell_name)
    r = _run(workspace, "c.py", "import os; print('CWD=' + os.getcwd())", _cfg(True), sh=sh)
    assert f"CWD={workspace}".lower() in r.output.lower() or f"CWD={workspace.resolve()}".lower() in r.output.lower(), \
        f"{sh.display}: {r.output}"


@win_only
def test_win_write_inside_workspace_ok(wdirs):
    workspace, tmp_path = wdirs.ws, wdirs.root
    target = str(workspace / "inside.txt")
    r = _run(workspace, "w.py", WRITE.format(target=target), _cfg(True))
    assert "WROTE" in r.output, r.output
    assert (workspace / "inside.txt").exists()
    sub = workspace / "deep" / "er"
    sub.mkdir(parents=True)
    r = _run(workspace, "w2.py", WRITE.format(target=str(sub / "f.txt")), _cfg(True))
    assert "WROTE" in r.output, r.output


@win_only
def test_win_write_outside_workspace_denied(wdirs):
    workspace, tmp_path = wdirs.ws, wdirs.root
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    target = outside / "nope.txt"
    r = _run(workspace, "w.py", WRITE.format(target=str(target)), _cfg(True))
    assert "DENIED" in r.output, r.output
    assert not target.exists()
    # 对照：不开沙箱时能写
    r = _run(workspace, "w.py", WRITE.format(target=str(target)), _cfg(False))
    assert "WROTE" in r.output and target.exists()


@win_only
def test_win_temp_points_to_sandbox_tmp(wdirs):
    workspace, tmp_path = wdirs.ws, wdirs.root
    body = ("import os, tempfile; t = os.environ['TEMP']; print('T=' + t);"
            "f = tempfile.NamedTemporaryFile(delete=False); f.write(b'1'); f.close(); print('TMPOK')")
    r = _run(workspace, "t.py", body, _cfg(True))
    assert f"T={SB.tmp_dir(workspace.resolve())}" in r.output, r.output
    assert "TMPOK" in r.output, r.output


@win_only
def test_win_network_env_blocks_proxy_aware_clients(wdirs):
    workspace, tmp_path = wdirs.ws, wdirs.root
    body = """
import os, urllib.request, urllib.error
print('P=' + os.environ.get('HTTPS_PROXY', ''))
print('I=' + os.environ.get('PIP_NO_INDEX', ''))
try:
    urllib.request.urlopen('http://example.com/', timeout=10)
    print('NET_OPEN')
except Exception as e:
    print('NET_BLOCKED', type(e).__name__)
"""
    r = _run(workspace, "n.py", body, _cfg(True, network=False))
    assert f"P={SB.DEAD_PROXY}" in r.output and "I=1" in r.output, r.output
    assert "NET_BLOCKED" in r.output, r.output


@win_only
def test_win_child_process_inherits_restriction(wdirs):
    workspace, tmp_path = wdirs.ws, wdirs.root
    outside = tmp_path / "outside2"
    outside.mkdir(exist_ok=True)
    target = outside / "child.txt"
    (workspace / "w.py").write_text(WRITE.format(target=str(target)), encoding="utf-8")
    body = f"""
import subprocess, sys
r = subprocess.run([sys.executable, {str(workspace / 'w.py')!r}, {str(target)!r}], capture_output=True, text=True)
print('CHILD:' + r.stdout.strip())
r = subprocess.run([sys.executable, {str(workspace / 'w.py')!r}, {str(workspace / 'child_in.txt')!r}],
                   capture_output=True, text=True)
print('CHILDIN:' + r.stdout.strip())
"""
    r = _run(workspace, "parent.py", body, _cfg(True))
    assert "CHILD:DENIED" in r.output, r.output
    assert "CHILDIN:WROTE" in r.output, r.output
    assert not target.exists()


@win_only
def test_win_background_command_sandboxed(wdirs):
    workspace, tmp_path = wdirs.ws, wdirs.root
    outside = tmp_path / "outside3"
    outside.mkdir(exist_ok=True)
    target = outside / "bg.txt"
    (workspace / "w.py").write_text(WRITE.format(target=str(target)), encoding="utf-8")
    sh = resolve_shell("auto")

    async def go():
        mgr = BgManager(tmp_path / "logs", owner="t")
        ctx = _ctx(workspace, sh, _cfg(True), mgr)
        r = await ShellTool().run({"command": _py(sh, "w.py"), "background": True}, ctx)
        out = r.output
        if r.meta.get("running"):
            out += (await ShellOutputTool().run({"id": r.meta["bg_id"], "wait": 20}, ctx)).output
        await mgr.close()
        return out

    out = asyncio.run(go())
    assert "DENIED" in out, out
    assert not target.exists()


@win_only
def test_win_helper_failure_reported(wdirs, monkeypatch):
    workspace, tmp_path = wdirs.ws, wdirs.root
    p = SB.plan({"sandbox": {"enabled": True}}, workspace)
    assert p is not None and p.active
    p.sid = "not-a-sid"
    monkeypatch.setattr(SB, "plan", lambda data, ws: p)
    sh = resolve_shell("auto")
    from ee.tools.base import ToolError
    with pytest.raises(ToolError, match="沙箱启动失败"):
        asyncio.run(ShellTool().run({"command": "echo hi"}, _ctx(workspace, sh, _cfg(True))))


def test_project_layer_cannot_disable_sandbox_or_enable_network():
    from ee.config import sanitize_project_layer
    user = {"sandbox": {"enabled": True, "network": False}}
    clean, dropped = sanitize_project_layer({"sandbox": {"enabled": False, "network": True, "x": 1}}, user)
    assert clean.get("sandbox", {}) == {}
    assert set(dropped) >= {"sandbox.enabled", "sandbox.network", "sandbox.x"}
    clean, dropped = sanitize_project_layer({"sandbox": {"enabled": True, "network": False}}, {})
    assert clean["sandbox"] == {"enabled": True, "network": False} and not dropped
