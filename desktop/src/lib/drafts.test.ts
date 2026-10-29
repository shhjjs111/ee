// 草稿收件箱的边界用例（T11 / ACC-20、ACC-21）。
// 每条都对应一个真实会丢用户文字的场景。
import { describe, expect, it } from "vitest";
import { hasReturned, mergeDraft, pushReturned, takeReturned, type ReturnedInbox } from "./drafts";

const empty: ReturnedInbox = {};

describe("pushReturned", () => {
  it("两个不同会话先后退回：互不覆盖（ACC-20）", () => {
    let box = pushReturned(empty, "A", [{ id: "a1", text: "草稿 A" }]);
    box = pushReturned(box, "B", [{ id: "b1", text: "草稿 B" }]);
    expect(box.A?.map((x) => x.text)).toEqual(["草稿 A"]);
    expect(box.B?.map((x) => x.text)).toEqual(["草稿 B"]);
  });

  it("同毫秒两条不同 id 都保留（ACC-21）", () => {
    // 原来用 Date.now() 当唯一标识，同毫秒会互相覆盖
    const box = pushReturned(empty, "A", [
      { id: "m1", text: "第一条" },
      { id: "m2", text: "第二条" },
    ]);
    expect(box.A?.map((x) => x.id)).toEqual(["m1", "m2"]);
  });

  it("同一会话追加保持到达顺序", () => {
    let box = pushReturned(empty, "A", [{ id: "1", text: "x" }]);
    box = pushReturned(box, "A", [{ id: "2", text: "y" }]);
    expect(box.A?.map((x) => x.text)).toEqual(["x", "y"]);
  });

  it("重复投递同一 id 只保留一条，且不制造新引用", () => {
    const one = pushReturned(empty, "A", [{ id: "1", text: "x" }]);
    const again = pushReturned(one, "A", [{ id: "1", text: "x" }]);
    expect(again).toBe(one);
    expect(again.A).toHaveLength(1);
  });

  it("空输入不改变引用", () => {
    expect(pushReturned(empty, "A", [])).toBe(empty);
    expect(pushReturned(empty, "", [{ id: "1", text: "x" }])).toBe(empty);
  });
});

describe("takeReturned", () => {
  it("只消费指定 id，同会话其余保留", () => {
    const box = pushReturned(empty, "A", [
      { id: "1", text: "x" },
      { id: "2", text: "y" },
    ]);
    const { inbox, taken } = takeReturned(box, "A", ["1"]);
    expect(taken.map((x) => x.text)).toEqual(["x"]);
    expect(inbox.A?.map((x) => x.id)).toEqual(["2"]);
  });

  it("消费 B 的不会动到 A（用户正在看 A）", () => {
    let box = pushReturned(empty, "A", [{ id: "a1", text: "A 的" }]);
    box = pushReturned(box, "B", [{ id: "b1", text: "B 的" }]);
    const { inbox, taken } = takeReturned(box, "B", ["b1"]);
    expect(taken.map((x) => x.id)).toEqual(["b1"]);
    expect(inbox.A).toHaveLength(1);
    expect(inbox.B).toBeUndefined();
  });

  it("不指定 ids 时取走该会话全部", () => {
    const box = pushReturned(empty, "A", [
      { id: "1", text: "x" },
      { id: "2", text: "y" },
    ]);
    const { inbox, taken } = takeReturned(box, "A");
    expect(taken).toHaveLength(2);
    expect(inbox.A).toBeUndefined();
  });

  it("指定了不存在的 id：什么都不取，引用不变", () => {
    const box = pushReturned(empty, "A", [{ id: "1", text: "x" }]);
    const r = takeReturned(box, "A", ["nope"]);
    expect(r.taken).toEqual([]);
    expect(r.inbox).toBe(box);
  });
});

describe("hasReturned", () => {
  it("按会话判断，不受其他会话影响", () => {
    const box = pushReturned(empty, "A", [{ id: "1", text: "x" }]);
    expect(hasReturned(box, "A")).toBe(true);
    expect(hasReturned(box, "B")).toBe(false);
    expect(hasReturned(box, null)).toBe(false);
  });
});

describe("mergeDraft", () => {
  it("退回内容排在已有文字前面，两边都不丢", () => {
    expect(mergeDraft(["退回的"], "我正在写的")).toBe("退回的\n\n我正在写的");
  });

  it("输入框为空时直接是退回内容", () => {
    expect(mergeDraft(["只有退回"], "")).toBe("只有退回");
  });

  it("空白退回不污染输入框", () => {
    expect(mergeDraft(["", "   "], "原文")).toBe("原文");
  });

  it("多条按顺序拼", () => {
    expect(mergeDraft(["1", "2"], "")).toBe("1\n\n2");
  });
});
