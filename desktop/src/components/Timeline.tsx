// 检查点时间线（D5-7）：每回合自动存的检查点，点开看这一回合改了什么，可以把文件恢复到回合前/后。
import clsx from "clsx";
import { ChevronRight, History, RotateCcw } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { formatWhen } from "../lib/hunks";
import { useApp } from "../lib/store";
import type { Checkpoint, CheckpointList, DiffFile } from "../types";
import { DiffView, Stat } from "./DiffView";
import { Button, Spinner } from "./ui";
import { partialToast } from "../lib/store";

type DiffResult = { files: DiffFile[]; additions: number; deletions: number };

function FileList({ files }: { files: DiffFile[] }) {
  const [open, setOpen] = useState<string | null>(files.length === 1 ? files[0].path : null);
  if (!files.length) return <div className="px-3 py-2 text-[12px] text-faint">这一回合没有改动文件</div>;
  return (
    <div className="border-t border-line">
      {files.map((f) => (
        <div key={f.path} className="border-b border-line last:border-b-0">
          <div className="flex h-8 cursor-default items-center gap-2 px-3 text-[12.5px] hover:bg-hover" onClick={() => setOpen(open === f.path ? null : f.path)}>
            <ChevronRight size={12} className={clsx("shrink-0 text-faint transition-transform", open === f.path && "rotate-90")} />
            <span className="min-w-0 flex-1 truncate">{f.path}</span>
            <Stat add={f.additions} del={f.deletions} className="shrink-0" />
          </div>
          {open === f.path && (
            <div className="border-t border-line bg-code">
              {f.binary ? <div className="px-4 py-3 text-[12px] text-faint">二进制或过大的文件，不显示内容</div> : <DiffView patch={f.patch} maxHeight={420} />}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function Row({
  cp,
  threadId,
  idle,
  onRestored,
}: {
  cp: Checkpoint;
  threadId: string;
  idle: boolean;
  onRestored: () => void;
}) {
  const s = useApp();
  const [open, setOpen] = useState(false);
  const [diff, setDiff] = useState<DiffResult | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const isTurn = cp.kind === "turn";

  useEffect(() => {
    if (!open || diff || !isTurn || cp.expired) return;
    let live = true;
    s.rpc!
      .call("checkpoint/diff", { thread_id: threadId, checkpoint_id: cp.id, mode: "turn" })
      .then((r: DiffResult) => live && setDiff(r))
      .catch((e: Error) => live && setErr(e.message));
    return () => {
      live = false;
    };
  }, [open, diff, isTurn, cp.id, cp.expired, threadId, s.rpc]);

  const restore = async (side: "before" | "after") => {
    setBusy(true);
    try {
      const mode = isTurn ? side : "before";
      const plan: DiffResult = await s.rpc!.call("checkpoint/diff", { thread_id: threadId, checkpoint_id: cp.id, mode });
      if (!plan.files.length) {
        s.showToast("文件已经是这个状态了");
        return;
      }
      const names = plan.files.slice(0, 6).map((f) => "  " + f.path).join("\n") + (plan.files.length > 6 ? `\n  …等 ${plan.files.length} 个` : "");
      const what = isTurn ? `第 ${cp.turn} 回合${side === "before" ? "之前" : "之后"}` : "这个状态";
      const ok = confirm(
        `把文件恢复到${what}？将改动 ${plan.files.length} 个文件：\n${names}\n\n` +
          "恢复前的文件会自动存成一个检查点，随时可以再恢复回来。\n只恢复文件，不改对话记录；被 .gitignore 忽略的文件和大于 2MB 的新文件不在快照里，不会变。",
      );
      if (!ok) return;
      const r = await s.rpc!.call("checkpoint/restore", { thread_id: threadId, checkpoint_id: cp.id, side });
      if (r.partial) s.showToast(partialToast(r.restored.length, r.failed ?? []), "error");
      else s.showToast(r.restored.length ? `已恢复到${r.label}（${r.restored.length} 个文件）` : "文件已经是这个状态了");
      onRestored();
    } catch (e) {
      s.showToast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={clsx("border-b border-line", cp.undone && "opacity-70")}>
      <div className="flex cursor-default gap-2 px-3 py-2 hover:bg-hover" onClick={() => setOpen(!open)}>
        <ChevronRight size={13} className={clsx("mt-[3px] shrink-0 text-faint transition-transform", open && "rotate-90")} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 text-[12.5px]">
            {isTurn ? (
              <span className="font-medium text-fg">第 {cp.turn} 回合</span>
            ) : (
              <span className="flex items-center gap-1 font-medium text-accent">
                <RotateCcw size={12} />
                恢复点
              </span>
            )}
            <span className="text-[11.5px] text-faint">{formatWhen(cp.ts)}</span>
            {cp.running && <span className="rounded bg-soft px-1.5 text-[11px] text-warn">进行中</span>}
            {cp.undone && <span className="rounded bg-soft px-1.5 text-[11px] text-faint">已撤销</span>}
            {cp.expired && <span className="rounded bg-soft px-1.5 text-[11px] text-faint">已清理</span>}
            <span className="flex-1" />
            {cp.stat && (cp.stat.files ? <Stat add={cp.stat.additions} del={cp.stat.deletions} className="shrink-0" /> : <span className="text-[11.5px] text-faint">无改动</span>)}
          </div>
          <div className="mt-0.5 line-clamp-2 break-all text-[12px] leading-snug text-muted">{cp.text || "（空消息）"}</div>
        </div>
      </div>
      {open && (
        <div className="bg-bg">
          <div className="flex flex-wrap items-center gap-1.5 px-3 pb-2 pl-8">
            {isTurn ? (
              <>
                <Button size="sm" variant="outline" disabled={!idle || busy || cp.expired} onClick={() => restore("before")}>
                  {busy ? <Spinner size={12} /> : <RotateCcw size={13} />}
                  恢复到这回合之前
                </Button>
                {cp.after && (
                  <Button size="sm" variant="ghost" disabled={!idle || busy || cp.after_expired} onClick={() => restore("after")}>
                    恢复到这回合之后
                  </Button>
                )}
              </>
            ) : (
              <Button size="sm" variant="outline" disabled={!idle || busy || cp.expired} onClick={() => restore("before")}>
                {busy ? <Spinner size={12} /> : <RotateCcw size={13} />}
                恢复到这个状态
              </Button>
            )}
            {!idle && <span className="text-[11.5px] text-faint">运行中不能恢复</span>}
          </div>
          {isTurn &&
            (cp.expired ? (
              <div className="px-3 pb-2 pl-8 text-[12px] text-faint">这个检查点超过保留期限，已被清理</div>
            ) : err ? (
              <div className="px-3 pb-2 pl-8 text-[12px] text-danger">{err}</div>
            ) : diff ? (
              <FileList files={diff.files} />
            ) : (
              <div className="flex items-center gap-2 px-3 pb-2 pl-8 text-[12px] text-faint">
                <Spinner size={12} />
                正在对比…
              </div>
            ))}
          {!isTurn && <div className="px-3 pb-2 pl-8 text-[12px] text-faint">恢复检查点之前自动存下的文件状态，用来反悔。</div>}
        </div>
      )}
    </div>
  );
}

export function Timeline() {
  const s = useApp();
  const d = s.activeThreadId ? s.details[s.activeThreadId] : null;
  const [data, setData] = useState<CheckpointList | null>(null);
  const [loading, setLoading] = useState(false);
  const seq = useRef(0);
  const threadId = d?.id;
  const status = d?.status;

  const load = useCallback(async () => {
    if (!threadId || !s.rpc) return;
    const my = ++seq.current;
    setLoading(true);
    try {
      const r: CheckpointList = await s.rpc.call("checkpoint/list", { thread_id: threadId });
      if (my === seq.current) setData(r);
    } catch (e) {
      if (my === seq.current) {
        setData(null);
        s.showToast((e as Error).message, "error");
      }
    } finally {
      if (my === seq.current) setLoading(false);
    }
  }, [threadId, s.rpc]);

  useEffect(() => {
    setData(null);
  }, [threadId]);
  useEffect(() => {
    load();
  }, [load, s.reviewNonce, status]);

  if (!d) return <div className="px-6 py-10 text-center text-[12.5px] leading-relaxed text-faint">开始对话后，每一回合都会自动存一个检查点</div>;
  if (!data) return <div className="flex justify-center py-10">{loading && <Spinner />}</div>;
  if (!data.available)
    return <div className="px-6 py-10 text-center text-[12.5px] leading-relaxed text-faint">这个工作区没有检查点：{data.reason}</div>;
  if (!data.checkpoints.length)
    return (
      <div className="flex flex-col items-center px-6 py-14 text-center text-[12.5px] text-faint">
        <History size={28} className="mb-3 opacity-50" />
        还没有检查点。每一回合开始前和结束后会自动记录工作区文件。
      </div>
    );
  const list = [...data.checkpoints].reverse();
  return (
    <div>
      {list.map((cp) => (
        <Row
          key={cp.id}
          cp={cp}
          threadId={d.id}
          idle={d.status === "idle"}
          onRestored={() => {
            load();
            useApp.setState((st) => ({ reviewNonce: st.reviewNonce + 1 }));
          }}
        />
      ))}
    </div>
  );
}
