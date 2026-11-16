// 纯函数：把后端通知应用到线程详情上（便于单测）。
import type { Item, ThreadDetail, ToolItem } from "../types";

export function applyItemNotification(t: ThreadDetail, method: string, p: any): ThreadDetail {
  switch (method) {
    case "item/started": {
      const it = p.item as Item;
      if (t.items.some((x) => x.id === it.id)) return { ...t, items: t.items.map((x) => (x.id === it.id ? it : x)) };
      return {
        ...t,
        ...(it.type === "user" && !it.queued && !it.queue
          ? {
              allout_plan: undefined, allout_candidates: undefined, allout_winner: undefined,
              allout_review: undefined,
              // 这两个是「上一回合」的事实：清了它们，面板才不会在新回合里
              // 继续显示上一回合的"实际启动 N 路"（单路回合根本不推 fanout）。
              allout_started: undefined, allout_requested: undefined,
            }
          : {}),
        items: [...t.items, it],
      };
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
    case "goal_check":
      // Goal / 全力以赴的进度：每轮判定都推一条。没有它长跑看起来就是卡死。
      return {
        ...t,
        status_text: p.met
          ? "判据已通过"
          : `目标还没达成：${p.reason || ""}（第 ${p.round ?? "?"} 次判定）`.trim(),
      };
    case "goal_done":
      return {
        ...t,
        status_text: "",
      };
    case "allout_plan":
      return {
        ...t,
        status_text: `全力以赴 · ${p.plan?.candidates ?? 1} 路`,
        allout_plan: p.plan,
        // 用户设置的上限与推导上限分开存：界面要能显示"设了 6 路、本轮 2 路、因为内存"。
        allout_requested: p.requested ?? p.plan?.candidates,
      };
    // 全力以赴的执行进度：修复轮、候选、写回都在这里。没有它，长跑只剩
    // 「全力以赴 · 3 路」一行不动，用户看不出到底在修、在试、还是已经写回。
    case "allout_progress": {
      const phase = String(p.phase || "");
      if (phase === "candidate_state" && typeof p.id === "string") {
        const previous = t.allout_candidates?.[p.id];
        return { ...t, allout_candidates: { ...t.allout_candidates, [p.id]: { ...previous, ...p } } };
      }
      if (phase === "selection") {
        const candidates = { ...t.allout_candidates };
        for (const score of p.scores ?? []) {
          candidates[score.id] = { ...candidates[score.id], ...score, state: "passed" };
        }
        return { ...t, allout_candidates: candidates, allout_winner: p.winner };
      }
      if (phase === "review") {
        let review = "模型评审未完成，已按验收结果择优";
        if (p.state === "running") review = "正在对照评审通过验收的候选";
        else if (p.state === "skipped") review = "已按验收结果择优，无需模型对照";
        else if (p.reviewed > 0) review = `模型评审完成 ${p.reviewed}/${p.total} 组对照`;
        return { ...t, allout_review: review };
      }
      if (phase === "self_review" || phase === "implementation") {
        return { ...t, status_text: phase === "self_review" ? "全力以赴：核对需求与验证证据" : "全力以赴：实现与建立验证" };
      }
      if (phase === "start") {
        return { ...t, status_text: `全力以赴 · ${p.candidates ?? 1} 路开始` };
      }
      if (phase === "fanout") {
        // 实际启动的候选数：可能少于推导上限（无 Git/无提交/工作区不可隔离时会跳过）。
        return { ...t, allout_started: p.started ?? 0, status_text: `全力以赴：启动 ${p.started ?? 0} 路候选` };
      }
      if (phase === "judge") {
        const n = typeof p.tests === "number" && p.tests > 0 ? `（${p.tests} 个测试）` : "";
        return { ...t, status_text: `全力以赴：判据${p.met ? "通过" : "未通过"}${n}` };
      }
      if (phase === "repair") {
        // 「未保留」必须说清楚磁盘到底回滚了没有：reverted 是回滚的文件数，null = 没能回滚。
        // 有明确进展但还没通过完整验收时，代码是**留在工作区继续修**的，不能显示成"未保留"。
        const fixed = typeof p.fixed === "number" && p.fixed > 0 ? `（修好 ${p.fixed} 条）` : "";
        let tail = "，未保留（未能回滚，工作区仍是这份改动）";
        if (p.kept) tail = "，已保留";
        else if (p.progress === "improved") tail = `，失败面缩小${fixed}，保留继续修`;
        else if (typeof p.reverted === "number") tail = `，已回滚${p.reverted} 个文件`;
        return { ...t, status_text: `全力以赴：第 ${p.attempt ?? 1} 轮修复${tail}` };
      }
      if (phase === "candidate") {
        // 候选分两种思路：延续（带着上一轮失败继续修）与独立（从原始需求重新定位）。
        let role = "";
        if (p.role === "continuation") role = "延续路线";
        else if (p.role === "independent") role = "独立路线";
        const who = role ? `${role}候选 ${p.id ?? "?"}` : `候选 ${p.id ?? "?"}`;
        return { ...t, status_text: `全力以赴：${who}${p.met ? "通过判据" : "未通过"}` };
      }
      if (phase === "apply") {
        return { ...t, status_text: p.result?.applied ? "全力以赴：候选补丁已写回" : "全力以赴：补丁写回失败" };
      }
      if (phase === "regression") {
        const who = p.id ? `（${p.id}）` : "";
        return { ...t, status_text: `全力以赴：回归检查${p.met ? "通过" : "未通过"}${who}` };
      }
      if (phase === "fanout_skipped") {
        return { ...t, status_text: `全力以赴：未分叉（${p.why || "原因未知"}）` };
      }
      return t;   // done：交给 turn/completed 清空
    }
    case "turn/status":
      return { ...t, status_text: p.text || "" };
    case "thread/usage":
      return { ...t, usage: p.usage, context_left: p.context_left };
    case "turn/completed":
      return {
        ...t,
        status: "idle",
        status_text: "",
        usage: p.usage ?? t.usage,
        context_left: p.context_left ?? t.context_left,
        instructions: p.instructions ?? t.instructions,
      };
    default:
      return t;
  }
}

export type Block =
  | { kind: "item"; item: Item }
  | { kind: "explore"; id: string; items: Item[] }
  | { kind: "agents"; id: string; items: ToolItem[] };

/** task 工具条目（子代理卡片）。 */
export function isSubagent(it: Item): it is ToolItem {
  return it.type === "tool" && (it.name === "task" || it.details?.kind === "subagent");
}

/** 子代理条目的状态：queued / running / completed / …（记录优先，没有就看工具条目）。 */
export function subagentStatus(it: ToolItem): string {
  const rec = it.details?.subagent;
  const st: string | undefined = rec?.status;
  if (st) {
    // 记录还停在“运行/排队”但工具条目已经结束（被中断、内核重启）：以工具条目为准
    if ((st === "running" || st === "queued") && it.status !== "running") return it.status === "ok" ? "completed" : "interrupted";
    return st;
  }
  if (it.status === "running") return "running";
  return it.status === "ok" ? "completed" : it.status;
}

const EXPLORE_TOOLS = new Set(["read", "grep", "glob"]);

export function isExplore(it: Item): boolean {
  if (it.type !== "tool") return false;
  const k = it.details?.kind;
  if (k === "read" || k === "list" || k === "search") return true;
  return EXPLORE_TOOLS.has(it.name);
}

/** 把连续的读/搜工具合并成一张“已浏览 N 个文件”卡片。 */
export function groupBlocks(items: Item[]): Block[] {
  const out: Block[] = [];
  for (const it of items) {
    if (isSubagent(it)) {
      // 连续的子代理（同一步并行派出的）合成一组，组头显示几个在跑
      const last = out[out.length - 1];
      if (last && last.kind === "agents") {
        last.items.push(it);
        continue;
      }
      const prev = out[out.length - 1];
      if (prev && prev.kind === "item" && isSubagent(prev.item)) {
        out[out.length - 1] = { kind: "agents", id: `ag_${prev.item.id}`, items: [prev.item, it] };
        continue;
      }
      out.push({ kind: "item", item: it });
      continue;
    }
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
