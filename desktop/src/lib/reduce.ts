// 纯函数：把后端通知应用到线程详情上（便于单测）。
import type { Item, ThreadDetail } from "../types";

export function applyItemNotification(t: ThreadDetail, method: string, p: any): ThreadDetail {
  switch (method) {
    case "item/started": {
      const it = p.item as Item;
      if (t.items.some((x) => x.id === it.id)) return { ...t, items: t.items.map((x) => (x.id === it.id ? it : x)) };
      return { ...t, items: [...t.items, it] };
    }
    case "item/updated": {
      const it = p.item as Item;
      if (!t.items.some((x) => x.id === it.id)) return { ...t, items: [...t.items, it] };
      return { ...t, items: t.items.map((x) => (x.id === it.id ? it : x)) };
    }
    case "item/delta": {
      const { item_id, field, delta } = p as { item_id: string; field: string; delta: string };
      return {
        ...t,
        items: t.items.map((x) => (x.id === item_id ? ({ ...x, [field]: ((x as any)[field] || "") + delta } as Item) : x)),
      };
    }
    case "item/removed":
      return { ...t, items: t.items.filter((x) => x.id !== p.item_id) };
    case "thread/usage":
      return { ...t, usage: p.usage, context_left: p.context_left };
    case "turn/completed":
      return { ...t, status: "idle", usage: p.usage ?? t.usage, context_left: p.context_left ?? t.context_left };
    default:
      return t;
  }
}

export type Block =
  | { kind: "item"; item: Item }
  | { kind: "explore"; id: string; items: Item[] };

const EXPLORE_TOOLS = new Set(["read", "grep", "glob"]);

export function isExplore(it: Item): boolean {
  if (it.type !== "tool") return false;
  const k = it.meta?.kind;
  if (k === "read" || k === "list" || k === "search") return true;
  return EXPLORE_TOOLS.has(it.name);
}

/** 把连续的读/搜工具合并成一张“已浏览 N 个文件”卡片（Codex 的做法）。 */
export function groupBlocks(items: Item[]): Block[] {
  const out: Block[] = [];
  for (const it of items) {
    if (isExplore(it)) {
      const last = out[out.length - 1];
      if (last && last.kind === "explore") {
        last.items.push(it);
        continue;
      }
      out.push({ kind: "explore", id: `ex_${it.id}`, items: [it] });
      continue;
    }
    out.push({ kind: "item", item: it });
  }
  return out;
}

/** 解析 unified diff 为行，供 diff 视图渲染。 */
export type DiffLine = { kind: "add" | "del" | "ctx" | "hunk" | "meta"; text: string; oldNo?: number; newNo?: number };

export function parseDiff(patch: string): DiffLine[] {
  const lines: DiffLine[] = [];
  let o = 0;
  let n = 0;
  for (const raw of patch.split("\n")) {
    if (raw.startsWith("@@")) {
      const m = raw.match(/@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (m) {
        o = parseInt(m[1], 10);
        n = parseInt(m[2], 10);
      }
      lines.push({ kind: "hunk", text: raw });
    } else if (raw.startsWith("diff --git") || raw.startsWith("index ") || raw.startsWith("---") || raw.startsWith("+++") ||
      raw.startsWith("new file") || raw.startsWith("deleted file") || raw.startsWith("similarity") ||
      raw.startsWith("rename ") || raw.startsWith("old mode") || raw.startsWith("new mode") || raw.startsWith("\\")) {
      lines.push({ kind: "meta", text: raw });
    } else if (raw.startsWith("+")) {
      lines.push({ kind: "add", text: raw.slice(1), newNo: n++ });
    } else if (raw.startsWith("-")) {
      lines.push({ kind: "del", text: raw.slice(1), oldNo: o++ });
    } else {
      lines.push({ kind: "ctx", text: raw.startsWith(" ") ? raw.slice(1) : raw, oldNo: o++, newNo: n++ });
    }
  }
  return lines;
}

export function diffStat(patch: string): { add: number; del: number } {
  let add = 0;
  let del = 0;
  for (const l of patch.split("\n")) {
    if (l.startsWith("+") && !l.startsWith("+++")) add++;
    else if (l.startsWith("-") && !l.startsWith("---")) del++;
  }
  return { add, del };
}

export function formatDuration(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  return `${m} 分 ${s % 60} 秒`;
}

export function shortPath(p: string, n = 2): string {
  const parts = p.replace(/\\/g, "/").split("/").filter(Boolean);
  return parts.slice(-n).join("/") || p;
}

export function relTime(ts?: number): string {
  if (!ts) return "";
  const d = Date.now() / 1000 - ts;
  if (d < 60) return "刚刚";
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  if (d < 86400 * 30) return `${Math.floor(d / 86400)} 天前`;
  const dt = new Date(ts * 1000);
  return `${dt.getMonth() + 1}月${dt.getDate()}日`;
}
