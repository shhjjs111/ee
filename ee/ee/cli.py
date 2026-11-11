"""命令行入口（给桌面端、排错和自动化用，没有终端交互界面）：

  ee serve [--port N] [--host 127.0.0.1] [--exit-on-stdin-eof]   桌面端后端（token 从环境变量 EE_SERVE_TOKEN 读）
  ee exec [提示词|-] [--json] [-C 目录] [--mode …] [--resume id|--last] …  无界面跑一个回合（见 ee/headless.py）
  ee doctor [模型]                                               自检某个模型的兼容性（会真实调用 API）
  ee usage [会话id] [--all] [--json]                             看缓存的命中率诊断（逐请求账本：命中率 / 断点 / 压缩）
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
            reconfigure = getattr(s, "reconfigure", None)
            if reconfigure is None:
                continue
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _setup_logging() -> None:
    """配日志，并顺手用日志里已有的首包记录回填延迟统计。

    回填放在这里而不是每处 record 之前：日志目录就在 log.setup() 里确定，一次就能拿全。
    """
    from ee import log as eelog
    from ee.core import latency
    latency.backfill_from_log(eelog.setup())


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
    e = sub.add_parser("export", help="把一个会话导出成 Markdown / JSON",
                       description="导出会话：不给 -o 时打印到 stdout，给了才写文件。"
                                   "默认拒绝覆盖已存在的文件（用 --force 明确覆盖）。")
    e.add_argument("thread_id", nargs="?", help="会话 id（用 --list 查看现有会话）")
    e.add_argument("--format", default="markdown", choices=("markdown", "json"),
                   help="导出格式，默认 markdown")
    e.add_argument("-o", "--output", help="写入这个文件（必须能给绝对路径或相对当前目录的路径）")
    e.add_argument("--force", action="store_true", help="目标文件已存在时覆盖")
    e.add_argument("--list", action="store_true", help="列出现有会话 id 与标题后就退出")
    u = sub.add_parser("usage", help="看会话的缓存命中率诊断（逐请求账本：命中率 / 断点 / 压缩）",
                       description="从会话 JSONL 里的逐请求 usage 行算命中率与断点归因。"
                                   "不给 id 时看最近的会话；--all 汇总全部会话。")
    u.add_argument("thread_id", nargs="?", help="会话 id（用 `ee export --list` 看有哪些）")
    u.add_argument("--all", action="store_true", help="汇总全部会话，不只看一个")
    u.add_argument("--json", action="store_true", help="输出 JSON（给脚本 / CI 用）")
    u.add_argument("--limit", type=int, default=20, help="--all 时最多看最近多少个会话（默认 20）")
    return p


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    if raw[:1] == ["__sandbox_exec"]:              # 内部用：沙箱启动器（见 ee/core/sandbox.py），不进帮助
        from ee.core.sandbox import helper_main
        return helper_main(raw[1:])
    _fix_windows_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "serve":
        _setup_logging()
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
        _setup_logging()
        _migrate_secrets()
        from ee.headless import run as run_exec
        return run_exec(args)
    if args.cmd == "selftest":
        return _selftest(secrets_check=args.secrets)
    if args.cmd == "export":
        return _cmd_export(args)
    if args.cmd == "usage":
        return _cmd_usage(args)
    if args.cmd == "doctor":
        from ee.config import load_config
        from ee.doctor import run_doctor
        cwd = Path(args.cd).resolve() if args.cd else Path.cwd()
        return asyncio.run(run_doctor(load_config(cwd), args.model_name))
    parser.print_help()
    print("\nee 现在只有桌面端：请打开 ee 桌面应用。开发时用 `cd desktop && npm run dev`。", file=sys.stderr)
    return 2


def _cmd_export(args) -> int:
    """`ee export`：把会话导出到 stdout 或文件。

    这是导出能力**唯一**当前可用的生产入口（桌面端入口尚未做）。因此这里
    自己承担全部边界检查，不依赖上层：会话不存在要报错而不是导出空文件，
    不指定输出就绝不写盘，已存在不加 --force 就拒绝。
    """
    from ee.config import ee_home
    from ee.core.export import FORMATS, export
    from ee.core.store import SessionStore

    home = ee_home()
    sessions = home / "sessions"

    if args.list:
        rows = []
        for p in SessionStore.list_sessions(home):
            try:
                s = SessionStore.summary(p)
            except Exception:  # noqa: BLE001  单个坏文件不该让整条命令失败
                continue
            rows.append((p.stem, s.get("title") or "新对话"))
        for tid, title in rows:
            print(f"{tid}  {title}")
        return 0

    if args.format not in FORMATS:
        print(f"未知导出格式：{args.format}（可选：{', '.join(FORMATS)}）", file=sys.stderr)
        return 2

    if not args.thread_id:
        print("缺少会话 id（用 `ee export --list` 看有哪些）", file=sys.stderr)
        return 2

    path = sessions / f"{args.thread_id}.jsonl"
    if not path.is_file():
        print(f"会话不存在：{args.thread_id}", file=sys.stderr)
        return 1

    try:
        content = export(SessionStore.load(path), args.format)
    except Exception as e:  # noqa: BLE001  会话文件损坏时给可读错误，不是 traceback
        print(f"读取会话失败：{e}", file=sys.stderr)
        return 1

    if not args.output:
        # 不指定输出就不写盘：脚本里 `ee export ID > x.md` 更符合直觉
        print(content, end="" if content.endswith("\n") else "\n")
        return 0

    target = Path(args.output).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    if target.is_dir():
        print(f"输出路径是目录：{target}", file=sys.stderr)
        return 2
    if target.exists() and not args.force:
        print(f"目标文件已存在：{target}（加 --force 覆盖）", file=sys.stderr)
        return 1
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        from ee.server.app import _atomic_write
        _atomic_write(target, content)
    except OSError as e:
        print(f"写入失败：{e}", file=sys.stderr)
        return 1
    print(f"已导出 {len(content)} 字符到 {target}", file=sys.stderr)
    return 0


def _cmd_usage(args) -> int:
    """`ee usage`：从会话 JSONL 的逐请求账本算命中率与断点归因。

    这是 §8 验收断言的可执行入口。**没有这份账本就做不了验收**：turn_end 的
    累计快照只有「到现在花了多少」，看不出哪一次请求漏了、漏之前刚发生了什么。

    只读会话文件，不碰运行中的后端，所以随时可以跑（包括对话进行中）。
    """
    import json

    from ee.config import ee_home
    from ee.core.cache_report import summarize
    from ee.core.store import SessionStore

    home = ee_home()
    sessions = home / "sessions"

    def rows_of(path: Path) -> list[dict]:
        try:
            return SessionStore.usage_records(path)
        except Exception:  # noqa: BLE001  单个坏文件不该让整条命令失败
            return []

    if args.thread_id:
        path = sessions / f"{args.thread_id}.jsonl"
        if not path.is_file():
            print(f"会话不存在：{args.thread_id}（用 `ee export --list` 看有哪些）", file=sys.stderr)
            return 1
        targets = [path]
    else:
        targets = SessionStore.list_sessions(home)[:max(1, args.limit)]

    if not targets:
        print("还没有任何会话。", file=sys.stderr)
        return 1

    if args.all or not args.thread_id:
        rows = [r for p in targets for r in rows_of(p)]
        if not rows:
            print("这些会话里没有逐请求账本（老会话，或账本落盘上线前录的）。", file=sys.stderr)
            return 1
        d = summarize(rows)
        d["sessions"] = len(targets)
        if args.json:
            print(json.dumps(d, ensure_ascii=False, indent=2))
        else:
            _print_usage(d, title=f"最近 {len(targets)} 个会话")
        return 0

    rows = rows_of(targets[0])
    if not rows:
        print("这个会话没有逐请求账本（老会话，或账本落盘上线前录的）。", file=sys.stderr)
        return 1
    d = summarize(rows)
    d["sessions"] = 1
    # 压缩审计：压缩会不会把细节吃掉 / 压完要不要重读（roadmap §7）
    from ee.core import compaction_audit as A

    try:
        d["compaction_audit"] = A.audit(SessionStore.load_messages(targets[0]),
                                        records=list(SessionStore._records(targets[0])))
    except Exception:  # noqa: BLE001  诊断不该让主命令失败
        d["compaction_audit"] = None
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        _print_usage(d, title=targets[0].stem)
        _print_audit(d.get("compaction_audit"))
    return 0


def _pct(v) -> str:
    return "未知" if v is None else f"{v * 100:.2f}%"


def _print_usage(d: dict, *, title: str) -> None:
    """人读摘要：先回答「到没到 99%」，再给断点与压缩的对照。"""
    m = d["main"]
    print(f"{title}")
    print(f"  请求 {d['requests']} 次（缺用量 {d['usage_missing']} 次）")
    print(f"  主对话缓存命中 {_pct(m['hit_rate'])} · 覆盖 {_pct(m['hit_coverage'])} 输入"
          f" · {m['requests']} 次请求")
    br = d["breaks"]
    print(f"  断点 {br['count']} 次 / 压缩 {d['compactions']} 次", end="")
    if br["count"] > d["compactions"]:
        print("  ← 断点比压缩还多，说明有别的东西在改写前缀（换模型 / 修剪 / 老版本白压）")
    else:
        print()
    g = d["normal_gap"]
    if g["count"]:
        print(f"  正常回合未命中：中位 {g['median']} · p90 {g['p90']} · 最大 {g['max']} token")
    for b in d["buckets"]:
        if b["requests"]:
            print(f"    {b['label']:<9} {_pct(b['hit_rate']):>7} · {b['requests']} 次")
    others = {k: v for k, v in d["by_role"].items() if k != "main" and v["requests"]}
    if others:
        print("  其它请求：" + "，".join(f"{k} {v['requests']} 次" for k, v in sorted(others.items())))


def _print_audit(a: dict | None) -> None:
    """压缩审计两行：摘要把细节吃掉多少、压完有没有被迫重读。"""
    if not a:
        return
    cov = a.get("identifier_coverage")
    rr = a.get("reread") or {}
    if cov and cov.get("count"):
        mean = cov.get("mean_rate")
        if mean is None:
            print(f"  摘要标识符覆盖率：这次压缩没留记录（{cov['count']} 次压缩，字段是本版新加的）")
        else:
            worst = min((x["rate"] for x in cov["items"] if x["rate"] is not None), default=None)
            print(f"  摘要标识符覆盖率：均值 {_pct(mean)} · 最差 {_pct(worst)} · {cov['count']} 次压缩")
    if rr.get("compactions"):
        line = f"  压缩后 3 轮内重读：{rr['rereads']} 次 / {rr['compactions']} 次压缩"
        if rr["rereads"]:
            line += "  ← 保留段没盖住刚看过的内容（keep_tools / keep_tokens 被调小了？）"
        else:
            line += "  ← 0 是健康值"
        print(line)


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
