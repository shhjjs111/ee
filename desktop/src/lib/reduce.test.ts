import { describe, expect, it } from "vitest";
import type { ThreadDetail } from "../types";
import { applyItemNotification, diffStat, groupBlocks, parseDiff, subagentStatus } from "./reduce";

const base: ThreadDetail = {
  id: "t1", title: "x", cwd: "/w", model: "m", mode: "default", status: "running",
  items: [], usage: {}, context_left: null, shell: null, git: null,
};

describe("applyItemNotification", () => {
  it("goal_check 把判定进度写进 status_text（长跑不会看起来卡死）", () => {
    const t = applyItemNotification(base, "goal_check", { round: 2, met: false, reason: "exit=1：命令以非零退出" });
    expect(t.status_text).toContain("目标还没达成");
    expect(t.status_text).toContain("第 2 次判定");
    const ok = applyItemNotification(base, "goal_check", { round: 3, met: true, reason: "ok：12 个测试通过" });
    expect(ok.status_text).toBe("判据已通过");
  });

  it("goal_done 清掉进度文字", () => {
    const t = applyItemNotification({ ...base, status_text: "目标还没达成" }, "goal_done", { stop_reason: "exhausted" });
    expect(t.status_text).toBe("");
  });

  it("allout_plan 显示火力路数", () => {
    const t = applyItemNotification(base, "allout_plan", { plan: { candidates: 3 }, goal: null, probe: null });
    expect(t.status_text).toContain("全力以赴");
    expect(t.status_text).toContain("3 路");
  });

  it("allout_progress 把修复轮、候选、写回都写进状态（长跑不看起来卡死）", () => {
    const start = applyItemNotification(base, "allout_progress", { phase: "start", candidates: 2 });
    expect(start.status_text).toContain("2 路开始");

    const judge = applyItemNotification(base, "allout_progress", { phase: "judge", met: false, tests: 5 });
    expect(judge.status_text).toContain("未通过");
    expect(judge.status_text).toContain("5 个测试");

    const repair = applyItemNotification(base, "allout_progress", { phase: "repair", attempt: 1, kept: true });
    expect(repair.status_text).toContain("第 1 轮修复");
    expect(repair.status_text).toContain("已保留");

    const cand = applyItemNotification(base, "allout_progress", { phase: "candidate", id: "cand-2", met: true });
    expect(cand.status_text).toContain("cand-2");
    expect(cand.status_text).toContain("通过判据");

    const applied = applyItemNotification(base, "allout_progress", { phase: "apply", winner: "cand-1", result: { applied: true } });
    expect(applied.status_text).toContain("已写回");

    const skipped = applyItemNotification(base, "allout_progress", { phase: "fanout_skipped", why: "不是 git 仓库" });
    expect(skipped.status_text).toContain("未分叉");
    expect(skipped.status_text).toContain("不是 git 仓库");

    const reg = applyItemNotification(base, "allout_progress", { phase: "regression", met: false, id: "cand-1" });
    expect(reg.status_text).toContain("回归检查未通过");
    expect(reg.status_text).toContain("cand-1");
  });

  it("allout_progress 的 done 不清状态，交给 turn/completed", () => {
    const t = applyItemNotification({ ...base, status_text: "全力以赴：判据通过" }, "allout_progress", { phase: "done", how: "first_try" });
    expect(t.status_text).toBe("全力以赴：判据通过");
  });

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

describe("通知乱序（同一连接内理论上不该发生，但重连/补齐路径会）", () => {
  it("item/updated 先到：条目照样出现，后来的 item/started 不能产生重复", () => {
    const updated = applyItemNotification(base, "item/updated", {
      item: { id: "a1", type: "assistant", text: "最终文本", streaming: false },
    });
    expect(updated.items.map((x) => x.id)).toEqual(["a1"]);

    const started = applyItemNotification(updated, "item/started", {
      item: { id: "a1", type: "assistant", text: "", streaming: true },
    });
    expect(started.items.map((x) => x.id), "同一条目出现两次会让消息重复渲染").toEqual(["a1"]);
    expect((started.items[0] as any).text, "started 是权威的初始版本，覆盖而不是合并").toBe("");
  });

  it("item/removed 先到、item/updated 后到：条目被重新加回（后端不会这么发，但不许崩）", () => {
    const t = applyItemNotification(base, "item/removed", { item_id: "a1" });
    expect(t.items).toEqual([]);
    const back = applyItemNotification(t, "item/updated", {
      item: { id: "a1", type: "assistant", text: "x", streaming: false },
    });
    expect(back.items.map((x) => x.id)).toEqual(["a1"]);
  });
});
