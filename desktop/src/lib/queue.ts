// 运行中排队的消息（纯函数，便于单测）：插话 = 当前步骤结束后送入；待追加 = 这一轮做完后再送入。
import type { Item, UserItem } from "../types";

export type QueueKind = "steer" | "follow";

export const QUEUE_LABEL: Record<QueueKind, string> = { steer: "插话", follow: "待追加" };
/** 已送入的消息在对话里的小标签 */
export const DELIVERED_LABEL: Record<QueueKind, string> = { steer: "运行中插话", follow: "完成后追加" };
export const QUEUE_HINT: Record<QueueKind, string> = {
  steer: "当前步骤结束后送给 ee",
  follow: "等这一轮做完再送给 ee",
};

export function isQueued(it: Item): it is UserItem {
  return it.type === "user" && !!(it as UserItem).queued;
}

/** 对话区只显示已送入的；还在排队的放到输入框上方的托盘里（按发送顺序）。 */
export function splitQueued(items: Item[]): { visible: Item[]; queued: UserItem[] } {
  const visible: Item[] = [];
  const queued: UserItem[] = [];
  for (const it of items) (isQueued(it) ? queued : visible).push(it);
  return { visible, queued };
}

/** 运行中按键决定排进哪条队列：Ctrl/⌘+Enter = 完成后再做，Enter = 插话。 */
export function queueKindForKey(e: { ctrlKey?: boolean; metaKey?: boolean }): QueueKind {
  return e.ctrlKey || e.metaKey ? "follow" : "steer";
}

/** 被退回 / 撤回的消息放回输入框：排在前面，输入框里正在写的接在后面。 */
export function mergeDraft(returned: string[], current: string): string {
  const parts = returned.map((t) => t.trim()).filter(Boolean);
  if (current.trim()) parts.push(current);
  return parts.join("\n\n");
}
