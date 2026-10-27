import { describe, expect, it } from "vitest";
import { loopListText, loopStatus, loopToAutomation, parseLoopArgs, type SessionLoop } from "./loop";
import { formatCost, formatDuration, runTimeline, worktreeChangeText, worktreeStateLabel } from "./schedule";

const lp = (p: Partial<SessionLoop> = {}): SessionLoop => ({
  id: "1",
  thread_id: "t1",
  prompt: "检查CI是否通过",
  schedule: { kind: "interval", minutes: 10 },
  schedule_text: "每 10 分钟",
  created: 0,
  next_run: null,
  last_run: null,
  runs: 0,
  skipped: 0,
  pending: false,
  ...p,
});

describe("/loop", () => {
  it("parses subcommands", () => {
    expect(parseLoopArgs("")).toEqual({ kind: "help" });
    expect(parseLoopArgs("list")).toEqual({ kind: "list" });
    expect(parseLoopArgs("stop 2")).toEqual({ kind: "stop", id: "2" });
    expect(parseLoopArgs("stop #3")).toEqual({ kind: "stop", id: "3" });
    expect(parseLoopArgs("停止")).toEqual({ kind: "stop", id: "" });
    expect(parseLoopArgs("10m 检查CI是否通过")).toEqual({ kind: "create", text: "10m 检查CI是否通过" });
    expect(parseLoopArgs("stop the build when red")).toEqual({ kind: "create", text: "stop the build when red" });
  });

  it("describes status and list", () => {
    expect(loopStatus(lp({ pending: true, runs: 2 }))).toBe("到点了，等这一回合结束后发送 · 已发 2 次");
    const now = new Date(2026, 9, 27, 8, 0).getTime() / 1000;
    expect(loopStatus(lp({ next_run: new Date(2026, 9, 27, 9, 0).getTime() / 1000, skipped: 1 }), now)).toBe("下次 今天 09:00 · 忙时跳过 1 次");
    expect(loopListText([])).toContain("没有循环");
    expect(loopListText([lp(), lp({ id: "2", schedule_text: "每天 09:00", prompt: "总结" })])).toBe("本会话 2 个循环：#1 每 10 分钟 · 检查CI是否通过；#2 每天 09:00 · 总结");
  });

  it("converts to an automation draft", () => {
    const a = loopToAutomation(lp({ schedule: { kind: "daily", time: "09:00", weekdays: [] } }), "/repo", "deepseek");
    expect(a).toMatchObject({ name: "检查CI是否通过", prompt: "检查CI是否通过", cwd: "/repo", mode: "read-only", model: "deepseek", fromLoop: { id: "1", threadId: "t1" } });
    expect(a.schedule).toEqual({ kind: "daily", time: "09:00", weekdays: [] });
  });
});

describe("运行历史", () => {
  it("formats duration, cost and worktree summary", () => {
    expect(formatDuration(45000)).toBe("45 秒");
    expect(formatDuration(200000)).toBe("3 分 20 秒");
    expect(formatDuration(3900000)).toBe("1 小时 5 分");
    expect(formatCost({ cost: 0.01234, currency: "¥", prompt: 1 })).toBe("¥0.0123");
    expect(formatCost({ cost: null, prompt: 100 })).toBe("花费未知");
    expect(formatCost(undefined)).toBe("");
    expect(worktreeChangeText({ path: "p", branch: "b", file_count: 3, additions: 12, deletions: 4 })).toBe("3 个文件 +12 −4");
    expect(worktreeChangeText({ path: "p", branch: "b", file_count: 0 })).toBe("没有改动");
    expect(worktreeStateLabel({ path: "p", branch: "b", state: "merged", merged: "squash" })).toBe("已压成一份放进暂存区");
    expect(worktreeStateLabel({ path: "p", branch: "b", state: "clean" })).toBe("没有改动，已自动清理");
    const now = new Date(2026, 9, 27, 12, 0).getTime() / 1000;
    const started = new Date(2026, 9, 27, 9, 0).getTime() / 1000;
    expect(runTimeline({ id: "r", automation_id: "a", name: "n", trigger: "manual", started, finished: started + 200, status: "completed", thread_id: "t", summary: "", unread: false, duration_ms: 200000, usage: { cost: 0.5, currency: "¥" } }, now)).toBe("今天 09:00 开始 · 用时 3 分 20 秒 · ¥0.5000");
  });
});
