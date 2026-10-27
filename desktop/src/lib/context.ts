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

/** 修剪 / 压缩节省说明；都没有返回 null。 */
export function savingsLine(d: ContextBreakdown): string | null {
  const parts: string[] = [];
  if (d.savings.pruned > 0) parts.push(`修剪旧工具输出省 ~${fmtTokens(d.savings.pruned)}`);
  if (d.savings.compacted > 0) parts.push(`压缩 ${d.compactions || 1} 次省 ~${fmtTokens(d.savings.compacted)}`);
  return parts.length ? parts.join(" · ") : null;
}

/** 阈值说明：「60% 时修剪旧工具输出，80% 时自动压缩」。 */
export function thresholdLine(d: ContextBreakdown): string | null {
  const t = d.thresholds;
  const parts: string[] = [];
  if (t.prune != null) parts.push(`${Math.round(t.prune * 100)}% 时修剪旧工具输出`);
  if (t.compact != null) parts.push(`${Math.round(t.compact * 100)}% 时自动压缩`);
  return parts.length ? parts.join("，") : null;
}
