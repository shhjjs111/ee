import clsx from "clsx";
import { useMemo } from "react";
import { parseDiff } from "../lib/reduce";

export function DiffView({ patch, maxHeight, showMeta = false }: { patch: string; maxHeight?: number; showMeta?: boolean }) {
  const lines = useMemo(() => parseDiff(patch), [patch]);
  const shown = showMeta ? lines : lines.filter((l) => l.kind !== "meta");
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
