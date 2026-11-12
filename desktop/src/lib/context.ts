// 上下文构成视图的纯函数（A12）。
// pi-lens-ignore: find-import-file-without-extension -- Vite/TS bundler 解析，项目统一不写扩展名
import type { ContextBreakdown } from "../types";

export function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`;
  if (n >= 10_000) return `${Math.round(n / 1000)}k`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

/** 顶部一行：「已用 12.3k / 1M（1%）」；用服务端用量时注明。 */
export function usageLine(d: ContextBreakdown): string {
  const pct = d.window > 0 ? Math.round((100 * d.used) / d.window) : 0;
  return `已用 ${fmtTokens(d.used)} / ${fmtTokens(d.window)}（${pct}%）${d.actual ? "" : " · 估算"}`;
}

/** 修剪 / 压缩节省说明；都没有返回 null。
 * reasoning_cut 是「不再回传的旧推理」累计节省量，跟 pruning 分开说——原来两边写进同一个
 * pruned 字段，开了清推理后会把工具修剪的数字虚高。 */
export function savingsLine(d: ContextBreakdown): string | null {
  const parts: string[] = [];
  const reasoning = d.savings.reasoning_cut ?? 0;
  if (reasoning > 0) parts.push(`旧推理不再回传省 ~${fmtTokens(reasoning)}`);
  if (d.savings.pruned > 0) parts.push(`修剪旧工具输出省 ~${fmtTokens(d.savings.pruned)}`);
  if (d.savings.compacted > 0) parts.push(`压缩 ${d.compactions || 1} 次省 ~${fmtTokens(d.savings.compacted)}`);
  return parts.length ? parts.join(" · ") : null;
}

/** 阈值说明：「60% 时修剪旧工具输出，80% 时自动压缩」；配了绝对上限时改说 token 数。
 *
 * prune_tokens 缺省/为 0 表示消息级修剪关着（后端默认如此）：这时 60% 只是「清旧推理」
 * 的门槛文案不适用，直接不说——否则用户会以为 60% 会修剪工具输出，实际永远不修剪。 */
export function thresholdLine(d: ContextBreakdown): string | null {
  const t = d.thresholds;
  const parts: string[] = [];
  if (t.prune != null && t.prune_tokens) {
    parts.push(`${fmtTokens(t.prune_tokens)} token 时修剪旧工具输出`);
  } else if (t.prune != null && t.prune_tokens === undefined) {
    // 旧版后端不会发 prune_tokens：按百分比说，保持兼容
    parts.push(`${Math.round(t.prune * 100)}% 时修剪旧工具输出`);
  }
  if (t.compact != null) {
    parts.push(t.compact_tokens ? `${fmtTokens(t.compact_tokens)} token 时自动压缩` : `${Math.round(t.compact * 100)}% 时自动压缩`);
  }
  return parts.length ? parts.join("，") : null;
}

/** 服务端缓存命中（T19）：命中率 + 覆盖率。未知（hit_rate 为 null）时不显示，绝不编造成 0。
 *
 * 优先用主对话口径（main_*，P0-1）：标题 / commit / 压缩 / 子代理这些侧路请求前缀独立、
 * 命中恒 0 或很低，混进分母会把用户看到的数字无端拉低几个百分点。旧后端没有 main_*
 * 时回落到 hit_*，行为与之前一致。 */
export function cacheLine(d: ContextBreakdown): string | null {
  const c = d.cache;
  if (!c) return null;
  const main = c.main_hit_rate != null;
  const rate = main ? c.main_hit_rate : c.hit_rate;
  if (rate == null) return null;
  const cov = main ? c.main_hit_coverage : c.hit_coverage;
  const label = main ? "主对话缓存命中" : "服务端缓存命中";
  const pct = `${Math.round(rate * 100)}%`;
  const missN = c.usage_missing ?? 0;
  const parts = cov == null ? [`${label} ${pct}`] : [`${label} ${pct} · 覆盖 ${Math.round(cov * 100)}% 输入`];
  // 缺用量的请求 prompt=0，token 覆盖率永远是 100%——不说出来，那行「覆盖 100%」
  // 就会把「N 次请求根本没报用量」盖掉。
  if (missN > 0) parts.push(`缺用量 ${missN} 次`);
  // 冷启动/无法归因不计入命中率时，也要让用户知道它们发生过；
  // 否则重启、换模型、压缩造成的账本变化只能靠 ee usage 看出来。
  if (c.cold != null && c.cold > 0) parts.push(`冷启动 ${c.cold} 次`);
  // 老账本（baseline 字段上线前）分不清是冷启动还是整段未命中：单独一句话，
  // 不冒充冷启动——混进去会让「冷启动几次」变成不可信的旧账。
  if (c.legacy != null && c.legacy > 0) parts.push(`旧账本无法归因 ${c.legacy} 次`);
  const causes = Object.entries(c.causes ?? {}).filter(([, n]) => n > 0).sort(([a], [b]) => a.localeCompare(b));
  if (causes.length) parts.push(`已知原因 ${causes.map(([k, n]) => `${k} ${n}`).join("、")}`);
  return parts.join(" · ");
}
