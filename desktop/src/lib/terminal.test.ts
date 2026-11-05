import { describe, expect, it } from "vitest";
import {
  CARD_TEXT,
  FAILED_REASONS,
  NOTIFY_HEAD,
  NOTIFY_KIND,
  STOP_REASONS,
  TURN_END_TEXT,
  cardText,
  turnEndText,
} from "./terminal";

describe("停止原因：三张表必须覆盖同一个取值集合", () => {
  it("每个取值在三张表里都有文案/类型，不会落进默认分支", () => {
    for (const r of STOP_REASONS) {
      expect(TURN_END_TEXT[r], `${r} 的回合结束文案`).toBeTruthy();
      expect(CARD_TEXT[r], `${r} 的卡片状态词`).toBeTruthy();
      expect(NOTIFY_HEAD[r], `${r} 的通知标题`).toBeTruthy();
      expect(NOTIFY_KIND[r], `${r} 的通知类型`).toBeDefined();
    }
  });

  it("跑满轮次未达标（exhausted）不能显示成「完成」或「空闲」", () => {
    expect(turnEndText("exhausted")).not.toBe("完成");
    expect(turnEndText("exhausted")).toContain("未达成");
    expect(cardText("exhausted")).not.toBe("空闲");
  });

  it("目标达成（met）在卡片上不能显示成「空闲」", () => {
    expect(cardText("met")).not.toBe("空闲");
    expect(turnEndText("met")).toBe("完成");
  });

  it("失败类结束方式一律要弹通知；用户主动停止不弹", () => {
    for (const r of FAILED_REASONS) {
      expect(NOTIFY_KIND[r as keyof typeof NOTIFY_KIND], `${r} 应当通知`).not.toBeNull();
    }
    expect(NOTIFY_KIND.interrupted).toBeNull();
  });

  it("未知 reason 回落到调用方的默认词，不抛", () => {
    expect(turnEndText("从没见过的值")).toBe("完成");
    expect(cardText(undefined)).toBe("空闲");
  });
});
