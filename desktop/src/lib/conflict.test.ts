import { describe, expect, it } from "vitest";
import { conflictPrompt } from "./store";

describe("conflictPrompt", () => {
  it("列出被手改过的文件并说明会先存恢复点", () => {
    const t = conflictPrompt(["a.txt", "src/b.ts"], "撤销上一回合");
    expect(t).toContain("撤销上一回合");
    expect(t).toContain("a.txt\nsrc/b.ts");
    expect(t).toContain("检查点");
  });
  it("文件太多时只列前 10 个", () => {
    const files = Array.from({ length: 12 }, (_, i) => `f${i}.txt`);
    const t = conflictPrompt(files, "编辑重发");
    expect(t).toContain("f9.txt");
    expect(t).not.toContain("f10.txt");
    expect(t).toContain("等 12 个文件");
  });
});
