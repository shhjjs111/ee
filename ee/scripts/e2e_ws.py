"""端到端排查脚本：拉起 ee 后端 + 假模型服务器，模拟桌面端发“你好”，打印全部消息。

用法：python ee/scripts/e2e_ws.py [--blackhole] <后端命令...>
  --blackhole：模型地址指向“接受连接但永不回应”的端点，断言回合在超时内以错误结束（界面能看到错误）。
例：  python ee/scripts/e2e_ws.py python -m ee serve --exit-on-stdin-eof
      python ee/scripts/e2e_ws.py desktop/resources/backend/ee-backend/ee-backend.exe serve --exit-on-stdin-eof
"""
import asyncio, json, os, pathlib, secrets, socket, subprocess, sys, tempfile, threading, time

import websockets

ROOT = pathlib.Path(__file__).resolve().parents[2]
T0 = time.time()


def log(*a):
    print(f"[{time.time()-T0:6.2f}]", *a, flush=True)


def blackhole():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    keep = []
    def loop():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            keep.append(c)
    threading.Thread(target=loop, daemon=True).start()
    return srv, srv.getsockname()[1]


async def main(cmd, hole=False):
    home = pathlib.Path(tempfile.mkdtemp(prefix="eehome"))
    logdir = home / "logs"
    ws_dir = pathlib.Path(tempfile.mkdtemp()) / "ee日常 测试"
    ws_dir.mkdir()
    (ws_dir / "a.txt").write_text("hi", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=ws_dir)
    fake = subprocess.Popen([sys.executable, str(ROOT / "desktop/scripts/fake_model_server.py"), "18080"])
    token = secrets.token_hex(8)
    env = {**os.environ, "EE_SERVE_TOKEN": token, "HOME": str(home), "USERPROFILE": str(home),
           "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "EE_HOME": str(home / ".ee"),
           "EE_LOG_DIR": str(logdir)}
    env.pop("DEEPSEEK_API_KEY", None)
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    port = None
    def pump(stream, name):
        nonlocal port
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            log(f"backend {name}:", line)
            if "EE_SERVE_READY" in line:
                port = json.loads(line.split("EE_SERVE_READY", 1)[1])["port"]
    threading.Thread(target=pump, args=(p.stdout, "out"), daemon=True).start()
    threading.Thread(target=pump, args=(p.stderr, "err"), daemon=True).start()
    for _ in range(300):
        if port: break
        await asyncio.sleep(0.1)
    assert port, "后端没启动"
    ok = True
    async with websockets.connect(f"ws://127.0.0.1:{port}/?token={token}", max_size=None, proxy=None) as ws:
        seq = 0; pending = {}; notes = []
        async def reader():
            async for m in ws:
                msg = json.loads(m)
                log("<-", json.dumps(msg, ensure_ascii=False)[:400])
                if "id" in msg and msg["id"] in pending:
                    pending.pop(msg["id"]).set_result(msg)
                elif "method" in msg:
                    notes.append(msg)
        rt = asyncio.create_task(reader())
        async def rpc(method, params=None, timeout=15):
            nonlocal seq
            seq += 1
            fut = asyncio.get_running_loop().create_future(); pending[seq] = fut
            log("->", method)
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": seq, "method": method, "params": params or {}}))
            try:
                r = await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                log(f"!!! {method} 超时 {timeout}s 无响应"); raise
            if "error" in r: log("!!! error", r["error"])
            return r.get("result")
        try:
            await rpc("app/info")
            s = (await rpc("settings/get"))["settings"]
            if hole:
                hsrv, hport = blackhole()
                s["providers"]["fake"] = {"base_url": f"http://127.0.0.1:{hport}/v1", "api_key": "sk-fake"}
                s["network"] = {"proxy_mode": "system", "connect_timeout": 5, "read_timeout": 5, "retries": 1}
            else:
                s["providers"]["fake"] = {"base_url": "http://127.0.0.1:18080/v1", "api_key": "sk-fake"}
            s["models"]["fake-gpt"] = {"provider": "fake", "model": "fake-gpt", "family": "default"}
            s["default_model"] = "fake-gpt"
            await rpc("settings/set", {"settings": s})
            await rpc("workspace/open", {"path": str(ws_dir)})
            th = await rpc("thread/start", {"cwd": str(ws_dir)})
            tid = th["thread"]["id"]
            t_turn = time.time()
            await rpc("turn/start", {"thread_id": tid, "text": "你好"})
            await rpc("settings/get", timeout=5)      # 回合进行中，其他 RPC 必须照常秒回
            for _ in range(600):
                if any(n["method"] == "turn/completed" for n in notes): break
                if any(n["method"] == "item/started" and n["params"]["item"].get("type") == "approval" for n in notes):
                    a = [n for n in notes if n["method"] == "item/started" and n["params"]["item"].get("type") == "approval"][-1]
                    notes.remove(a)
                    await rpc("approval/respond", {"thread_id": tid, "call_id": a["params"]["item"].get("call_id"), "choice": "once"})
                await asyncio.sleep(0.1)
            else:
                log("!!! 60s 内没有 turn/completed"); ok = False
            done = [n for n in notes if n["method"] == "turn/completed"]
            errs = [n["params"]["item"]["message"] for n in notes
                    if n["method"] == "item/started" and n["params"]["item"].get("type") == "error"]
            stat = [n["params"]["text"] for n in notes if n["method"] == "turn/status"]
            if not stat or "正在" not in stat[0]:
                log("!!! 没有收到“正在连接模型…”状态"); ok = False
            if hole:
                took = time.time() - t_turn
                if not done or done[0]["params"]["reason"] != "error" or not errs or "超时" not in errs[0]:
                    log("!!! 黑洞端点：期望 reason=error 且有含“超时”的错误条目", done[:1], errs[:1]); ok = False
                elif took > 45:
                    log(f"!!! 黑洞端点：{took:.0f}s 才结束，太慢"); ok = False
                else:
                    log(f"黑洞端点：{took:.1f}s 后报错：{errs[0]}")
            elif done and done[0]["params"]["reason"] != "completed":
                log("!!! 回合没有正常完成", done[0], errs); ok = False
            await rpc("settings/get")
            await rpc("thread/get", {"thread_id": tid})
        except Exception as e:
            log("!!! 失败", repr(e)); ok = False
        rt.cancel()
    p.stdin.close()
    try: p.wait(10)
    except Exception: p.kill()
    fake.kill()
    lf = logdir / "ee-backend.log"
    if lf.is_file():
        text = lf.read_text(encoding="utf-8", errors="replace")
        log(f"后端日志 {lf}（{len(text)} 字节），末尾：\n" + "\n".join(text.splitlines()[-25:]))
        if "回合开始" not in text or "模型请求开始" not in text:
            log("!!! 后端日志里没有回合/模型请求记录"); ok = False
    else:
        log("!!! 没有生成后端日志", lf); ok = False
    log("结果:", "通过" if ok else "失败")
    sys.exit(0 if ok else 1)


args = sys.argv[1:]
hole = bool(args) and args[0] == "--blackhole"
asyncio.run(main(args[1:] if hole else args, hole))
