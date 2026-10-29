/** 按会话隔离的「退回消息」收件箱（T11 / ACC-20、ACC-21）。
 *
 * 问题：退回消息原来只有一个全局槽位 `returnedDraft`，且写入时按 threadId 判断，
 * 只要换一个会话再退回，前一个会话还没被消费的文案就被整格覆盖、永久丢失。
 * 用户在多会话并行时按 Esc，可能丢掉自己刚打的插话。
 *
 * 这里只做纯数据操作（无 React、无 store），便于单测覆盖边界：
 * 同毫秒两条、重复投递、乱序、只消费指定 ID。
 */

export type ReturnedInput = { id: string; text: string; queue?: "steer" | "follow" };

/** 按会话 id 存的收件箱。用 Map 语义的对象，写入时整体替换以保证不可变更新。 */
export type ReturnedInbox = Record<string, ReturnedInput[]>;

/** 追加退回消息：同一 id 只保留一条（后端可能重发），顺序按到达顺序。
 *
 * 用 id 去重而不是按时间戳：同毫秒的两条事件必须有各自的 id，否则会互相覆盖
 * （原来用 Date.now() 当 nonce，同毫秒就分不出来）。
 */
export function pushReturned(
  inbox: ReturnedInbox,
  threadId: string,
  items: ReturnedInput[],
): ReturnedInbox {
  if (!threadId || !items?.length) return inbox;
  const prev = inbox[threadId] ?? [];
  const seen = new Set(prev.map((x) => x.id));
  const next = [...prev];
  for (const it of items) {
    if (!it?.id || seen.has(it.id)) continue;
    seen.add(it.id);
    next.push(it);
  }
  if (next.length === prev.length) return inbox; // 全是重复：不制造新引用
  return { ...inbox, [threadId]: next };
}

/** 取走某个会话里指定的一批消息（输入框实际接收的那些），其余保留。
 *
 * 只消费指定 id，不能一把清空：用户正在看 A 会话时，B 会话新退回的消息
 * 不该被顺手丢掉。ids 为空表示把该会话的全部取走（用户主动要）。
 */
export function takeReturned(
  inbox: ReturnedInbox,
  threadId: string,
  ids?: string[],
): { inbox: ReturnedInbox; taken: ReturnedInput[] } {
  const prev = inbox[threadId] ?? [];
  if (!prev.length) return { inbox, taken: [] };
  if (!ids) {
    const rest = { ...inbox };
    delete rest[threadId];
    return { inbox: rest, taken: prev };
  }
  const want = new Set(ids);
  const taken = prev.filter((x) => want.has(x.id));
  if (!taken.length) return { inbox, taken: [] };
  const left = prev.filter((x) => !want.has(x.id));
  const rest = { ...inbox };
  if (left.length) rest[threadId] = left;
  else delete rest[threadId];
  return { inbox: rest, taken };
}

/** 该会话还有没有未消费的退回消息。 */
export function hasReturned(inbox: ReturnedInbox, threadId: string | null | undefined): boolean {
  if (!threadId) return false;
  return (inbox[threadId]?.length ?? 0) > 0;
}

/** 把退回文本合并进当前输入框内容：退回的排在前面，已有文字不丢。 */
export function mergeDraft(texts: string[], current: string): string {
  const head = texts.filter((t) => t && t.trim()).join("\n\n");
  if (!head) return current;
  if (!current.trim()) return head;
  return `${head}\n\n${current}`;
}
