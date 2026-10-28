import { clsx } from "clsx";
import { GitBranch, X } from "lucide-react";
import type { ThreadSummary } from "../types";
import { Spinner, Tip } from "./ui";

/** 终态原因 → 中文。只看末条 run 的 terminal_reason：
 *
 *  `status` 的 idle 同时覆盖「跑完了」和「被杀/超预算」，单看它会把失败的子任务显示成完成。
 *  没有 run 记录时也不能报完成——没有证据说明它跑过。
 */
export function terminalText(t: ThreadSummary): string {
  if (t.status === "running") return "运行中";
  if (t.status === "waiting_approval") return "等待批准";
  if (t.status === "compacting") return "压缩中";
  const run = (t.runs ?? []).at(-1);
  switch (run?.terminal_reason) {
    case "completed":
      return "完成";
    case "budget":
      return "超预算停止";
    case "interrupted":
      return "已中断";
    case "error":
      return "出错";
    case "max_steps":
      return "步数上限";
    case "loop":
      return "循环停止";
    case "blocked":
      return "被拦截";
    default:
      return "空闲";
  }
}

/** 终态的颜色：成功绿、需注意黄、失败红、其余灰。 */
function tone(t: ThreadSummary): string {
  const r = (t.runs ?? []).at(-1)?.terminal_reason;
  if (t.status === "waiting_approval") return "text-warn";
  if (t.status === "running") return "text-muted";
  if (r === "completed") return "text-ok";
  if (r === "budget" || r === "max_steps" || r === "loop") return "text-warn";
  if (r === "error" || r === "blocked") return "text-danger";
  return "text-faint";
}

/** 子会话卡片（D6-1 U8）：父会话里看得见「哪个子任务失败/超预算」，不必点进去才发现。 */
export function ChildThreadCard({
  t,
  onOpen,
  onDismiss,
}: {
  t: ThreadSummary;
  onOpen: (id: string) => void;
  onDismiss?: (id: string) => void;
}) {
  const branch = t.worktree?.branch;
  return (
    <div
      className="group relative ml-7 flex flex-col gap-1 rounded-lg border border-line bg-bg/40 px-2 py-1.5 text-[12px]"
      data-testid="child-thread-card"
    >
      <div className="flex items-center gap-1.5">
        {t.status === "running" ? (
          <Spinner size={11} className="shrink-0 text-muted" />
        ) : (
          <span className={clsx("h-1.5 w-1.5 shrink-0 rounded-full", tone(t).replace("text-", "bg-"))} />
        )}
        <span className="min-w-0 flex-1 truncate text-fg" title={t.title}>
          {t.title}
        </span>
        <span className={clsx("shrink-0 text-[11px]", tone(t))}>{terminalText(t)}</span>
        {onDismiss && (
          <button
            className="hidden h-5 w-5 shrink-0 items-center justify-center rounded text-faint hover:bg-hover hover:text-fg group-hover:flex"
            onClick={() => onDismiss(t.id)}
            title="从列表移除（不删除会话）"
          >
            <X size={11} />
          </button>
        )}
      </div>
      <div className="flex items-center gap-2 text-[11px] text-faint">
        {branch && (
          <Tip label={`worktree 分支：${branch}`}>
            <span className="flex items-center gap-1 truncate">
              <GitBranch size={10} className="shrink-0 text-accent" />
              <span className="truncate">{branch}</span>
            </span>
          </Tip>
        )}
        {typeof t.changed_count === "number" && t.changed_count > 0 && <span>{t.changed_count} 处改动</span>}
        <button
          className="ml-auto shrink-0 rounded px-1.5 py-0.5 text-accent hover:bg-hover"
          onClick={() => onOpen(t.id)}
        >
          打开
        </button>
      </div>
    </div>
  );
}
