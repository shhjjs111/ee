import clsx from "clsx";
import { ChevronRight, FileCode2, FileDiff, GitBranch, GitCommitHorizontal, RefreshCw, RotateCcw, SearchCheck, SquareTerminal, Undo2 } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useApp, type RightTab } from "../lib/store";
import type { DiffFile, GitReview } from "../types";
import { DiffView, Stat } from "./DiffView";
import { FilesPanel } from "./FilesPanel";
import { TerminalPanel } from "./TerminalPanel";
import { Button, Spinner, Tip } from "./ui";

const STATUS: Record<string, { label: string; cls: string }> = {
  modified: { label: "M", cls: "text-warn" },
  added: { label: "A", cls: "text-ok" },
  deleted: { label: "D", cls: "text-danger" },
  renamed: { label: "R", cls: "text-accent" },
};

function FileRow({ f, cwd, onChanged }: { f: DiffFile; cwd: string; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const s = useApp();
  const st = STATUS[f.status] ?? STATUS.modified;
  const name = f.path.split("/").pop();
  const dir = f.path.includes("/") ? f.path.slice(0, f.path.lastIndexOf("/")) : "";
  const revert = async () => {
    const msg = f.status === "added" ? `删除新文件 ${f.path}？` : `把 ${f.path} 恢复成最近一次提交的版本？未提交的改动会丢失。`;
    if (!confirm(msg)) return;
    setBusy(true);
    try {
      await s.rpc!.call("git/revert_file", { cwd, path: f.path });
      s.showToast(f.status === "added" ? `已删除 ${f.path}` : `已撤销 ${f.path} 的改动`);
      onChanged();
    } catch (e) {
      s.showToast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="border-b border-line last:border-b-0">
      <div
        className="group flex h-9 cursor-default items-center gap-2 px-3 text-[13px] hover:bg-hover"
        onClick={() => setOpen(!open)}
      >
        <ChevronRight size={13} className={clsx("shrink-0 text-faint transition-transform", open && "rotate-90")} />
        <span className={clsx("mono w-3 shrink-0 text-[11.5px] font-semibold", st.cls)}>{st.label}</span>
        <span className="min-w-0 truncate">
          <span className="text-fg">{name}</span>
          {dir && <span className="ml-1.5 text-[12px] text-faint">{dir}</span>}
        </span>
        <span className="flex-1" />
        <Stat add={f.additions} del={f.deletions} className="shrink-0 group-hover:hidden" />
        <span className="hidden shrink-0 group-hover:flex" onClick={(e) => e.stopPropagation()}>
          <Tip label={f.status === "added" ? "删除这个新文件" : "撤销这个文件的改动"}>
            <Button size="icon-sm" variant="danger" onClick={revert} disabled={busy}>
              {busy ? <Spinner size={12} /> : <RotateCcw size={13} />}
            </Button>
          </Tip>
        </span>
      </div>
      {open && (
        <div className="border-t border-line bg-code">
          {f.binary ? <div className="px-4 py-3 text-[12px] text-faint">二进制或过大的文件，不显示内容</div> : <DiffView patch={f.patch} />}
        </div>
      )}
    </div>
  );
}

function Review() {
  const s = useApp();
  const d = s.activeThreadId ? s.details[s.activeThreadId] : null;
  const cwd = d?.cwd ?? s.activeWorkspace;
  const [data, setData] = useState<GitReview | null>(null);
  const [loading, setLoading] = useState(false);

  // 回合中改文件会频繁触发刷新、也可能切换项目：只认最后一次请求的结果
  const gen = useRef(0);
  const load = useCallback(async () => {
    if (!cwd || !s.rpc) return;
    const tok = ++gen.current;
    setLoading(true);
    try {
      const r = await s.rpc.call("git/diff", { cwd });
      if (tok === gen.current) setData(r);
    } catch (e) {
      if (tok !== gen.current) return;
      setData(null);
      s.showToast((e as Error).message, "error");
    } finally {
      if (tok === gen.current) setLoading(false);
    }
  }, [cwd, s.rpc]);

  useEffect(() => {
    load();
  }, [load, s.reviewNonce]);

  const undo = async () => {
    if (!d) return;
    try {
      const r = await s.rpc!.call("thread/undo", { thread_id: d.id });
      s.showToast(r.restored.length ? `已恢复 ${r.restored.length} 个文件` : "上一回合没有可撤销的文件改动");
      load();
    } catch (e) {
      s.showToast((e as Error).message, "error");
    }
  };

  if (!cwd) return <div className="p-6 text-center text-[13px] text-faint">先选择一个项目</div>;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex h-10 shrink-0 items-center gap-2 border-b border-line px-3 text-[12.5px] text-muted">
        {data?.is_repo ? (
          <>
            <GitBranch size={13} />
            <span className="truncate">{data.branch}</span>
            <span className="text-faint">·</span>
            <span>{data.files.length} 个文件</span>
            {data.files.length > 0 && <Stat add={data.additions ?? 0} del={data.deletions ?? 0} />}
          </>
        ) : (
          <span>{data ? "不是 git 仓库" : ""}</span>
        )}
        <span className="flex-1" />
        {d && (
          <Tip label="撤销 ee 上一回合写过的文件">
            <Button size="icon-sm" onClick={undo} disabled={d.status !== "idle"}>
              <Undo2 size={14} />
            </Button>
          </Tip>
        )}
        {data?.is_repo && data.files.length > 0 && (
          <>
            <Tip label="审查这些改动（/review）">
              <Button size="icon-sm" onClick={() => s.set({ dialog: { kind: "review" } })}>
                <SearchCheck size={14} />
              </Button>
            </Tip>
            <Tip label="提交（模型写提交信息，确认后 git commit）">
              <Button size="icon-sm" onClick={() => s.set({ dialog: { kind: "commit" } })} disabled={!!d && d.status !== "idle"}>
                <GitCommitHorizontal size={14} />
              </Button>
            </Tip>
          </>
        )}
        <Tip label="刷新">
          <Button size="icon-sm" onClick={load}>
            {loading ? <Spinner size={12} /> : <RefreshCw size={13} />}
          </Button>
        </Tip>
      </div>
      <div className="flex-1 overflow-y-auto">
        {data && !data.is_repo && (
          <div className="px-6 py-10 text-center text-[12.5px] leading-relaxed text-faint">
            {data.git_available ? "这个文件夹不是 git 仓库，没法按文件对比。" : "没有找到 git。安装 Git for Windows 后可以按文件审阅改动。"}
            <br />
            仍可用右上角的撤销按钮回退 ee 上一回合的改动。
          </div>
        )}
        {data?.is_repo && data.files.length === 0 && (
          <div className="flex flex-col items-center px-6 py-14 text-center text-[12.5px] text-faint">
            <FileDiff size={28} className="mb-3 opacity-50" />
            工作区没有未提交的改动
          </div>
        )}
        {data?.is_repo && data.files.map((f) => <FileRow key={f.path} f={f} cwd={cwd} onChanged={load} />)}
      </div>
    </div>
  );
}

const TABS: { id: RightTab; label: string; icon: React.ReactNode }[] = [
  { id: "review", label: "改动", icon: <FileDiff size={14} /> },
  { id: "terminal", label: "终端", icon: <SquareTerminal size={14} /> },
  { id: "files", label: "文件", icon: <FileCode2 size={14} /> },
];

const MIN_W = 320;
const MAX_W = 820;

export function RightPanel() {
  const s = useApp();
  const [width, setWidth] = useState(() => {
    const w = Number(localStorage.getItem("ee.rightWidth"));
    return w >= MIN_W && w <= MAX_W ? w : 400;
  });
  const [termMounted, setTermMounted] = useState(false);
  const drag = useRef<{ x: number; w: number } | null>(null);
  useEffect(() => {
    if (s.rightTab === "terminal") setTermMounted(true);
  }, [s.rightTab]);
  if (!s.rightOpen || s.view !== "chat") return null;

  const onDown = (e: React.PointerEvent) => {
    drag.current = { x: e.clientX, w: width };
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
  };
  const onMove = (e: React.PointerEvent) => {
    if (!drag.current) return;
    const max = Math.min(MAX_W, window.innerWidth - 560);
    setWidth(Math.round(Math.max(MIN_W, Math.min(max, drag.current.w + drag.current.x - e.clientX))));
  };
  const onUp = () => {
    if (!drag.current) return;
    drag.current = null;
    localStorage.setItem("ee.rightWidth", String(width));
  };

  return (
    <aside className="relative flex shrink-0 flex-col border-l border-line bg-bg" style={{ width }}>
      <div
        className="no-drag absolute -left-[3px] top-0 z-20 h-full w-[6px] cursor-col-resize transition-colors hover:bg-accent/30"
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onDoubleClick={() => {
          setWidth(400);
          localStorage.setItem("ee.rightWidth", "400");
        }}
      />
      <div className="drag flex h-11 shrink-0 items-center gap-1 border-b border-line pl-2" style={{ paddingRight: "calc(var(--overlay-w) + 8px)" }}>
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => s.set({ rightTab: t.id })}
            className={clsx(
              "no-drag flex h-7 items-center gap-1.5 rounded-lg px-2.5 text-[12.5px] transition-colors",
              s.rightTab === t.id ? "bg-active text-fg" : "text-muted hover:bg-hover hover:text-fg",
            )}
          >
            {t.icon}
            {t.label}
          </button>
        ))}
      </div>
      {s.rightTab === "review" && <Review />}
      {termMounted && (
        <div className={clsx("min-h-0 flex-1 flex-col", s.rightTab === "terminal" ? "flex" : "hidden")}>
          <TerminalPanel />
        </div>
      )}
      {s.rightTab === "files" && <FilesPanel />}
    </aside>
  );
}
