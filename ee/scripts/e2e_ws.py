"""端到端排查脚本：拉起 ee 后端 + 假模型服务器，模拟桌面端发“你好”，打印全部消息。

用法（**必须在 ee/ 目录内执行**，见下方说明）：
  cd ee
  ../.venv/Scripts/python.exe scripts/e2e_ws.py ../.venv/Scripts/python.exe -m ee serve --exit-on-stdin-eof
  ../.venv/Scripts/python.exe scripts/e2e_ws.py --blackhole <后端命令...>

  --blackhole：模型地址指向“接受连接但永不回应”的端点，断言回合在超时内以错误结束。

为什么必须在 ee/ 里跑：`python -m ee` 要求 `ee` 是一个**常规包**。在仓库根执行时，
`ee/` 这个目录本身会被当成命名空间包（`ee.__file__` 是 None），子进程 `-m ee` 找不到
`ee/ee/__init__.py` 里的 `__version__`，启动即崩：`ImportError: cannot import name '__version__'`。
脚本会自己检查这一点并在启动后端前报错，而不是让后端崩掉之后才在日志里发现。
"""
import asyncio, json, os, pathlib, secrets, socket, subprocess, sys, tempfile, threading, time

import websockets

ROOT = pathlib.Path(__file__).resolve().parents[2]
PKG = ROOT / "ee" / "ee"   # 真正的包目录（含 __init__.py）：<repo>/ee/ee/
T0 = time.time()


def log(*a):
    print(f"[{time.time()-T0:6.2f}]", *a, flush=True)


