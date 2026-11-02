// 上下文构成视图的纯函数（A12）。
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

/** 服务端缓存命中（T19）：命中率 + 覆盖率。未知（hit_rate 为 null）时不显示，绝不编造成 0。 */
export function cacheLine(d: ContextBreakdown): string | null {
  const c = d.cache;
  if (!c || c.hit_rate == null) return null;
  const rate = `${Math.round(c.hit_rate * 100)}%`;
  if (c.hit_coverage == null) return `服务端缓存命中 ${rate}`;
  return `服务端缓存命中 ${rate} · 覆盖 ${Math.round(c.hit_coverage * 100)}% 输入`;
}
