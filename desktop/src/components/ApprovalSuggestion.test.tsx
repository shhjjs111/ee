// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import type { ApprovalItem } from "../types";
import { ApprovalCard } from "./Items";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => {
  document.body.innerHTML = "";
});

function approval(over: Partial<ApprovalItem> = {}): ApprovalItem {
  return {
    id: "it_a1",
    type: "approval",
    call_id: "c1",
    tool: "shell",
    title: "运行命令：git status",
    detail: "git status",
    prefix: "shell(git status *)",
    status: "pending",
    ...over,
  } as ApprovalItem;
}

function mount(item: ApprovalItem) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  act(() => {
    createRoot(host).render(<ApprovalCard item={item} />);
  });
  return () => document.body.textContent ?? "";
}

describe("ApprovalCard 的自动审批建议（T10-c）", () => {
  it("有合格建议时把它显示出来，让用户知道可写入什么规则", () => {
    const view = mount(approval({
      suggestion: {
        eligible: true,
        rule: "shell(git status *)",
        blocked_by: "",
        summary: "以后遇到相同模式（shell(git status *)）不再询问",
        hits: 3,
        reason: "",
        action: "ask",
      },
    }));
    const text = view();
    expect(text).toContain("以后遇到相同模式");
    expect(text).toContain("shell(git status *)");
  });

  it("不合格时说明被哪一条挡住（force / 只读 / 未开启）", () => {
    const view = mount(approval({
      suggestion: {
        eligible: false,
        rule: "",
        blocked_by: "force：强制询问，任何模式下都要问",
        summary: "",
        hits: 0,
        reason: "",
        action: "ask",
      },
    }));
    expect(view()).toContain("force");
  });

  it("没有 suggestion 字段时界面不显示建议区（老数据兼容）", () => {
    const view = mount(approval());
    const text = view();
    // 审批本身照常显示
    expect(text).toContain("需要你的批准");
    // 但不该冒出「建议」「可以放行」之类的字样
    expect(text).not.toContain("以后遇到相同模式");
  });

  it("建议区不提供任何「自动放行」按钮——决策权仍在用户", () => {
    const view = mount(approval({
      suggestion: {
        eligible: true, rule: "shell(git status *)", blocked_by: "",
        summary: "以后遇到相同模式不再询问", hits: 1, reason: "", action: "ask",
      },
    }));
    const host = document.body;
    const buttons = [...host.querySelectorAll("button")].map((b) => b.textContent ?? "");
    // 不能出现「自动批准/自动放行」这类越权入口
    expect(buttons.some((t) => t.includes("自动放行") || t.includes("自动批准"))).toBe(false);
    expect(view()).toContain("需要你的批准");
  });
});
