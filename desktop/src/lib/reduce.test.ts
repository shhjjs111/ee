import { describe, expect, it } from "vitest";
import type { ThreadDetail } from "../types";
import { applyItemNotification, diffStat, groupBlocks, parseDiff, subagentStatus } from "./reduce";

const base: ThreadDetail = {
  id: "t1", title: "x", cwd: "/w", model: "m", mode: "default", status: "running",
  items: [], usage: {}, context_left: null, shell: null, git: null,
};

describe("applyItemNotification", () => {
  it("started → delta → updated → removed", () => {
    let t = applyItemNotification(base, "item/started", { item: { id: "a1", type: "assistant", text: "", reasoning: "" } });
    t = applyItemNotification(t, "item/delta", { item_id: "a1", field: "text", delta: "你好" });
    t = applyItemNotification(t, "item/delta", { item_id: "a1", field: "text", delta: "，世界" });
    expect((t.items[0] as any).text).toBe("你好，世界");
    t = applyItemNotification(t, "item/updated", { item: { id: "a1", type: "assistant", text: "最终", reasoning: "" } });
    expect((t.items[0] as any).text).toBe("最终");
    t = applyItemNotification(t, "item/removed", { item_id: "a1" });
    expect(t.items).toHaveLength(0);
  });
  it("turn/completed 置为空闲", () => {
    const t = applyItemNotification(base, "turn/completed", { usage: { requests: 2 }, context_left: 0.9 });
    expect(t.status).toBe("idle");
    expect(t.usage.requests).toBe(2);
  });
});

describe("groupBlocks", () => {
  it("连续的读/搜工具合并为一组", () => {
    const tool = (id: string, name: string, kind?: string) => ({ id, type: "tool", name, call_id: id, args: {}, summary: "", status: "ok", output: "", details: kind ? { kind } : {} }) as any;
    const blocks = groupBlocks([
      tool("1", "read", "read"),
      tool("2", "grep", "search"),
      tool("3", "shell", "shell"),
      tool("4", "glob", "list"),
    ]);
    expect(blocks.map((b) => b.kind)).toEqual(["explore", "item", "explore"]);
    expect((blocks[0] as any).items).toHaveLength(2);
  });

  it("连续的子代理合成一组，单个子代理保持原样", () => {
    const task = (id: string, status = "running", sub?: string) =>
      ({ id, type: "tool", name: "task", call_id: id, args: {}, summary: "", status, output: "", details: sub ? { kind: "subagent", subagent: { status: sub } } : {} }) as any;
    const other = { id: "x", type: "tool", name: "shell", call_id: "x", args: {}, summary: "", status: "ok", output: "", details: {} } as any;
    const blocks = groupBlocks([task("a"), task("b", "running", "queued"), task("c"), other, task("d")]);
    expect(blocks.map((b) => b.kind)).toEqual(["agents", "item", "item"]);
    expect((blocks[0] as any).items.map((i: any) => i.id)).toEqual(["a", "b", "c"]);
    expect(subagentStatus(task("e", "running", "queued"))).toBe("queued");
    expect(subagentStatus(task("f", "interrupted", "running"))).toBe("interrupted");
    expect(subagentStatus(task("g", "ok", "completed"))).toBe("completed");
    expect(subagentStatus(task("h", "running"))).toBe("running");
  });
});

describe("parseDiff", () => {
  it("行号和增删统计", () => {
    const patch = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,2 +1,3 @@\n a\n-b\n+c\n+d";
    const lines = parseDiff(patch).filter((l) => l.kind !== "meta");
    expect(lines[0].kind).toBe("hunk");
    expect(lines[1]).toMatchObject({ kind: "ctx", oldNo: 1, newNo: 1 });
    expect(lines[2]).toMatchObject({ kind: "del", oldNo: 2 });
    expect(lines[4]).toMatchObject({ kind: "add", newNo: 3 });
    expect(diffStat(patch)).toEqual({ add: 2, del: 1 });
  });
});
