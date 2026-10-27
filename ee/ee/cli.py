"""命令行入口（给桌面端、排错和自动化用，没有终端交互界面）：

  ee serve [--port N] [--host 127.0.0.1] [--exit-on-stdin-eof]   桌面端后端（token 从环境变量 EE_SERVE_TOKEN 读）
  ee exec [提示词|-] [--json] [-C 目录] [--mode …] [--resume id|--last] …  无界面跑一个回合（见 ee/headless.py）
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
    s.add_argument("--parent-pid", type=int, default=None, help="这个进程（桌面端）退出时后端跟着退出，并结束所有子进程")
    t = sub.add_parser("selftest", help="检查打包是否完整（openai/httpx/证书/编码），不联网，输出 JSON")
    t.add_argument("--secrets", action="store_true", help="另外测一次凭据存储读写删（写入临时项后立即删除）")
    from ee.headless import add_arguments as exec_arguments
    x = sub.add_parser("exec", help="无界面跑一个回合就退出（脚本 / CI / 定时任务用）",
                       description="无界面执行：跑完一个回合就退出。会话与桌面端共用，默认只读。",
                       epilog="退出码：0 完成；1 出错；2 参数错误；3 到达步数上限；4 防死循环停下；130 被 Ctrl+C 中断。"
                              " 不加 --json 时 stdout 只有最终回答，进度在 stderr。",
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    exec_arguments(x)
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
        _migrate_secrets()
        from ee.server import app as server_app
        token = server_app.take_serve_token(args.token)        # 读完即从 os.environ 删除，子进程拿不到
        if not token:
            import secrets
            token = secrets.token_urlsafe(24)
            print(f"token: {token}", file=sys.stderr, flush=True)
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            print("警告：后端监听在非本机地址上，任何拿到 token 的人都能控制它。", file=sys.stderr)
        try:
            asyncio.run(server_app.serve(args.host, args.port, token, exit_on_stdin_eof=args.exit_on_stdin_eof,
                                         parent_pid=args.parent_pid))
        except KeyboardInterrupt:
            pass
        return 0
    if args.cmd == "exec":
        from ee import log as eelog
        eelog.setup()
        _migrate_secrets()
        from ee.headless import run as run_exec
        return run_exec(args)
    if args.cmd == "selftest":
        return _selftest(secrets_check=args.secrets)
    if args.cmd == "doctor":
        from ee.config import load_config
        from ee.doctor import run_doctor
        cwd = Path(args.cd).resolve() if args.cd else Path.cwd()
        return asyncio.run(run_doctor(load_config(cwd), args.model_name))
    parser.print_help()
    print("\nee 现在只有桌面端：请打开 ee 桌面应用。开发时用 `cd desktop && npm run dev`。", file=sys.stderr)
    return 2


def _migrate_secrets() -> None:
    """启动时：config.toml 明文 key 迁进凭据存储、抹掉备份里的明文（失败只记日志）。"""
    import logging
    try:
        from ee.config import user_config_path
        from ee.core.migrate import migrate_user_file
        from ee.core.secrets import scrub_backups
        migrate_user_file()
        n = scrub_backups(user_config_path())
        if n:
            logging.getLogger("ee.secrets").info("已抹掉 %d 个配置备份里的明文 key", n)
    except Exception as e:  # noqa: BLE001
        logging.getLogger("ee.secrets").warning("key 迁移失败：%s", type(e).__name__)


def _secrets_selftest(out: dict) -> bool:
    import uuid
    from ee.core import secrets
    name = f"selftest/{uuid.uuid4().hex[:8]}"
    val = "sk-selftest-" + uuid.uuid4().hex
    try:
        st = secrets.store()
        out["secrets_backend"] = st.name
        st.set(name, val)
        got = st.get(name)
        st.delete(name)
        gone = st.get(name)
        ok = got == val and gone is None
        if sys.platform == "win32":
            from ee.core.secrets import _dpapi
            ok = ok and _dpapi(_dpapi(b"ee-dpapi", True), False) == b"ee-dpapi"
            out["dpapi"] = "ok" if ok else "fail"
        out["secrets"] = "ok" if ok else "mismatch"
        return ok
    except Exception as e:  # noqa: BLE001
        out["secrets"] = f"{type(e).__name__}: {e}"
        return False


def _selftest(secrets_check: bool = False) -> int:
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
    try:   # 文档解析组件（D4-7）：打包漏了就在 CI 里暴露；docx/pptx 需要内置模板，真建一个文档验证
        import io

        import docx
        import openpyxl
        import pptx
        import pypdf
        buf = io.BytesIO()
        docx.Document().save(buf)
        pptx.Presentation().save(io.BytesIO())
        out["docs"] = {"pypdf": pypdf.__version__, "openpyxl": openpyxl.__version__, "docx": len(buf.getvalue()) > 0}
    except Exception as e:  # noqa: BLE001
        ok = False
        out["docs_error"] = f"{type(e).__name__}: {e}"
    try:   # 仓库地图（D5-6）：真解析一小段 Python/TSX，语言库漏打包时在 CI 里暴露
        from ee.core.repomap import selftest as repomap_selftest
        rm = repomap_selftest()
        out["repomap"] = rm
        ok = ok and rm["ok"]
    except Exception as e:  # noqa: BLE001
        ok = False
        out["repomap_error"] = f"{type(e).__name__}: {e}"
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
    if secrets_check:
        ok = _secrets_selftest(out) and ok
    out["ok"] = ok
    print(json.dumps(out, ensure_ascii=False), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
