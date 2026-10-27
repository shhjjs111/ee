import { describe, expect, it } from "vitest";
import { changedFiles, exploreHint, truncationText } from "./details";

const tool = (name: string, details: any, status = "ok") =>
  ({ id: "x", type: "tool", name, call_id: "x", args: {}, summary: "", status, output: "", details }) as any;

describe("details", () => {
  it("read 行范围", () => {
    expect(exploreHint(tool("read", { kind: "read", start: 1, end: 20, total_lines: 20 }))).toBe("20 行");
    expect(exploreHint(tool("read", { kind: "read", start: 1, end: 200, total_lines: 500 }))).toBe("第 1–200 行 / 共 500 行");
    expect(exploreHint(tool("read", { kind: "read", ref: true }))).toBe("未改变");
    expect(exploreHint(tool("read", { kind: "read", start: 1, end: 2, total_lines: 2 }, "error"))).toBe("");
  });
  it("搜索与列表", () => {
    expect(exploreHint(tool("grep", { kind: "search", matches: 3, files: ["a", "b"] }))).toBe("3 处 · 2 个文件");
    expect(exploreHint(tool("grep", { kind: "search", matches: 0, files: [] }))).toBe("没有匹配");
    expect(exploreHint(tool("glob", { kind: "list", files: ["a"], total: 7 }))).toBe("7 项");
  });
  it("改动文件列表兼容单文件写法", () => {
    expect(changedFiles(tool("write", { kind: "edit", path: "a.py", verb: "Created", added: 2, removed: 0 }))).toEqual([
      { verb: "Created", path: "a.py", added: 2, removed: 0 },
    ]);
    const files = [{ verb: "Edited", path: "b", added: 1, removed: 1 }];
    expect(changedFiles(tool("apply_patch", { kind: "edit", files }))).toEqual(files);
    expect(changedFiles(tool("edit", {}))).toEqual([]);
  });
  it("截断提示", () => {
    expect(truncationText({ truncated: { lines: 5000, bytes: 50000, path: "/x" } })).toContain("5,000 行");
    expect(truncationText({})).toBeNull();
  });
});
