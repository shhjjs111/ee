// @vitest-environment jsdom
import * as TT from "@radix-ui/react-tooltip";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import type { ApprovalItem, ToolItem } from "../types";
import { SubagentGroup } from "./AgentItems";
import { ApprovalCard } from "./Items";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => {
  document.body.innerHTML = "";
});

function task(id: string, desc: string, sub: Record<string, unknown>, status = "running"): ToolItem {
  return {
    id: `tool_${id}`,
    type: "tool",
    call_id: id,
    name: "task",
    args: { description: desc },
    summary: "",
    status,
    output: "",
    diff: "",
    meta: { kind: "subagent", subagent: { id, agent: "explore", description: desc, model: "m", mode: "read-only", max_steps: 30, step: 0, tools: [], text: "", summary: "", ...sub } },
  } as unknown as ToolItem;
}

function render(node: React.ReactNode) {
  const el = document.createElement("div");
  document.body.appendChild(el);
  act(() => createRoot(el).render(<TT.Provider>{node}</TT.Provider>));
  return el;
}

describe("SubagentGroup", () => {
  it("同时显示多个运行中的子代理和各自进度", () => {
    const el = render(
      <SubagentGroup
        items={[
          task("p0", "查甲", { status: "running", step: 2, tools: [{ id: "k1", name: "read", summary: "Read a.ts", status: "ok" }] }),
          task("p1", "查乙", { status: "running", step: 1, tools: [{ id: "k1", name: "grep", summary: "Grep foo", status: "running" }] }),
          task("p2", "查丙", { status: "queued" }),
          task("p3", "写丁", { status: "completed", agent: "general", mode: "auto", summary: "完成", worktree: { path: "/w", branch: "ee/agent-1", files: [{ path: "b.txt", additions: 1, deletions: 0 }], file_count: 1, additions: 1, deletions: 0 } }, "ok"),
        ]}
      />,
    );
    const text = el.textContent ?? "";
    expect(text).toContain("4 个子代理并行");
    expect(text).toContain("2 个进行中");
    expect(text).toContain("1 个排队中");
    expect(el.querySelectorAll('[data-testid="subagent-card"]').length).toBe(4);
    // 运行中的两张自动展开，各自显示自己的工具步骤
    expect(text).toContain("Read a.ts");
    expect(text).toContain("Grep foo");
    expect(text).toContain("2/30 步");
    expect(text).toContain("worktree");
  });
});

describe("SubagentCard fork", () => {
  it("分叉子代理显示分叉标识", () => {
    const el = render(
      <SubagentGroup items={[task("p9", "旁支", { status: "completed", summary: "好", fork: true }, "ok"), task("p8", "普通", { status: "completed" }, "ok")]} />,
    );
    const cards = el.querySelectorAll('[data-testid="subagent-card"]');
    expect(cards[0].textContent).toContain("分叉");
    expect(cards[1].textContent).not.toContain("分叉");
  });
});

describe("ApprovalCard", () => {
  it("子代理发出的审批标明来源", () => {
    const item = {
      id: "a1",
      type: "approval",
      call_id: "p1/k1",
      tool: "shell",
      title: "[子代理 general] 运行命令：curl x",
      detail: "curl x",
      prefix: "curl",
      status: "pending",
      source: { call_id: "p1", agent: "general", description: "命令乙" },
    } as unknown as ApprovalItem;
    const el = render(<ApprovalCard item={item} />);
    const src = el.querySelector('[data-testid="approval-source"]');
    expect(src?.textContent).toContain("来自子代理 general · 命令乙");
  });
});
