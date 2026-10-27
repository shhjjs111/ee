import { describe, expect, it } from "vitest";
import { formatWhen, hunkPatch, pendingCount, splitPatch } from "./hunks";

const PATCH = [
  "diff --git a/中文.txt b/中文.txt",
  "--- a/中文.txt",
  "+++ b/中文.txt",
  "@@ -1,3 +1,3 @@",
  " 一",
  "-二",
  "+二（改）",
  " 三",
  "@@ -20,2 +20,3 @@ def f():",
  " x\r",
  "+y\r",
  " z",
  "\\ No newline at end of file",
].join("\n");

describe("splitPatch", () => {
  it("拆出文件头和各块，去掉行尾 \\r，保留无结尾换行标记", () => {
    const { head, hunks } = splitPatch(PATCH);
    expect(head).toHaveLength(3);
    expect(hunks.map((h) => h.header)).toEqual(["@@ -1,3 +1,3 @@", "@@ -20,2 +20,3 @@ def f():"]);
    expect(hunks[1].lines).toEqual([" x", "+y", " z", "\\ No newline at end of file"]);
  });
  it("没有块 / 空 patch", () => {
    expect(splitPatch("").hunks).toEqual([]);
    expect(splitPatch("diff --git a/x b/x\nBinary files differ").hunks).toEqual([]);
  });
  it("hunkPatch 只含文件头和那一块", () => {
    const p = hunkPatch(PATCH, 1);
    expect(p.startsWith("diff --git")).toBe(true);
    expect(p).toContain("+y");
    expect(p).not.toContain("二（改）");
    expect(hunkPatch(PATCH, 5)).toBe("");
  });
});

describe("helpers", () => {
  it("pendingCount", () => {
    expect(pendingCount([{ accepted: true }, { accepted: false }])).toBe(1);
    expect(pendingCount(undefined)).toBe(0);
  });
  it("formatWhen 今天只显示时分", () => {
    const now = new Date(2026, 9, 27, 12, 0);
    expect(formatWhen(new Date(2026, 9, 27, 9, 5).getTime() / 1000, now)).toBe("09:05");
    expect(formatWhen(new Date(2026, 9, 3, 18, 30).getTime() / 1000, now)).toBe("10月3日 18:30");
    expect(formatWhen(null, now)).toBe("");
  });
});
