import { describe, expect, it } from "vitest";
import { automationNotice, describeSchedule, formatNextRun, parseHHMM, runStatus, untilText } from "./schedule";

const at = (y: number, mo: number, d: number, h = 0, mi = 0) => new Date(y, mo - 1, d, h, mi).getTime() / 1000;

describe("schedule", () => {
  it("describes schedules", () => {
    expect(describeSchedule({ kind: "interval", minutes: 30 })).toBe("每 30 分钟");
    expect(describeSchedule({ kind: "interval", minutes: 120 })).toBe("每 2 小时");
    expect(describeSchedule({ kind: "interval", minutes: 2880 })).toBe("每 2 天");
    expect(describeSchedule({ kind: "daily", time: "09:00", weekdays: [] })).toBe("每天 09:00");
    expect(describeSchedule({ kind: "daily", time: "09:00", weekdays: [5, 1, 2, 3, 4] })).toBe("工作日 09:00");
    expect(describeSchedule({ kind: "daily", time: "10:00", weekdays: [0, 6] })).toBe("周末 10:00");
    expect(describeSchedule({ kind: "daily", time: "08:30", weekdays: [3, 1] })).toBe("每周一、周三 08:30");
    expect(describeSchedule({ kind: "cron", expr: "0 9 * * 1-5" })).toBe("cron：0 9 * * 1-5");
    expect(describeSchedule(null)).toBe("未设置");
  });

  it("formats next run relative to now", () => {
    const now = at(2026, 10, 26, 10, 0); // 周一
    expect(formatNextRun(at(2026, 10, 26, 18, 5), now)).toBe("今天 18:05");
    expect(formatNextRun(at(2026, 10, 27, 9, 0), now)).toBe("明天 09:00");
    expect(formatNextRun(at(2026, 10, 29, 9, 0), now)).toBe("周四 09:00");
    expect(formatNextRun(at(2026, 11, 2, 9, 0), now)).toBe("11月2日 09:00"); // 跨月
    expect(formatNextRun(at(2027, 1, 1, 0, 0), now)).toBe("2027年1月1日 00:00"); // 跨年
    expect(formatNextRun(null, now)).toBe("—");
    expect(untilText(now + 30, now)).toBe("即将运行");
    expect(untilText(now + 600, now)).toBe("10 分钟后");
    expect(untilText(now + 7200, now)).toBe("2 小时后");
  });

  it("status, notice, hhmm", () => {
    expect(runStatus("completed").label).toBe("已完成");
    expect(runStatus("weird").label).toBe("weird");
    const run = { id: "r", automation_id: "a", name: "早检", trigger: "schedule", started: 1, finished: 2, status: "completed", thread_id: "t1", summary: "没问题", unread: true } as const;
    expect(automationNotice({ ...run })).toEqual({ kind: "automation", threadId: "t1", title: "自动化 · 早检 · 已完成", body: "没问题" });
    expect(automationNotice({ ...run, status: "skipped" })).toBeNull();
    expect(automationNotice({ ...run, status: "failed", thread_id: null })?.title).toContain("启动失败");
    expect(parseHHMM("9:05")).toBe("09:05");
    expect(parseHHMM("24:00")).toBeNull();
    expect(parseHHMM("abc")).toBeNull();
  });
});
