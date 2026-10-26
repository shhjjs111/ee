// 运行中排队的消息：输入框上方的托盘（插话 / 待追加，可撤回），以及运行中的发送按钮（带下拉选择排进哪条队列）。
import clsx from "clsx";
import { ArrowUp, ChevronDown, CornerDownLeft, ListPlus, Undo2 } from "lucide-react";
import { QUEUE_HINT, QUEUE_LABEL, type QueueKind } from "../lib/queue";
import type { UserItem } from "../types";
import { Menu, Tip } from "./ui";

export function QueueTray({ items, onWithdraw }: { items: UserItem[]; onWithdraw: (id: string) => void }) {
  if (!items.length) return null;
  return (
    <div className="flex flex-col gap-1 border-b border-line px-3 py-2" data-testid="queue-tray">
      {items.map((it) => {
        const kind: QueueKind = it.queue === "follow" ? "follow" : "steer";
        return (
          <div key={it.id} className="group/q flex min-w-0 items-center gap-2 text-[12.5px]" data-queue={kind}>
            <Tip label={QUEUE_HINT[kind]}>
              <span
                className={clsx(
                  "shrink-0 rounded-md px-1.5 py-0.5 text-[11px] font-medium",
                  kind === "steer" ? "bg-accent/15 text-accent" : "bg-soft text-muted",
                )}
              >
                {QUEUE_LABEL[kind]}
              </span>
            </Tip>
            <span className="min-w-0 flex-1 truncate text-muted" title={it.text}>
              {it.text}
            </span>
            <Tip label="撤回到输入框">
              <button
                aria-label="撤回"
                onClick={() => onWithdraw(it.id)}
                className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-faint opacity-60 hover:bg-hover hover:text-fg group-hover/q:opacity-100"
              >
                <Undo2 size={13} />
              </button>
            </Tip>
          </div>
        );
      })}
    </div>
  );
}

/** 运行中的发送：主按钮 = 插话（Enter）；下拉里可选「完成后再做」（Ctrl+Enter）。 */
export function QueueSendButton({ disabled, onSend }: { disabled: boolean; onSend: (kind: QueueKind) => void }) {
  return (
    <div className={clsx("ml-1 flex h-8 items-center rounded-full bg-fg text-bg", disabled && "opacity-25")}>
      <Tip label="插话：当前步骤结束后送入（Enter）">
        <button
          aria-label="插话"
          disabled={disabled}
          onClick={() => onSend("steer")}
          className="flex h-8 w-8 items-center justify-center rounded-l-full transition hover:opacity-85"
        >
          <ArrowUp size={16} strokeWidth={2.4} />
        </button>
      </Tip>
      <Menu
        trigger={
          <button aria-label="选择发送方式" disabled={disabled} className="flex h-8 w-5 items-center justify-center rounded-r-full border-l border-bg/20 pr-0.5 transition hover:opacity-85">
            <ChevronDown size={12} />
          </button>
        }
        items={[
          { label: "插话（Enter）", icon: <CornerDownLeft size={13} />, onSelect: () => onSend("steer") },
          { label: "完成后再做（Ctrl+Enter）", icon: <ListPlus size={13} />, onSelect: () => onSend("follow") },
        ]}
      />
    </div>
  );
}
