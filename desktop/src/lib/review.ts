// /review 结果解析（D4-2）：回答末尾的 ```ee-review JSON 块 → 问题卡片。纯函数，便于测试。

export type Finding = {
  priority: number; // 0–3
  title: string;
  body: string;
  file: string;
  line_start?: number | null;
  line_end?: number | null;
  confidence?: number | null;
};

export type ReviewResult = { overall: "correct" | "incorrect"; summary: string; findings: Finding[] };

const BLOCK_RE = /```ee-review[^\n]*\n([\s\S]*?)(?:```|$)/g;

/** 拆出正文（去掉 ee-review 块）和解析结果；没有块或解析失败时 result 为 null。 */
export function splitReview(text: string): { body: string; result: ReviewResult | null } {
  const blocks = [...(text || "").matchAll(BLOCK_RE)];
  if (!blocks.length) return { body: text, result: null };
  const body = text.replace(BLOCK_RE, "").trimEnd();
  let data: any;
  try {
    data = JSON.parse(blocks[blocks.length - 1][1]);
  } catch {
    return { body, result: null };
  }
  if (!data || typeof data !== "object") return { body, result: null };
  const findings: Finding[] = (Array.isArray(data.findings) ? data.findings : [])
    .filter((f: any) => f && typeof f === "object" && f.title)
    .map((f: any) => {
      const p = Number.isFinite(Number(f.priority)) ? Math.round(Number(f.priority)) : 2;
      const num = (v: any) => (Number.isFinite(Number(v)) && v !== null && v !== "" ? Number(v) : null);
      return {
        priority: Math.min(3, Math.max(0, p)),
        title: String(f.title).slice(0, 200),
        body: String(f.body ?? ""),
        file: String(f.file ?? ""),
        line_start: num(f.line_start),
        line_end: num(f.line_end),
        confidence: num(f.confidence),
      };
    })
    .sort((a: Finding, b: Finding) => a.priority - b.priority);
  const overall = String(data.overall ?? "").toLowerCase().startsWith("in") ? "incorrect" : "correct";
  return { body, result: { overall, summary: String(data.summary ?? ""), findings } };
}

export function location(f: Finding): string {
  if (!f.file) return "";
  if (f.line_start == null) return f.file;
  return f.line_end != null && f.line_end !== f.line_start ? `${f.file}:${f.line_start}-${f.line_end}` : `${f.file}:${f.line_start}`;
}

/** “修复这些问题”发给模型的消息。 */
export function fixMessage(findings: Finding[]): string {
  const lines = findings.map((f, i) => {
    const loc = location(f);
    const body = f.body.trim() ? `\n   ${f.body.trim().replace(/\n/g, "\n   ")}` : "";
    return `${i + 1}. [P${f.priority}] ${f.title}${loc ? `（${loc}）` : ""}${body}`;
  });
  return `请修复刚才代码审查发现的这些问题，改完简单说明每条怎么处理的（如果认为某条不成立，说明理由，不要硬改）：\n\n${lines.join("\n")}`;
}
