import { describe, expect, it } from "vitest";
import { approvalNotice, lastAssistantSnippet, turnCompletedNotice } from "./notify";

const items: any[] = [
  { id: "1", type: "user", text: "改一下" },
  { id: "2", type: "assistant", text: "## 已完成\n把 **foo** 改成了 `bar`。\n```js\nx\n```" },
  { id: "3", type: "turn_end", reason: "completed" },
];

describe("notify", () => {
  it("snippet strips markdown and stops at user", () => {
    expect(lastAssistantSnippet(items as any)).toBe("已完成 把 foo 改成了 bar。");
    expect(lastAssistantSnippet([...items, { id: "4", type: "user", text: "再来" }] as any)).toBe("");
    expect(lastAssistantSnippet([{ id: "a", type: "assistant", text: "x".repeat(300) }] as any, 10)).toHaveLength(10);
  });

  it("turn completed", () => {
    const n = turnCompletedNotice({ thread_id: "t", reason: "completed" }, "修 bug", items as any)!;
    expect(n).toMatchObject({ kind: "done", threadId: "t", title: "修 bug · 已完成" });
    expect(n.body).toContain("foo");
    expect(turnCompletedNotice({ thread_id: "t", reason: "interrupted" }, "x")).toBeNull();
    const e = turnCompletedNotice({ thread_id: "t", reason: "error" }, "", [{ id: "e", type: "error", message: "连接超时" }] as any)!;
    expect(e).toMatchObject({ kind: "error", title: "对话 · 出错了", body: "连接超时" });
    expect(turnCompletedNotice({ thread_id: "t", reason: "loop" }, "x")!.kind).toBe("loop");
  });

  it("approval only on transition", () => {
    const its = [{ id: "a", type: "approval", status: "pending", title: "运行 npm install" }] as any;
    expect(approvalNotice("t", "running", "waiting_approval", "装依赖", its)).toMatchObject({ kind: "approval", body: "运行 npm install", title: "装依赖 · 需要你审批" });
    expect(approvalNotice("t", "waiting_approval", "waiting_approval", "x", its)).toBeNull();
    expect(approvalNotice("t", "running", "idle", "x", its)).toBeNull();
    expect(approvalNotice("t", undefined, "waiting_approval", "x")!.body).toBe("有操作在等你批准");
  });
});
