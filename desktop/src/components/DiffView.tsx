import clsx from "clsx";
import { Check, Undo2, X } from "lucide-react";
import { useMemo } from "react";
import { hunkPatch, splitPatch } from "../lib/hunks";
import { parseDiff } from "../lib/reduce";
import type { HunkInfo } from "../types";
import { Spinner } from "./ui";

export function DiffView({ patch, maxHeight, showMeta = false, hideHunkHeader = false }: { patch: string; maxHeight?: number; showMeta?: boolean; hideHunkHeader?: boolean }) {
  const lines = useMemo(() => parseDiff(patch), [patch]);
  const shown = lines.filter((l) => (showMeta || l.kind !== "meta") && !(hideHunkHeader && l.kind === "hunk"));
  if (!shown.length) return <div className="px-3 py-2 text-[12px] text-faint">（没有可显示的内容）</div>;
  return (
    <div className="mono overflow-auto text-[12px] leading-[1.6]" style={maxHeight ? { maxHeight } : undefined}>
      <table className="w-full border-collapse">
        <tbody>
          {shown.map((l, i) =>
            l.kind === "hunk" ? (
              <tr key={i} className="bg-soft text-faint">
                <td colSpan={3} className="select-none px-3 py-0.5 text-[11.5px]">
                  {l.text}
                </td>
              </tr>
            ) : (
              <tr
                key={i}
                className={clsx(
                  l.kind === "add" && "bg-add-bg",
                  l.kind === "del" && "bg-del-bg",
                  l.kind === "meta" && "text-faint",
                )}
              >
                <td className="w-[1%] select-none whitespace-nowrap px-2 text-right text-[11px] text-faint opacity-70">
                  {l.oldNo ?? ""}
                </td>
                <td className="w-[1%] select-none whitespace-nowrap px-2 text-right text-[11px] text-faint opacity-70">
                  {l.newNo ?? ""}
                </td>
                <td
                  className={clsx(
                    "whitespace-pre-wrap break-all pr-3",
                    l.kind === "add" && "text-add",
                    l.kind === "del" && "text-del",
                  )}
                >
                  <span className="mr-2 select-none opacity-60">{l.kind === "add" ? "+" : l.kind === "del" ? "-" : " "}</span>
                  {l.text}
                </td>
              </tr>
            ),
          )}
        </tbody>
      </table>
    </div>
  );
}

export function Stat({ add, del, className }: { add: number; del: number; className?: string }) {
  return (
    <span className={clsx("mono text-[12px]", className)}>
      <span className="text-add">+{add}</span> <span className="text-del">-{del}</span>
    </span>
  );
}

export type HunkAction = "accept" | "unaccept" | "reject";

/** 按块显示 diff，每块有「接受」「拒绝」；已接受的块收起。块数和后端 hunks 对不上时退回普通 DiffView。 */
export function HunkDiff({
  patch,
  hunks,
  busy,
  onAction,
}: {
  patch: string;
  hunks: HunkInfo[];
  busy?: string | null;
  onAction: (h: HunkInfo, action: HunkAction) => void;
}) {
  const parts = useMemo(() => splitPatch(patch).hunks, [patch]);
  if (parts.length !== hunks.length || !hunks.length) return <DiffView patch={patch} />;
  return (
    <div>
      {hunks.map((h, i) => (
        <div key={h.id + i} className="border-b border-line last:border-b-0" data-hunk={i}>
          <div className={clsx("flex h-7 items-center gap-2 bg-soft px-3 text-[11.5px]", h.accepted ? "text-faint" : "text-muted")}>
            <span className="mono min-w-0 flex-1 truncate">{parts[i].header}</span>
            {busy === h.id ? (
              <Spinner size={12} />
            ) : h.accepted ? (
              <>
                <span className="flex items-center gap-1 text-ok">
                  <Check size={12} />
                  已接受
                </span>
                <button className="flex items-center gap-1 rounded px-1.5 py-0.5 hover:bg-hover hover:text-fg" onClick={() => onAction(h, "unaccept")} title="取消接受，重新显示这块">
                  <Undo2 size={12} />
                  撤销
                </button>
              </>
            ) : (
              <>
                <button
                  className="flex items-center gap-1 rounded px-1.5 py-0.5 text-ok hover:bg-add-bg"
                  onClick={() => onAction(h, "accept")}
                  title="保留这块改动，从待审列表里收起"
                  disabled={!!busy}
                >
                  <Check size={12} />
                  接受
                </button>
                <button
                  className="flex items-center gap-1 rounded px-1.5 py-0.5 text-danger hover:bg-del-bg"
                  onClick={() => onAction(h, "reject")}
                  title="撤回这块改动（把文件里的这一块改回原样）"
                  disabled={!!busy}
                >
                  <X size={12} />
                  拒绝
                </button>
              </>
            )}
          </div>
          {!h.accepted && <DiffView patch={hunkPatch(patch, i)} hideHunkHeader />}
        </div>
      ))}
    </div>
  );
}
