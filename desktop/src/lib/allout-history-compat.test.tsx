// @vitest-environment jsdom
/**
 * 旧 allout/turn_end 状态的桌面回放兼容（合成 fixture，不读取真实会话）。
 *
 * 旧会话里：
 * - turn_end 没有 delivery；
 * - allout_plan 没有 judge_slots/explore_slots/per_lane_gb/limiting 等新资源字段；
 * - allout_candidates 没有 comparisons；
 * 打开/渲染不能崩，旧 error/interrupted 不能显示成成功；新回合必须清掉旧 allout 状态。
 */
import * as TT from "@radix-ui/react-tooltip";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { Conversation } from "../components/Conversation";
import { applyItemNotification } from "./reduce";
import { deliveryText, turnEndText } from "./terminal";
import { useApp } from "./store";
import { useAutomations } from "./automationStore";
import { useLoops } from "./loopStore";
import type { Item, ThreadDetail, ThreadSummary } from "../types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let mounted: Root | null = null;

afterEach(() => {
  if (mounted) act(() => mounted!.unmount());
  mounted = null;
  document.body.innerHTML = "";
});

beforeEach(() => {
  useLoops.setState({ loops: [] } as never);
  useAutomations.setState({ automations: [] } as never);
});

const OLD_PLAN = {
  candidates: 2,
  repair_rounds: 2,
  effort: "",
  max_steps: 50,
  temperature_ladder: [0, 0.3],
  verified: true,
  reasons: ["旧计划原因"],
  degradations: [],
  // 故意缺少新字段：selection_grace_seconds / feedback_chars / explore_slots /
  // judge_slots / per_lane_gb / limiting。
};

function oldDetail(extra: Partial<ThreadDetail> = {}): ThreadDetail {
  const summary: ThreadSummary = {
    id: "old-session", title: "旧会话", cwd: "/w", model: "old-model",
    mode: "default", status: "idle",
  };
  return {
    ...summary,
    items: [
      { id: "u1", type: "user", text: "旧任务", seq: 1 } as Item,
      { id: "a1", type: "assistant", text: "旧回答", reasoning: "", seq: 2 } as Item,
    ],
    usage: {},
    context_left: null,
    shell: null,
    git: null,
    ...extra,
  };
}

function endItem(reason: string, seq = 3): Item {
  return { id: `e-${reason}`, type: "turn_end", reason, duration_ms: 5, usage: {}, seq } as Item;
}

function open(detail: ThreadDetail) {
  const summary: ThreadSummary = {
    id: detail.id, title: detail.title, cwd: detail.cwd, model: detail.model,
    mode: detail.mode, status: detail.status,
  };
  act(() =>
    useApp.setState({
      rpc: null,
      activeThreadId: detail.id,
      activeWorkspace: "/w",
      workspaces: ["/w"],
      rightOpen: false,
      threads: [summary],
      details: { [detail.id]: detail },
    } as never),
  );
}

function renderConversation(detail: ThreadDetail): string {
  open(detail);
  const el = document.createElement("div");
  document.body.appendChild(el);
  const root = createRoot(el);
  mounted = root;
  act(() => root.render(<TT.Provider><Conversation /></TT.Provider>));
  return el.textContent ?? "";
}

describe("旧 turn_end 兼容", () => {
  it("缺 delivery 的 error 渲染为出错，不显示成完成", () => {
    const text = renderConversation(oldDetail({ items: [endItem("error")] }));
    expect(text).toContain("出错");
    expect(text).not.toContain("交付已复验");
    expect(deliveryText(undefined)).toBe("");
    expect(turnEndText("error")).toBe("出错");
  });

  it("旧 interrupted 渲染为已停止，不显示成完成", () => {
    const text = renderConversation(oldDetail({ items: [endItem("interrupted")] }));
    expect(text).toContain("已停止");
    expect(text).not.toContain("完成");
    expect(turnEndText("interrupted")).toBe("已停止");
  });

  it("没有 allout 状态的旧会话也能打开渲染", () => {
    const text = renderConversation(oldDetail());
    expect(text).toContain("旧任务");
    expect(text).toContain("旧回答");
  });
});

describe("旧 allout 状态兼容", () => {
  it("缺 comparisons/资源字段的计划与候选不崩溃，且不编造对照/资源数字", () => {
    const text = renderConversation(oldDetail({
      items: [endItem("error")],
      allout_plan: OLD_PLAN,
      allout_candidates: {
        "cand-1": { state: "passed", tests: 1, files: 1, votes: 1 },
      },
    }));
    expect(text).toContain("本轮 2 路");
    expect(text).toContain("每段 50 步");
    expect(text).toContain("验收 1 槽");
    expect(text).toContain("候选 1");
    expect(text).toContain("净胜票 1");
    // 新字段缺失时不能显示成 0 或伪造值。
    expect(text).not.toContain("每路估算");
    expect(text).not.toContain("只读探索");
    expect(text).not.toContain("组完整对照");
  });

  it("新回合开始清掉旧 allout 计划/候选/赢家/评审/启动数", () => {
    const old = oldDetail({
      allout_plan: OLD_PLAN,
      allout_candidates: { "cand-1": { state: "passed", votes: 1 } },
      allout_winner: "cand-1",
      allout_review: "旧评审",
      allout_started: 2,
      allout_requested: 6,
    });
    const next = applyItemNotification(old, "item/started", {
      item: { id: "u2", type: "user", text: "新任务", seq: 4 },
    });
    expect(next.allout_plan).toBeUndefined();
    expect(next.allout_candidates).toBeUndefined();
    expect(next.allout_winner).toBeUndefined();
    expect(next.allout_review).toBeUndefined();
    expect(next.allout_started).toBeUndefined();
    expect(next.allout_requested).toBeUndefined();
  });
});
