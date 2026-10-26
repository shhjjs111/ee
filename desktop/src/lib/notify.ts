// 桌面通知（D4-5）：根据后端事件决定要不要弹 Windows 通知、弹什么。纯函数，便于测试。
// 真正弹不弹（窗口在不在前台、设置是 从不/不在前台/总是）由 Electron 主进程判断。
import type { Item, ThreadStatus } from "../types";

export type NotifyKind = "done" | "error" | "loop" | "approval";
export type NotifyMsg = { kind: NotifyKind; threadId: string; title: string; body: string };

const REASON: Record<string, { kind: NotifyKind; head: string } | undefined> = {
  completed: { kind: "done", head: "已完成" },
  done: { kind: "done", head: "已完成" },
  max_steps: { kind: "done", head: "已达到最大步数，已停下" },
  error: { kind: "error", head: "出错了" },
  loop: { kind: "loop", head: "检测到重复操作，已停止" },
};

/** 取最后一条助手回复的开头当通知正文（去掉 markdown 记号、压成一行）。 */
export function lastAssistantSnippet(items: Item[] | undefined, max = 120): string {
  if (!items) return "";
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i] as any;
    if (it.type === "user") break;
    if (it.type === "assistant" && typeof it.text === "string" && it.text.trim()) {
      const t = it.text
        .replace(/```[\s\S]*?```/g, " ")
        .replace(/`([^`]*)`/g, "$1")
        .replace(/^\s*(#+|>|[-*]\s)\s*/gm, "")
        .replace(/[*_~]+/g, "")
        .replace(/\s+/g, " ")
        .trim();
      return t.length > max ? t.slice(0, max - 1) + "…" : t;
    }
  }
  return "";
}

/** turn/completed：interrupted（用户自己停的）不通知；其他按原因给标题。 */
export function turnCompletedNotice(p: { thread_id: string; reason: string }, threadTitle: string, items?: Item[]): NotifyMsg | null {
  const r = REASON[p.reason];
  if (!r) return null;
  const name = threadTitle || "对话";
  const snippet = r.kind === "done" ? lastAssistantSnippet(items) : "";
  let body = snippet;
  if (!body && r.kind === "error") {
    const err = [...(items ?? [])].reverse().find((x) => x.type === "error") as any;
    body = err?.message ? String(err.message).slice(0, 160) : "点开查看详情";
  }
  return { kind: r.kind, threadId: p.thread_id, title: `${name} · ${r.head}`, body: body || "点开查看结果" };
}

/** 会话状态从非审批变成 waiting_approval 时通知一次。 */
export function approvalNotice(threadId: string, prev: ThreadStatus | undefined, next: ThreadStatus, threadTitle: string, items?: Item[]): NotifyMsg | null {
  if (next !== "waiting_approval" || prev === "waiting_approval") return null;
  const ap = [...(items ?? [])].reverse().find((x) => x.type === "approval" && (x as any).status === "pending") as any;
  const body = ap?.title ? String(ap.title).slice(0, 160) : "有操作在等你批准";
  return { kind: "approval", threadId, title: `${threadTitle || "对话"} · 需要你审批`, body };
}
