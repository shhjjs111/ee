"""命令行入口（只给桌面端和排错用，没有终端交互界面）：

  ee serve [--port N] [--host 127.0.0.1] [--exit-on-stdin-eof]   桌面端后端（token 从环境变量 EE_SERVE_TOKEN 读）
  ee doctor [模型]                                               自检某个模型的兼容性（会真实调用 API）
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from ee import __version__


def _fix_windows_console() -> None:
    if sys.platform.startswith("win"):
        for s in (sys.stdout, sys.stderr):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ee", description="ee：个人 AI 编程 agent（桌面端后端）")
    p.add_argument("-V", "--version", action="version", version=f"ee {__version__}")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="启动桌面端用的本地后端（WebSocket，仅 127.0.0.1）")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=0, help="端口，0 = 随机")
    s.add_argument("--token", help="连接口令（默认读环境变量 EE_SERVE_TOKEN，都没有则随机生成并打印）")
    s.add_argument("--exit-on-stdin-eof", action="store_true", help="stdin 关闭时退出（由 Electron 拉起时用）")
    sub.add_parser("selftest", help="检查打包是否完整（openai/httpx/证书/编码），不联网，输出 JSON")
    d = sub.add_parser("doctor", help="自检某个模型的兼容性（会真实调用 API）")
    d.add_argument("model_name", nargs="?", help="模型名，默认用默认模型")
    d.add_argument("-C", "--cd", help="按这个目录加载项目级配置")
    return p


def main(argv: list[str] | None = None) -> int:
    _fix_windows_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "serve":
        from ee import log as eelog
        eelog.setup()
        from ee.server.app import serve
        token = args.token or os.environ.get("EE_SERVE_TOKEN")
        if not token:
            import secrets
            token = secrets.token_urlsafe(24)
            print(f"token: {token}", file=sys.stderr, flush=True)
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            print("警告：后端监听在非本机地址上，任何拿到 token 的人都能控制它。", file=sys.stderr)
        try:
            asyncio.run(serve(args.host, args.port, token, exit_on_stdin_eof=args.exit_on_stdin_eof))
        except KeyboardInterrupt:
            pass
        return 0
    if args.cmd == "selftest":
        return _selftest()
    if args.cmd == "doctor":
        from ee.config import load_config
        from ee.doctor import run_doctor
        cwd = Path(args.cd).resolve() if args.cd else Path.cwd()
        return asyncio.run(run_doctor(load_config(cwd), args.model_name))
    parser.print_help()
    print("\nee 现在只有桌面端：请打开 ee 桌面应用。开发时用 `cd desktop && npm run dev`。", file=sys.stderr)
    return 2


def _selftest() -> int:
    import json
    import ssl
    out: dict = {"version": __version__, "frozen": bool(getattr(sys, "frozen", False)), "python": sys.version.split()[0]}
    ok = True
    try:
        import openai
        out["openai"] = openai.__version__
        from ee.config import ModelSpec
        from ee.core.providers.openai_chat import ChatClient
        from ee.core.providers.profiles import profile_for
        spec = ModelSpec(alias="selftest", provider="p", model="m", base_url="https://api.deepseek.com",
                         api_key_env="", profile=profile_for("deepseek-flash"), stored_key="sk-selftest-0000")
        c = ChatClient(spec, network={"proxy": "direct"})
        out["client"] = type(c.client).__name__
        import anyio._backends._asyncio  # noqa: F401
        out["anyio"] = "ok"
    except Exception as e:  # noqa: BLE001
        ok = False
        out["openai_error"] = f"{type(e).__name__}: {e}"
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        out["certifi"] = os.path.isfile(certifi.where())
        out["ca_certs"] = len(ctx.get_ca_certs())
        ok = ok and out["certifi"]
    except Exception as e:  # noqa: BLE001
        ok = False
        out["cert_error"] = f"{type(e).__name__}: {e}"
    try:
        "你好".encode("gbk").decode("gbk")
        out["encodings"] = "ok"
    except Exception as e:  # noqa: BLE001
        ok = False
        out["encoding_error"] = str(e)
    try:
        from ee.core.providers.net import plan
        out["proxy"] = plan("https://api.deepseek.com", {"proxy": "system"}).describe
        from ee import log as eelog
        out["log_dir"] = str(eelog.log_dir())
    except Exception as e:  # noqa: BLE001
        out["proxy_error"] = str(e)
    out["ok"] = ok
    print(json.dumps(out, ensure_ascii=False), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
