// 窄 selector（T22）：让 Conversation 等高频组件按需订阅，避免无关字段变化触发整树重渲染。
// 返回原始引用（对象/字符串/数字），zustand 用 Object.is 判断是否重渲染。
// 关键：只订阅本会话相关的切片——其他会话的 delta flush 会重建 details Record，
// 但本会话的 ThreadDetail 对象引用不变，因此本会话组件不会跟着重渲染。
import type { ThreadDetail } from "../types";

export type ActiveDetailSlice = {
  activeThreadId: string | null;
  details: Record<string, ThreadDetail>;
};

export function selectActiveThreadId(s: { activeThreadId: string | null }): string | null {
  return s.activeThreadId;
}

export function selectActiveDetail(s: ActiveDetailSlice): ThreadDetail | null {
  const id = s.activeThreadId;
  if (!id) return null;
  return s.details[id] ?? null;
}

export function selectActiveTurnStartedAt(s: { activeThreadId: string | null; turnStartedAt: Record<string, number> }): number | undefined {
  const id = s.activeThreadId;
  if (!id) return undefined;
  return s.turnStartedAt[id];
}