def check_package():
    """确认**子进程**能拿到真正的包。

    注意检查对象不是本进程：以脚本方式运行时 `sys.path[0]` 是 `ee/scripts/`，
    Python 从那里向上就能找到 `ee/ee/__init__.py`，`import ee` 一切正常。
    但后端是以 `-m ee` 从**当前工作目录**启动的，cwd 在仓库根时 `ee/` 会被当成
    命名空间包（`ee.__file__` 是 None、没有 `__version__`），启动即崩：
    `ImportError: cannot import name '__version__'`。

    所以这里查的是 `PKG/__init__.py` 是否存在、以及能否在 cwd 下以常规包形态导入。
    """
    init = PKG / "__init__.py"
    if not init.is_file():
        raise SystemExit(f"!! 找不到 {init}，仓库结构不对。")
    # 用子进程复现 -m ee 的解析结果，避免本进程 sys.path 的干扰
    probe = ("import ee,sys;"
             "f=getattr(ee,'__file__',None);v=getattr(ee,'__version__',None);"
             "sys.exit(0 if (f and v) else 1)")
    r = subprocess.run([sys.executable, "-c", probe], cwd=os.getcwd(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if r.returncode != 0:
        raise SystemExit(
            "!! 在当前目录用 `python -m ee` 启动后端会失败：`ee` 被解析成命名空间包"
            "（没有 __version__）。\n"
            f"   请在包目录内执行，例如：\n     cd {PKG.parent}\n"
            f"     {sys.executable} scripts/e2e_ws.py {sys.executable} -m ee serve --exit-on-stdin-eof")


def turn_finished(notes):
    """回合是否已经结束。判据是 turn/completed——工具条目出现不代表结束：
    只读工具跑完还有后续步骤，拿它当结束条件会提前收工。"""
    return any(n["method"] == "turn/completed" for n in notes)


def pending_approvals(notes):
    """还没响应的审批条目。

    只看 type == "approval"。todo/read/grep 这些只读工具在 READ_TOOLS 里，**不发审批**；
    ask_user 是 question 类型、响应方式也不同。把这些混进来就会误判成"需要响应"，
    或者反过来——像原先那样只等 approval 而在全只读的场景里空等 60 秒。
    """
    return [n["params"]["item"] for n in notes
            if n["method"] == "item/started"
            and (n["params"].get("item") or {}).get("type") == "approval"]


def orchestrator_verdict(child):
    """判定子会话是否成功完成，返回 (ok, 原因)。

    `status` 只有 idle/running/waiting_approval/compacting，idle 同时覆盖「跑完了」和
    「超预算/被杀」。判成功只能看 run 的 terminal_reason——把它当成「等 idle 就算完成」
    正是 D6-1 U2 要纠正的误报。
    """
    st = (child or {}).get("status")
    reason = (child or {}).get("terminal_reason")
    if st == "running":
        return False, "子会话还在跑（status=running），wait 没等到终态"
    if not reason:
        return False, "子会话没有 terminal_reason：无法判断是跑完还是被中断"
    if reason != "completed":
        return False, f"子会话终态是 {reason}，不是 completed"
    return True, ""


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
    # 编排默认关，且**只能由用户级配置打开**（项目层自己开等于给仓库加了五个可派发
    # 子会话的工具，config.sanitize_project_layer 会把它丢掉）。所以写用户级 config.toml，
    # 必须在 thread/start 之前——enabled 是建会话时读的。EE_HOME 是 home/.ee。
    ee_home_dir = home / ".ee"
    ee_home_dir.mkdir(parents=True, exist_ok=True)
    (ee_home_dir / "config.toml").write_text("[orchestrator]\nenabled = true\n", encoding="utf-8")
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
                log("<-", str(msg.get("method") or f"resp:{msg.get('id')}"))
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
            # 编排默认关（config 默认 enabled=False），这里显式打开：U9 要验的派发链路
            # 只在启用时才存在。用户配置不受影响（用的是临时 home）。
            s.setdefault("orchestrator", {})["enabled"] = True
            await rpc("settings/set", {"settings": s})
            await rpc("workspace/open", {"path": str(ws_dir)})
            th = await rpc("thread/start", {"cwd": str(ws_dir)})
            tid = th["thread"]["id"]
            t_turn = time.time()
            await rpc("turn/start", {"thread_id": tid, "text": "你好"})
            await rpc("settings/get", timeout=5)      # 回合进行中，其他 RPC 必须照常秒回
            deadline = time.time() + 60
            answered: set[str] = set()
            finished = False
            while time.time() < deadline:
                if turn_finished(notes):
                    finished = True
                    break
                # 有待响应审批就回复一次。原先只等 approval 而不先判断回合是否已结束，
                # 在全只读工具的场景（假模型 step 0 就是）里必然空等到超时。
                #
                # 用「标记已回复」而不是从 notes 里删除：原来边遍历边 remove，
                # 一旦同一批里有两条审批就会抛 ValueError，异常被外层吞掉、
                # 整个等待流程直接跳过——表现是"60s 内没有 turn/completed"，
                # 而且看不出真实原因。已回复的 call_id 记在 answered 里，
                # notes 保持只增不改，reader 与这里就不会互相破坏。
                for it in pending_approvals(notes):
                    cid = it.get("call_id")
                    if not cid or cid in answered:
                        continue
                    answered.add(cid)
                    await rpc("approval/respond", {"thread_id": tid, "call_id": cid,
                                                   "choice": "once"})
                await asyncio.sleep(0.1)
            if not finished:
                # 超时要把现场打出来：只报一句"没有 turn/completed"没法定位。
                pend = pending_approvals(notes)
                tools = [n["params"]["item"].get("name") for n in notes
                         if n.get("method") == "item/started"
                         and (n["params"].get("item") or {}).get("type") == "tool"]
                log(f"!!! 60s 内没有 turn/completed；pending_approvals={[p.get('call_id') for p in pend]} "
                    f"answered={sorted(answered)} 已执行工具={tools}")
                ok = False
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
            if not hole and ok:
                # 运行中排队：插话 / 待追加可撤回；中断时没送入的通过 turn/queue_returned 退回
                notes.clear()
                await rpc("turn/start", {"thread_id": tid, "text": "第二轮"})
                r1 = await rpc("turn/start", {"thread_id": tid, "text": "撤回我"})
                w = await rpc("turn/withdraw", {"thread_id": tid, "item_id": (r1 or {}).get("item_id")})
                r2 = await rpc("turn/start", {"thread_id": tid, "text": "完成后再说一句", "queue": "follow"})
                await rpc("turn/interrupt", {"thread_id": tid})
                for _ in range(300):
                    if any(n["method"] == "turn/completed" for n in notes): break
                    await asyncio.sleep(0.1)
                back = [n["params"]["items"] for n in notes if n["method"] == "turn/queue_returned"]
                if not (r1 or {}).get("queued") or (w or {}).get("text") != "撤回我":
                    log("!!! 排队 / 撤回不对", r1, w); ok = False
                elif (r2 or {}).get("queue") != "follow" or not back or back[0][0].get("text") != "完成后再说一句":
                    log("!!! 中断后没有退回待追加的消息", r2, back); ok = False
                else:
                    log("排队 / 撤回 / 中断退回 正常")
            if not hole and ok:
                # 编排（D6-1 U9）：派发 → 等终态 → 校验终态可判定。这是 U9 的核心闭环：
                # 只验「能列出子会话」是不够的，真正要证明的是 wait 能等到并如实带回终态。
                notes.clear()
                made = await rpc("thread/create_child", {"parent_id": tid, "title": "端到端子任务",
                                                         "mode": "read-only", "worktree": False,
                                                         "prompt": "算一下 2+2"})
                child = (made or {}).get("thread") or {}
                cid = child.get("id")
                if not cid:
                    log("!!! 派发子会话失败", made); ok = False
                elif child.get("orchestrator_parent_id") != tid:
                    log("!!! 子会话父关系不对", child.get("orchestrator_parent_id"), tid); ok = False
                else:
                    log(f"子会话已派发：{cid}")
                    waited = await rpc("thread/wait", {"parent_id": tid, "child_ids": [cid],
                                                       "timeout": 60}, timeout=90)
                    kids = (waited or {}).get("threads") or []
                    good, why = orchestrator_verdict(kids[0] if kids else {})
                    if not good:
                        log("!!! 编排终态不可判定：", why, kids[:1]); ok = False
                    else:
                        log(f"编排正常：{cid} 终态 {kids[0].get('terminal_reason')}")
                    # 列一下确认能从父会话看见它
                    listed = (await rpc("thread/children", {"parent_id": tid}) or {}).get("threads") or []
                    if cid not in [x.get("id") for x in listed]:
                        log("!!! thread/children 没列出刚建的子会话", listed); ok = False
                    await rpc("thread/delete", {"thread_id": cid})
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


if __name__ == "__main__":
    # 只在作为脚本运行时才做这些：被 import（例如测试）时不该启动后端、也不该因
    # 当前目录不对就 SystemExit——那会让这个模块无法被任何地方复用。
    args = sys.argv[1:]
    hole = bool(args) and args[0] == "--blackhole"
    check_package()      # 启动后端前先确认 -m ee 能跑，否则只会看到后端崩在 import 上
    asyncio.run(main(args[1:] if hole else args, hole))
