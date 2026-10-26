import { describe, expect, it } from "vitest";
import { fixMessage, location, splitReview } from "./review";

const text = [
  "总评：有一个越界。",
  "",
  "```ee-review",
  '{"overall":"incorrect","summary":"越界","findings":[',
  '{"priority":3,"title":"小","body":"","file":"a.py","line_start":1,"line_end":1},',
  '{"priority":"1","title":"越界","body":"x 为空时崩溃","file":"a.py","line_start":2,"line_end":4,"confidence":0.9},',
  '{"body":"无标题"}]}',
  "```",
].join("\n");

describe("splitReview", () => {
  it("拆出正文和问题，按优先级排序", () => {
    const { body, result } = splitReview(text);
    expect(body).toBe("总评：有一个越界。");
    expect(result!.overall).toBe("incorrect");
    expect(result!.findings.map((f) => f.title)).toEqual(["越界", "小"]);
    expect(location(result!.findings[0])).toBe("a.py:2-4");
    expect(location(result!.findings[1])).toBe("a.py:1");
  });
  it("没有块原样返回；坏 JSON 只去掉块", () => {
    expect(splitReview("普通回答")).toEqual({ body: "普通回答", result: null });
    expect(splitReview("正文\n```ee-review\n{坏\n```")).toEqual({ body: "正文", result: null });
  });
  it("流式输出中未闭合的块也不显示", () => {
    expect(splitReview('正文\n```ee-review\n{"overall":').body).toBe("正文");
  });
  it("修复消息列出选中的问题", () => {
    const m = fixMessage(splitReview(text).result!.findings.slice(0, 1));
    expect(m).toContain("1. [P1] 越界（a.py:2-4）");
    expect(m).toContain("x 为空时崩溃");
  });
});
