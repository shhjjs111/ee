import clsx from "clsx";
import { Check, ChevronRight, FileCode2, FileDiff, GitBranch, GitCommitHorizontal, History, RefreshCw, RotateCcw, SearchCheck, SquareTerminal, Undo2 } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { formatWhen, pendingCount } from "../lib/hunks";
import { useApp, type RightTab } from "../lib/store";
import { CONTENT_MIN, INSPECTOR_DEFAULT, INSPECTOR_MAX, INSPECTOR_MIN } from "../lib/layout";
import { useLayout } from "../lib/layoutStore";
import type { DiffFile, GitReview, HunkInfo } from "../types";
import { DiffView, HunkDiff, Stat, type HunkAction } from "./DiffView";
import { FilesPanel } from "./FilesPanel";
import { Timeline } from "./Timeline";
import { TerminalPanel } from "./TerminalPanel";
import { Button, Spinner, Tip } from "./ui";

const STATUS: Record<string, { label: string; cls: string }> = {
  modified: { label: "M", cls: "text-warn" },
  added: { label: "A", cls: "text-ok" },
  deleted: { label: "D", cls: "text-danger" },
  renamed: { label: "R", cls: "text-accent" },
};

function FileRow({ f, cwd, threadId, snapshot, onChanged }: { f: DiffFile; cwd: string; threadId?: string; snapshot: boolean; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [hunkBusy, setHunkBusy] = useState<string | null>(null);
  const s = useApp();
  const st = STATUS[f.status] ?? STATUS.modified;
  const name = f.path.split("/").pop();
  const dir = f.path.includes("/") ? f.path.slice(0, f.path.lastIndexOf("/")) : "";
  const hunks = f.hunks ?? [];
  const allAccepted = hunks.length > 0 && pendingCount(hunks) === 0;
  const onHunk = async (h: HunkInfo, action: HunkAction) => {
    if (action === "reject" && !confirm(`撤回这块改动（${h.header}）？文件里的这一块会改回原样。`)) return;
    setHunkBusy(h.id);
    try {
      const r = await s.rpc!.call("review/hunk", { cwd, path: f.path, hunk_id: h.id, action, thread_id: threadId });
      if (action === "reject") s.showToast(r.action === "deleted" ? `已删除新文件 ${f.path}` : "已撤回这块改动");
      onChanged();
    } catch (e) {
      s.showToast((e as Error).message, "error");
    } finally {
      setHunkBusy(null);
    }
  };
  const revert = async () => {
    const msg =
      f.status === "added"
        ? `删除新文件 ${f.path}？`
        : snapshot
          ? `把 ${f.path} 恢复成这个对话开始时的版本？之后的改动会丢失。`
          : `把 ${f.path} 恢复成最近一次提交的版本？未提交的改动会丢失。`;
    if (!confirm(msg)) return;
    setBusy(true);
    try {
      await s.rpc!.call("git/revert_file", { cwd, path: f.path, thread_id: threadId });
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
        {allAccepted && (
          <Tip label="所有改动块都已接受">
            <Check size={13} className="shrink-0 text-ok" />
          </Tip>
        )}
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
          {f.binary ? (
            <div className="px-4 py-3 text-[12px] text-faint">二进制或过大的文件，不显示内容</div>
          ) : hunks.length ? (
            <HunkDiff patch={f.patch} hunks={hunks} busy={hunkBusy} onAction={onHunk} />
          ) : (
            <DiffView patch={f.patch} />
          )}
        </div>
      )}
    </div>
  );
}

type ReviewView = "changes" | "timeline";

