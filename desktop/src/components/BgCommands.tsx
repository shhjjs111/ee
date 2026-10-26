// 后台命令（D4-6）：输入框工具栏里的「后台 N」按钮，展开列出本会话的后台命令，可看输出、停止。
import * as Dialog from "@radix-ui/react-dialog";
import clsx from "clsx";
import { RefreshCw, Square, SquareTerminal, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useApp } from "../lib/store";
import type { BgProc } from "../types";
import { Button, Spinner, Tip } from "./ui";

function fmtDur(sec: number) {
  if (sec < 60) return `${Math.round(sec)} 秒`;
  if (sec < 3600) return `${Math.floor(sec / 60)} 分 ${Math.round(sec % 60)} 秒`;
  return `${Math.floor(sec / 3600)} 小时 ${Math.floor((sec % 3600) / 60)} 分`;
}

function statusText(p: BgProc) {
  if (p.status === "running") return "运行中";
  if (p.status === "killed") return "已停止";
  return `已退出 · ${p.exit_code ?? "?"}`;
}

export function OutputDialog({ threadId, proc, onClose }: { threadId: string; proc: BgProc; onClose: () => void }) {
  const rpc = useApp((s) => s.rpc);
  const live = useApp((s) => s.bgProcs[threadId]?.find((x) => x.id === proc.id)) ?? proc;
  const [text, setText] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const box = useRef<HTMLPreElement>(null);
  const seq = useRef(0); // 只采用最后一次发出的读取结果（轮询和手动刷新可能乱序返回）；关闭后不再更新
  const load = async () => {
    const my = ++seq.current;
    try {
      const r = await rpc!.call("shell/read", { thread_id: threadId, id: proc.id, tail: 60000 });
      if (my !== seq.current) return;
      setText(r.output as string);
      setErr(null);
      requestAnimationFrame(() => box.current && (box.current.scrollTop = box.current.scrollHeight));
    } catch (e) {
      if (my !== seq.current) return;
      setErr((e as Error).message);
    }
  };
  useEffect(() => () => void ++seq.current, []);
  useEffect(() => {
    load();
    if (live.status !== "running") return;
    const t = setInterval(load, 1500); // 运行中自动刷新
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live.status]);
  return (
    <Dialog.Root open onOpenChange={(o) => !o && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/30 fadein" />
        <Dialog.Content
          aria-describedby={undefined}
          className="fixed left-1/2 top-[8%] z-50 flex max-h-[80vh] w-[820px] max-w-[94vw] -translate-x-1/2 flex-col rounded-2xl border border-line bg-elev p-4 shadow-[var(--shadow)] fadein"
        >
          <div className="mb-2 flex items-center gap-2">
            <SquareTerminal size={14} className="text-muted" />
            <Dialog.Title className="mono min-w-0 flex-1 truncate text-[13px] font-semibold text-fg">
              {proc.id} · {proc.command}
            </Dialog.Title>
            <span className="shrink-0 text-[12px] text-muted">{statusText(live)}</span>
            <Tip label="刷新">
              <Button size="icon-sm" onClick={load} aria-label="刷新">
                <RefreshCw size={13} />
              </Button>
            </Tip>
            <Dialog.Close asChild>
              <Button size="icon-sm" aria-label="关闭">
                <X size={14} />
              </Button>
            </Dialog.Close>
          </div>
          <div className="mb-2 truncate text-[11.5px] text-faint" title={live.log_path}>
            完整日志：{live.log_path}
          </div>
          {err && <div className="mb-2 text-[12px] text-danger">{err}</div>}
          <pre
            ref={box}
            className="mono min-h-[200px] flex-1 overflow-auto whitespace-pre-wrap rounded-xl border border-line bg-code p-3 text-[12px] leading-relaxed text-fg/90"
          >
            {text === null ? <Spinner /> : text || "（暂无输出）"}
          </pre>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export function BgCommands({ threadId }: { threadId: string | null }) {
  const procs = useApp((s) => (threadId ? s.bgProcs[threadId] : undefined)) ?? [];
  const rpc = useApp((s) => s.rpc);
  const showToast = useApp((s) => s.showToast);
  const [open, setOpen] = useState(false);
  const [view, setView] = useState<BgProc | null>(null);
  const [, tick] = useState(0);
  const running = procs.filter((p) => p.status === "running");
  useEffect(() => {
    if (!open || !running.length) return;
    const t = setInterval(() => tick((x) => x + 1), 1000);
    return () => clearInterval(t);
  }, [open, running.length]);
  if (!threadId || !procs.length) return null;
  const kill = async (p: BgProc) => {
    try {
      await rpc!.call("shell/kill", { thread_id: threadId, id: p.id });
    } catch (e) {
      showToast((e as Error).message, "error");
    }
  };
  const now = Date.now() / 1000;
  return (
    <div className="relative">
      <Tip label="本对话的后台命令（dev server、watch 等）">
        <button
          onClick={() => setOpen(!open)}
          className={clsx(
            "flex h-7 items-center gap-1.5 rounded-full border border-line px-2.5 text-[12px] transition hover:bg-soft",
            running.length ? "text-fg" : "text-muted",
          )}
        >
          <span className={clsx("h-1.5 w-1.5 rounded-full", running.length ? "bg-ok animate-pulse" : "bg-faint")} />
          后台 {running.length}
          {procs.length > running.length && <span className="text-faint">/ {procs.length}</span>}
        </button>
      </Tip>
      {open && (
        <>
          <div className="fixed inset-0 z-30" onClick={() => setOpen(false)} />
          <div className="absolute bottom-9 right-0 z-40 w-[440px] max-w-[80vw] rounded-xl border border-line bg-elev p-1.5 shadow-[var(--shadow)] fadein">
            <div className="px-2 pb-1 pt-0.5 text-[11.5px] text-faint">后台命令 · 回合结束不会停止，删除对话或退出 ee 时结束</div>
            {procs
              .slice()
              .reverse()
              .map((p) => (
                <div key={p.id} className="group flex items-center gap-2 rounded-lg px-2 py-1.5 hover:bg-soft">
                  <span className={clsx("h-1.5 w-1.5 shrink-0 rounded-full", p.status === "running" ? "bg-ok" : p.status === "killed" || (p.exit_code ?? 0) !== 0 ? "bg-danger" : "bg-faint")} />
                  <button className="min-w-0 flex-1 text-left" onClick={() => setView(p)}>
                    <div className="mono truncate text-[12.5px] text-fg">{p.command}</div>
                    <div className="text-[11.5px] text-muted">
                      {p.id} · {statusText(p)} · {fmtDur(p.status === "running" ? now - p.started : p.duration)}
                    </div>
                  </button>
                  <Button size="sm" onClick={() => setView(p)}>
                    输出
                  </Button>
                  {p.status === "running" && (
                    <Tip label="停止（连同子进程）">
                      <Button size="icon-sm" onClick={() => kill(p)} aria-label="停止">
                        <Square size={11} fill="currentColor" />
                      </Button>
                    </Tip>
                  )}
                </div>
              ))}
          </div>
        </>
      )}
      {view && <OutputDialog threadId={threadId} proc={view} onClose={() => setView(null)} />}
    </div>
  );
}
