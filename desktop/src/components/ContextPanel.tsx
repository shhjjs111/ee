// 上下文圆环 + 点开后的「上下文构成」面板（A12，RPC thread/context_breakdown）。
import * as DM from "@radix-ui/react-dropdown-menu";
import { useState } from "react";
import { fmtTokens, savingsLine, thresholdLine, usageLine } from "../lib/context";
import { useApp } from "../lib/store";
import type { ContextBreakdown } from "../types";
import { Button, Spinner } from "./ui";

export function ContextBreakdownView({ data, onCompact, canCompact }: { data: ContextBreakdown; onCompact?: () => void; canCompact?: boolean }) {
  const [showTools, setShowTools] = useState(false);
  const max = Math.max(1, ...data.items.map((x) => x.tokens));
  const saved = savingsLine(data);
  const th = thresholdLine(data);
  return (
    <div className="w-[320px] space-y-2.5 p-1 text-[12.5px]">
      <div>
        <div className="text-[13px] font-medium text-fg">上下文构成</div>
        <div className="mt-0.5 text-faint">{usageLine(data)}</div>
      </div>
      <div className="space-y-1">
        {data.items.map((x) => (
          <div key={x.key} data-key={x.key}>
            <div className="flex items-baseline gap-2">
              <span className="flex-1 text-muted">
                {x.label}
                {x.key === "tools" && data.tools.length > 0 && (
                  <button className="ml-1.5 text-[11.5px] text-accent hover:underline" onClick={() => setShowTools(!showTools)}>
                    {showTools ? "收起" : `${data.tools.length} 个`}
                  </button>
                )}
                {x.key === "images" && data.images > 0 && <span className="ml-1 text-faint">{data.images} 张</span>}
              </span>
              <span className="mono text-fg/85">{fmtTokens(x.tokens)}</span>
              <span className="mono w-11 text-right text-faint">{x.percent.toFixed(x.percent < 10 ? 1 : 0)}%</span>
            </div>
            <div className="mt-0.5 h-1 overflow-hidden rounded-full bg-soft">
              <div className="h-full rounded-full bg-[var(--fg-muted)]" style={{ width: `${(100 * x.tokens) / max}%`, opacity: 0.55 }} />
            </div>
            {x.key === "tools" && showTools && (
              <div className="mt-1 space-y-0.5 border-l-2 border-line pl-2">
                {data.tools.map((t) => (
                  <div key={t.name} className="flex text-[11.5px] text-faint">
                    <span className="mono flex-1 truncate">{t.name}</span>
                    <span className="mono">{fmtTokens(t.tokens)}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
      {saved && <div className="text-muted">{saved}</div>}
      <div className="text-[11.5px] leading-relaxed text-faint">
        各项为按字符估算{data.actual ? "，顶部占用取模型返回的实际用量" : ""}。{th ? `${th}。` : ""}
        {!data.frozen && "会话开始后系统提示和工具定义固定不变（保住缓存）。"}
      </div>
      {onCompact && (
        <div className="flex justify-end">
          <Button onClick={onCompact} disabled={!canCompact}>
            立即压缩（/compact）
          </Button>
        </div>
      )}
    </div>
  );
}

export function ContextRing({ threadId, left, onCompact, compacting, canCompact }: { threadId: string | null; left: number | null; onCompact: () => void; compacting: boolean; canCompact: boolean }) {
  const rpc = useApp((s) => s.rpc);
  const [data, setData] = useState<ContextBreakdown | null>(null);
  const [err, setErr] = useState<string | null>(null);
  if (compacting) {
    return (
      <span className="flex h-7 items-center gap-1.5 px-1.5 text-[12px] text-muted">
        <Spinner size={12} /> 正在压缩上下文…
      </span>
    );
  }
  if (!threadId || left === null || left === undefined) return null;
  const used = Math.max(0, 1 - left);
  const r = 7;
  const c = 2 * Math.PI * r;
  const load = (open: boolean) => {
    if (!open || !rpc) return;
    setErr(null);
    rpc
      .call("thread/context_breakdown", { thread_id: threadId })
      .then((d: ContextBreakdown) => setData(d))
      .catch((e: Error) => setErr(e.message));
  };
  return (
    <DM.Root onOpenChange={load}>
      <DM.Trigger asChild>
        <button
          title={`上下文剩余 ${Math.round(left * 100)}% · 点击查看构成`}
          className="flex h-7 w-7 items-center justify-center rounded-lg outline-none hover:bg-hover"
        >
          <svg width="18" height="18" viewBox="0 0 18 18" className="text-faint">
            <circle cx="9" cy="9" r={r} fill="none" stroke="currentColor" strokeOpacity="0.3" strokeWidth="2" />
            <circle
              cx="9"
              cy="9"
              r={r}
              fill="none"
              stroke={used > 0.85 ? "var(--danger)" : "var(--fg-muted)"}
              strokeWidth="2"
              strokeDasharray={`${c * used} ${c}`}
              transform="rotate(-90 9 9)"
              strokeLinecap="round"
            />
          </svg>
        </button>
      </DM.Trigger>
      <DM.Portal>
        <DM.Content side="top" align="end" sideOffset={6} className="z-50 rounded-xl border border-line bg-elev p-2.5 shadow-[var(--shadow)] fadein">
          {err ? (
            <div className="w-[260px] text-[12.5px] text-danger">读取上下文构成失败：{err}</div>
          ) : data ? (
            <ContextBreakdownView data={data} onCompact={onCompact} canCompact={canCompact} />
          ) : (
            <div className="flex w-[260px] items-center gap-2 text-[12.5px] text-muted">
              <Spinner size={12} /> 正在统计…
            </div>
          )}
        </DM.Content>
      </DM.Portal>
    </DM.Root>
  );
}
