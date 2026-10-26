import { describe, expect, it } from "vitest";
import type { Item } from "../types";
import { mergeDraft, queueKindForKey, splitQueued } from "./queue";
import { applyItemNotification } from "./reduce";

const u = (id: string, extra: Record<string, unknown> = {}) => ({ id, type: "user", text: id, ...extra }) as unknown as Item;
const a = (id: string) => ({ id, type: "assistant", text: id, reasoning: "" }) as unknown as Item;

describe("queue", () => {
  it("把排队中的用户消息从对话里分出来，保持顺序", () => {
    const items = [u("u1"), a("a1"), u("q1", { queued: true, queue: "follow" }), u("q2", { queued: true, queue: "steer" }), u("u2", { queue: "steer" })];
    const { visible, queued } = splitQueued(items);
    expect(visible.map((x) => x.id)).toEqual(["u1", "a1", "u2"]);
    expect(queued.map((x) => x.id)).toEqual(["q1", "q2"]);
  });

  it("Enter 插话，Ctrl/⌘+Enter 完成后再做", () => {
    expect(queueKindForKey({})).toBe("steer");
    expect(queueKindForKey({ ctrlKey: true })).toBe("follow");
    expect(queueKindForKey({ metaKey: true })).toBe("follow");
  });

  it("退回的消息排在输入框已有文字前面", () => {
    expect(mergeDraft(["第一句", "第二句"], "")).toBe("第一句\n\n第二句");
    expect(mergeDraft(["退回"], "正在写")).toBe("退回\n\n正在写");
    expect(mergeDraft([" ", ""], "x")).toBe("x");
  });

  it("送入时后端先移除再追加：排队条目挪到对话末尾并去掉排队标记", () => {
    let t = { id: "t", items: [u("u1"), u("q1", { queued: true, queue: "follow" }), a("a1")] } as any;
    t = applyItemNotification(t, "item/removed", { item_id: "q1" });
    t = applyItemNotification(t, "item/started", { item: u("q1", { queue: "follow" }) });
    const { visible, queued } = splitQueued(t.items);
    expect(visible.map((x: Item) => x.id)).toEqual(["u1", "a1", "q1"]);
    expect(queued).toEqual([]);
  });
});
