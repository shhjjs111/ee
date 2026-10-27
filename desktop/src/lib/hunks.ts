// 按块审阅（D5-7）：把单个文件的 patch 拆成文件头 + 各个块；块的顺序和后端 hunks 列表一一对应。纯函数，便于测试。

export type PatchHunk = { header: string; text: string; lines: string[] };

const HUNK_RE = /^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@/;

export function splitPatch(patch: string): { head: string[]; hunks: PatchHunk[] } {
  const head: string[] = [];
  const hunks: PatchHunk[] = [];
  let cur: PatchHunk | null = null;
  for (const raw of (patch || "").split("\n")) {
    const line = raw.endsWith("\r") ? raw.slice(0, -1) : raw;
    if (HUNK_RE.test(line)) {
      cur = { header: line, text: line, lines: [] };
      hunks.push(cur);
    } else if (cur) {
      cur.lines.push(line);
      cur.text += "\n" + line;
    } else {
      head.push(line);
    }
  }
  return { head, hunks };
}

/** 块 i 的 patch（文件头 + 这一块），给 DiffView 单独渲染。 */
export function hunkPatch(patch: string, i: number): string {
  const { head, hunks } = splitPatch(patch);
  const h = hunks[i];
  return h ? [...head, h.text].join("\n") : "";
}

/** 还没处理（没接受）的块数。 */
export function pendingCount(hunks: { accepted: boolean }[] | undefined): number {
  return (hunks ?? []).filter((h) => !h.accepted).length;
}

/** 时间线条目的时间：今天只显示时分，否则加月日。 */
export function formatWhen(ts: number | null | undefined, now: Date = new Date()): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  const hm = `${p(d.getHours())}:${p(d.getMinutes())}`;
  const same = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  return same ? hm : `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`;
}
