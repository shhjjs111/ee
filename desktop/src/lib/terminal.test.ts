import { describe, expect, it } from "vitest";
import {
  CARD_TEXT,
  FAILED_REASONS,
  NOTIFY_HEAD,
  NOTIFY_KIND,
  STOP_REASONS,
  TURN_END_TEXT,
  cardText,
  deliveryReason,
  deliveryText,
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

describe("交付结论文案", () => {
  it("完成但没独立判据要显示「未独立验收」，不能和已验证混在一起", () => {
    expect(deliveryText({ status: "unverified" })).toBe("未独立验收");
    expect(deliveryText({ status: "verified" })).toBe("交付已复验");
    expect(deliveryText({ status: "failed" })).toBe("交付复验失败");
    expect(deliveryText(undefined)).toBe("");
  });

  it("写回失败要说清是「补丁没写回」，不说成复验失败", () => {
    const why = deliveryReason({ status: "failed_apply" });
    expect(why).toContain("没能写回");
    expect(why).toContain("worktree");
  });

  it("验收依据变化要带上具体文件，用户才知道去确认什么", () => {
    const why = deliveryReason({ status: "needs_review", acceptance: { changed: true, detail: "pytest.ini" } });
    expect(why).toContain("验收依据发生变化");
    expect(why).toContain("pytest.ini");
  });

  it("复验失败用判据自己的原因兜底，拿不到就不编", () => {
    expect(deliveryReason({ status: "failed", target: { met: false, reason: "exit=1：命令以非零退出" } })).toContain("exit=1");
    expect(deliveryReason({ status: "failed" }), "没有原因时宁可不写").toBe("");
  });

  it("已验证与无交付结论都不附原因", () => {
    expect(deliveryReason({ status: "verified" })).toBe("");
    expect(deliveryReason(undefined)).toBe("");
  });

  it("needs_review 按证据分岔：改测试 / 扫描不完整 / 外部改动不说成同一句话", () => {
    const tests = deliveryReason({
      status: "needs_review",
      acceptance: { changed: true, diff: { tests_changed: ["tests/test_a.py"], tests_removed: ["tests/test_b.py"] } },
    });
    expect(tests).toContain("被改写");
    expect(tests).toContain("tests/test_a.py");
    expect(tests).toContain("不见了");

    const scan = deliveryReason({ status: "needs_review", acceptance: { changed: true, diff: { tests_incomplete: true } } });
    expect(scan).toContain("无法完整扫描");

    const cmd = deliveryReason({ status: "needs_review", acceptance: { changed: true, diff: { command: true } } });
    expect(cmd).toContain("判据命令变了");

    const outside = deliveryReason({ status: "needs_review", apply: { unexpected: ["README.md"] } });
    expect(outside).toContain("补丁之外");
    expect(outside).toContain("README.md");
    expect(outside, "扫描没报缺口时不能凭空说验收依据变了").not.toContain("验收依据发生变化");
  });

  it("unexpected 三态各不相同：null=没检查成，空数组=检查干净，非空=真被改了", () => {
    const unknown = deliveryReason({ status: "needs_review", apply: { unexpected: null } });
    expect(unknown).toContain("无法检查补丁之外的改动");
    expect(unknown).not.toContain("验收依据发生变化");

    const clean = deliveryReason({ status: "needs_review", apply: { unexpected: [] } });
    expect(clean, "空数组代表检查干净，不能报成外部改动").not.toContain("补丁之外");
    expect(clean, "但也不能编一个原因").toContain("无法确认");

    const dirty = deliveryReason({ status: "needs_review", apply: { unexpected: ["src/a.py"] } });
    expect(dirty).toContain("src/a.py");
  });
});
