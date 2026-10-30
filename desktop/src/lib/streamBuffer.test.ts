import { describe, expect, it } from "vitest";
import { applyDeltas, mergeDeltas, StreamBuffer, type DeltaEvent } from "./streamBuffer";

const d = (thread_id: string, item_id: string, field: string, delta: string): DeltaEvent => ({ thread_id, item_id, field, delta });

describe("mergeDeltas", () => {
  it("同 item 同字段按到达顺序拼接", () => {
    const out = mergeDeltas([
      d("t", "a", "text", "你"),
      d("t", "a", "text", "好"),
      d("t", "a", "text", "，世界"),
    ]);
    expect(out).toEqual([d("t", "a", "text", "你好，世界")]);
  });

  it("不同字段、不同 item 不串", () => {
    const out = mergeDeltas([
      d("t", "a", "text", "A"),
      d("t", "a", "reasoning", "R"),
      d("t", "b", "text", "B"),
    ]);
    expect(out).toHaveLength(3);
  });

  it("不同 thread 不串", () => {
    const out = mergeDeltas([d("t1", "a", "text", "1"), d("t2", "a", "text", "2")]);
    expect(out.map((x) => x.delta)).toEqual(["1", "2"]);
  });

  it("保序：同字段的合并结果保持在第一次出现的位置", () => {
    const out = mergeDeltas([
      d("t", "a", "text", "x"),
      d("t", "b", "text", "y"),
      d("t", "a", "text", "z"),
    ]);
    expect(out.map((x) => x.item_id)).toEqual(["a", "b"]);
    expect(out[0].delta).toBe("xz");
  });
});

describe("applyDeltas", () => {
  it("合并后应用 == 逐个应用（最终文本逐字一致）", () => {
    const items = [{ id: "a", type: "assistant", text: "", reasoning: "" }] as any[];
    const deltas = [d("t", "a", "text", "你"), d("t", "a", "text", "好"), d("t", "a", "text", "，世界")];
    let oneByOne = items;
    for (const x of deltas) {
      oneByOne = oneByOne.map((it) => (it.id === x.item_id ? { ...it, [x.field]: (it[x.field] || "") + x.delta } : it));
    }
    const merged = applyDeltas(items, mergeDeltas(deltas));
    expect(merged[0].text).toBe(oneByOne[0].text);
    expect(merged[0].text).toBe("你好，世界");
  });

  it("已有文本继续拼接（不清空、不覆盖）", () => {
    const items = [{ id: "a", text: "前缀" }] as any[];
    const merged = applyDeltas(items, mergeDeltas([d("t", "a", "text", "后续")]));
    expect(merged[0].text).toBe("前缀后续");
  });

  it("未知 item 的 delta 不产生新 item、不丢其他 item", () => {
    const items = [{ id: "a", text: "" }] as any[];
    const merged = applyDeltas(items, [d("t", "ghost", "text", "x")]);
    expect(merged).toBe(items); // 引用不变 = 无变化
  });

  it("空 delta 列表返回原引用", () => {
    const items = [{ id: "a", text: "" }] as any[];
    expect(applyDeltas(items, [])).toBe(items);
  });
});

describe("StreamBuffer", () => {
  it("帧内多个 delta 只调度一次、只交付一次，且合并", () => {
    let flushCount = 0;
    let delivered: DeltaEvent[] = [];
    const scheduled: (() => void)[] = [];
    const buf = new StreamBuffer(
      (dd) => {
        flushCount++;
        delivered = dd;
      },
      (fn) => scheduled.push(fn),
    );
    buf.push(d("t", "a", "text", "你"));
    buf.push(d("t", "a", "text", "好"));
    expect(flushCount).toBe(0);
    expect(scheduled).toHaveLength(1);
    scheduled[0]();
    expect(flushCount).toBe(1);
    expect(delivered).toEqual([d("t", "a", "text", "你好")]);
  });

  it("flush 后再次 push 会重新调度", () => {
    const scheduled: (() => void)[] = [];
    const buf = new StreamBuffer(() => {}, (fn) => scheduled.push(fn));
    buf.push(d("t", "a", "text", "1"));
    scheduled[0]();
    expect(buf.size).toBe(0);
    buf.push(d("t", "a", "text", "2"));
    expect(scheduled).toHaveLength(2);
  });

  it("手动 flush 立即交付并清空", () => {
    let delivered: DeltaEvent[] = [];
    const buf = new StreamBuffer((dd) => (delivered = dd), () => {});
    buf.push(d("t", "a", "text", "你"));
    buf.push(d("t", "a", "text", "好"));
    expect(buf.flush()).toBe(2);
    expect(delivered).toEqual([d("t", "a", "text", "你好")]);
    expect(buf.size).toBe(0);
  });

  it("空 buffer 的 flush 不调用回调", () => {
    let called = 0;
    const buf = new StreamBuffer(() => called++, () => {});
    expect(buf.flush()).toBe(0);
    expect(called).toBe(0);
  });
});
