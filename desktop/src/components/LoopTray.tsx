// 会话内 /loop（D5-3）：输入框上方列出本会话的循环（调度、提示词、下次 / 等待中），可停止或「转为自动化」。
import { CalendarClock, Repeat, X } from "lucide-react";
import { useEffect, useState } from "react";
import { loopStatus, type SessionLoop } from "../lib/loop";
import { Tip } from "./ui";

export function LoopTray({ loops, onStop, onConvert }: { loops: SessionLoop[]; onStop: (id: string) => void; onConvert: (lp: SessionLoop) => void }) {
  const [now, setNow] = useState(Date.now() / 1000);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now() / 1000), 30000);
    return () => clearInterval(t);
  }, []);
  if (!loops.length) return null;
  return (
    <div className="flex flex-col gap-1 border-b border-line px-3 py-2" data-testid="loop-tray">
      {loops.map((lp) => (
        <div key={lp.id} className="group/l flex min-w-0 items-center gap-2 text-[12.5px]">
          <Tip label="会话内循环：只在 ee 开着时运行，关闭应用后不保留">
            <span className="flex shrink-0 items-center gap-1 rounded-md bg-accent/15 px-1.5 py-0.5 text-[11px] font-medium text-accent">
              <Repeat size={11} /> #{lp.id} {lp.schedule_text}
            </span>
          </Tip>
          <span className="min-w-0 flex-1 truncate text-muted" title={lp.prompt}>
            {lp.prompt}
            <span className="ml-2 text-faint">{loopStatus(lp, now)}</span>
          </span>
          <Tip label="转为自动化：持久保存，ee 重启后也按计划运行（每次新建对话）">
            <button
              aria-label="转为自动化"
              onClick={() => onConvert(lp)}
              className="flex h-6 shrink-0 items-center gap-1 rounded-md px-1.5 text-[11.5px] text-faint hover:bg-hover hover:text-fg"
            >
              <CalendarClock size={12} /> 转为自动化
            </button>
          </Tip>
          <Tip label={`停止循环 #${lp.id}`}>
            <button
              aria-label="停止循环"
              onClick={() => onStop(lp.id)}
              className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-faint hover:bg-hover hover:text-fg"
            >
              <X size={13} />
            </button>
          </Tip>
        </div>
      ))}
    </div>
  );
}