function Review() {
  const s = useApp();
  const d = s.activeThreadId ? s.details[s.activeThreadId] : null;
  const cwd = d?.cwd ?? s.activeWorkspace;
  const threadId = d?.id;
  const [data, setData] = useState<GitReview | null>(null);
  const [loading, setLoading] = useState(false);
  const [view, setView] = useState<ReviewView>(() => (localStorage.getItem("ee.reviewView") === "timeline" ? "timeline" : "changes"));

  // 回合中改文件会频繁触发刷新、也可能切换项目：只认最后一次请求的结果
  const gen = useRef(0);
  const load = useCallback(async () => {
    if (!cwd || !s.rpc) return;
    const tok = ++gen.current;            // 切对话/项目后，迟到的旧结果不覆盖
    setLoading(true);
    try {
      const r = await s.rpc.call("git/diff", { cwd, thread_id: threadId });
      if (tok === gen.current) setData(r);
    } catch (e) {
      if (tok !== gen.current) return;
      setData(null);
      s.showToast((e as Error).message, "error");
    } finally {
      if (tok === gen.current) setLoading(false);
    }
  }, [cwd, threadId, s.rpc]);

  useEffect(() => {
    if (view === "changes") load();
  }, [load, s.reviewNonce, view]);

  const pick = (v: ReviewView) => {
    setView(v);
    localStorage.setItem("ee.reviewView", v);
  };
  const snap = !!data && !data.is_repo && !!data.snapshot?.available && !data.snapshot.empty;
  const listable = !!data && (data.is_repo || snap);

  const undo = async () => {
    if (!d) return;
    await s.undo();       // 冲突确认、恢复点提示都在 store.undo 里
    load();
  };

  if (!cwd) return <div className="p-6 text-center text-[13px] text-faint">先选择一个项目</div>;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex h-10 shrink-0 items-center gap-1.5 overflow-hidden border-b border-line px-2.5 text-[12.5px] text-muted">
        <div className="flex shrink-0 rounded-lg bg-soft p-0.5">
          {(["changes", "timeline"] as const).map((v) => (
            <button
              key={v}
              onClick={() => pick(v)}
              className={clsx("flex h-6 items-center gap-1 rounded-md px-2 text-[12px]", view === v ? "bg-bg text-fg shadow-sm" : "text-muted hover:text-fg")}
            >
              {v === "changes" ? <FileDiff size={12} /> : <History size={12} />}
              {v === "changes" ? "改动" : "时间线"}
            </button>
          ))}
        </div>
        {view === "timeline" ? null : data?.is_repo ? (
          <>
            <GitBranch size={13} className="shrink-0" />
            <span className="min-w-0 truncate">{data.branch}</span>
            <span className="text-faint">·</span>
            <span className="shrink-0 whitespace-nowrap">{data.files.length} 个文件</span>
            {data.files.length > 0 && <Stat add={data.additions ?? 0} del={data.deletions ?? 0} className="shrink-0 whitespace-nowrap" />}
          </>
        ) : snap ? (
          <>
            <Tip label="不是 git 仓库：对比这个对话开始时的快照和现在的文件">
              <span className="truncate">自对话开始 {formatWhen(data?.snapshot?.since)}</span>
            </Tip>
            <span className="text-faint">·</span>
            <span className="shrink-0 whitespace-nowrap">{data!.files.length} 个文件</span>
            {data!.files.length > 0 && <Stat add={data!.additions ?? 0} del={data!.deletions ?? 0} className="shrink-0 whitespace-nowrap" />}
          </>
        ) : (
          <span className="truncate">{data ? "不是 git 仓库" : ""}</span>
        )}
        <span className="flex-1" />
        {d && (
          <Tip label="撤销 ee 上一回合写过的文件">
            <Button size="icon-sm" onClick={undo} disabled={d.status !== "idle"}>
              <Undo2 size={14} />
            </Button>
          </Tip>
        )}
        {view === "changes" && data?.is_repo && data.files.length > 0 && (
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
          <Button size="icon-sm" onClick={() => (view === "changes" ? load() : useApp.setState((st) => ({ reviewNonce: st.reviewNonce + 1 })))}>
            {loading ? <Spinner size={12} /> : <RefreshCw size={13} />}
          </Button>
        </Tip>
      </div>
      <div className="flex-1 overflow-y-auto">
        {view === "timeline" ? (
          <Timeline />
        ) : (
          <>
            {data && !listable && (
              <div className="px-6 py-10 text-center text-[12.5px] leading-relaxed text-faint">
                {!data.git_available
                  ? "没有找到 git。安装 Git for Windows 后可以审阅改动、使用检查点。"
                  : data.snapshot?.empty
                    ? "这个文件夹不是 git 仓库。发出第一条消息后，这里会显示从对话开始到现在改过的文件。"
                    : data.snapshot && !data.snapshot.available
                      ? `这个文件夹不是 git 仓库，也没有快照（${data.snapshot.reason}）。`
                      : "这个文件夹不是 git 仓库。打开一个对话后，可以对比对话开始时的快照。"}
                <br />
                仍可用右上角的撤销按钮回退 ee 上一回合的改动。
              </div>
            )}
            {listable && data!.files.length === 0 && (
              <div className="flex flex-col items-center px-6 py-14 text-center text-[12.5px] text-faint">
                <FileDiff size={28} className="mb-3 opacity-50" />
                {snap ? "从对话开始到现在没有改动文件" : "工作区没有未提交的改动"}
              </div>
            )}
            {listable && data!.files.map((f) => <FileRow key={f.path} f={f} cwd={cwd} threadId={threadId} snapshot={snap} onChanged={load} />)}
          </>
        )}
      </div>
    </div>
  );
}

const TABS: { id: RightTab; label: string; icon: React.ReactNode }[] = [
  { id: "review", label: "改动", icon: <FileDiff size={14} /> },
  { id: "terminal", label: "终端", icon: <SquareTerminal size={14} /> },
  { id: "files", label: "文件", icon: <FileCode2 size={14} /> },
];

const MIN_W = INSPECTOR_MIN;
const MAX_W = INSPECTOR_MAX;

export function RightPanel() {
  const s = useApp();
  // 宽度由 layoutStore 统一持有（外层 AppShell 决定实际占位/覆盖），这里只负责拖动改偏好。
  const width = useLayout((st) => st.prefs.inspector);
  const setInspectorWidth = useLayout((st) => st.setInspectorWidth);
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
    const max = Math.min(MAX_W, window.innerWidth - CONTENT_MIN);
    setInspectorWidth(Math.round(Math.max(MIN_W, Math.min(max, drag.current.w + drag.current.x - e.clientX))));
  };
  const onUp = () => {
    if (!drag.current) return;
    drag.current = null;
  };

  return (
    <aside className="relative flex h-full w-full shrink-0 flex-col bg-bg">
      <div
        className="no-drag absolute -left-[3px] top-0 z-20 h-full w-[6px] cursor-col-resize transition-colors hover:bg-accent/30"
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onDoubleClick={() => setInspectorWidth(INSPECTOR_DEFAULT)}
      />
      {/* 顶部标签条不再是标题栏的一部分：工具面板在自己的栏内，右侧安全区由外层标题栏负责 */}
      <div className="flex h-10 shrink-0 items-center gap-1 border-b border-line pl-2 pr-2">
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
