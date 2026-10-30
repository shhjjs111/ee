// 流式 delta 的有界合并（T22）：高频 item/delta 在渲染帧 / 固定毫秒窗口内合并，
// 同 (thread_id, item_id, field) 按到达顺序拼接，最终文本与逐条应用逐字一致。
// 纯函数 + 一个可注入调度器的缓冲区，便于单测与接入 WebSocket 通知流。

export type DeltaEvent = { thread_id: string; item_id: string; field: string; delta: string };

/** 同 (thread_id, item_id, field) 的 delta 按首次出现顺序合并成一个。 */
export function mergeDeltas(events: DeltaEvent[]): DeltaEvent[] {
  const out: DeltaEvent[] = [];
  const idx = new Map<string, number>();
  for (const e of events) {
    const key = `${e.thread_id}\u0000${e.item_id}\u0000${e.field}`;
    const i = idx.get(key);
    if (i === undefined) {
      idx.set(key, out.length);
      out.push({ ...e });
    } else {
      out[i] = { ...out[i], delta: out[i].delta + e.delta };
    }
  }
  return out;
}

/** 把一批（已合并的）delta 一次性应用到条目数组。未知 item 的 delta 忽略，不产生新条目。 */
export function applyDeltas<T extends { id: string }>(items: T[], events: DeltaEvent[]): T[] {
  if (!events.length) return items;
  const merged = mergeDeltas(events);
  const byItem = new Map<string, DeltaEvent[]>();
  for (const e of merged) {
    const arr = byItem.get(e.item_id) ?? [];
    arr.push(e);
    byItem.set(e.item_id, arr);
  }
  let changed = false;
  const next = items.map((it) => {
    const arr = byItem.get(it.id);
    if (!arr) return it;
    changed = true;
    let cur = it;
    for (const e of arr) {
      cur = { ...cur, [e.field]: (((cur as unknown) as Record<string, unknown>)[e.field] as string || "") + e.delta } as T;
    }
    return cur;
  });
  return changed ? next : items;
}

export type FlushFn = (events: DeltaEvent[]) => void;
export type ScheduleFn = (fn: () => void) => void;

/** 一个窗口内只调度一次 flush；flush 前到达的 delta 全部合并后一次性交付。 */
export class StreamBuffer {
  private pending: DeltaEvent[] = [];
  private scheduled = false;

  constructor(
    private readonly onFlush: FlushFn,
    private readonly schedule: ScheduleFn,
  ) {}

  get size(): number {
    return this.pending.length;
  }

  push(e: DeltaEvent): void {
    this.pending.push(e);
    if (!this.scheduled) {
      this.scheduled = true;
      this.schedule(() => {
        this.scheduled = false;
        this.flush();
      });
    }
  }

  /** 立即交付并清空；返回本次交付的 delta 条数。 */
  flush(): number {
    const n = this.pending.length;
    if (!n) return 0;
    const events = this.pending;
    this.pending = [];
    this.onFlush(mergeDeltas(events));
    return n;
  }
}
