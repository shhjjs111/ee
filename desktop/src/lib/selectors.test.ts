import { describe, expect, it } from "vitest";
import { selectActiveDetail, selectActiveThreadId, selectActiveTurnStartedAt } from "./selectors";

describe("selectActiveThreadId", () => {
  it("返回活动会话 id，稳定字符串", () => {
    expect(selectActiveThreadId({ activeThreadId: "t1" })).toBe("t1");
    expect(selectActiveThreadId({ activeThreadId: null })).toBeNull();
  });
});

describe("selectActiveDetail", () => {
  it("返回活动会话的详情对象，引用稳定", () => {
    const d = { id: "t1", cwd: "/w", items: [] } as any;
    const s = { activeThreadId: "t1", details: { t1: d } };
    expect(selectActiveDetail(s)).toBe(d);
  });

  it("无关字段变化（toast/search/threads）不改变返回引用", () => {
    const d = { id: "t1", cwd: "/w", items: [] } as any;
    const base = { activeThreadId: "t1", details: { t1: d } };
    const changed = {
      ...base,
      toast: { text: "x", kind: "info" },
      search: "abc",
      threads: [{ id: "other" }],
      details: { ...base.details, other: { id: "other", items: [] } },
    };
    expect(selectActiveDetail(changed as any)).toBe(d);
  });

  it("其他会话 detail 重建时，本会话引用不变（这是窄订阅的关键）", () => {
    const d = { id: "t1", cwd: "/w", items: [] } as any;
    const s = { activeThreadId: "t1", details: { t1: d } };
    const changed = { ...s, details: { t1: d, other: { id: "other", items: [1] } } };
    expect(selectActiveDetail(changed as any)).toBe(d);
  });

  it("无活动会话或详情不存在返回 null", () => {
    expect(selectActiveDetail({ activeThreadId: null, details: {} })).toBeNull();
    expect(selectActiveDetail({ activeThreadId: "t1", details: {} })).toBeNull();
  });
});

describe("selectActiveTurnStartedAt", () => {
  it("返回本会话的计时起点，数字稳定", () => {
    expect(selectActiveTurnStartedAt({ activeThreadId: "t1", turnStartedAt: { t1: 100 } })).toBe(100);
    expect(selectActiveTurnStartedAt({ activeThreadId: null, turnStartedAt: {} })).toBeUndefined();
    expect(selectActiveTurnStartedAt({ activeThreadId: "t1", turnStartedAt: {} })).toBeUndefined();
  });
});
