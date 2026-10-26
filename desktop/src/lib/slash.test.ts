import { describe, expect, it } from "vitest";
import { parseSlash, resolveMode, slashItems } from "./slash";

const prompts = [
  { name: "init", description: "生成 AGENTS.md", argument_hint: "", source: "builtin", path: "builtin:init.md" },
  { name: "review", description: "代码审查", argument_hint: "<文件>", source: "user", path: "/h/prompts/review.md" },
  { name: "compact", description: "不应出现", argument_hint: "", source: "user", path: "x" },
];
const models = [{ name: "deepseek-flash" }, { name: "deepseek-pro" }, { name: "qwen-max" }];

describe("slash", () => {
  it("parses", () => {
    expect(parseSlash("/model  deepseek-pro ")).toEqual({ name: "model", args: "deepseek-pro" });
    expect(parseSlash("/compact")).toEqual({ name: "compact", args: "" });
    expect(parseSlash("/review a.py\n第二行")).toEqual({ name: "review", args: "a.py\n第二行" });
    expect(parseSlash("hello /x")).toBeNull();
    expect(parseSlash("/usr/bin/python 是什么")).toBeNull();
  });

  it("lists commands by prefix, prompts after builtins, no duplicates", () => {
    const all = slashItems("/", prompts, models)!;
    expect(all.map((i) => i.key)).toEqual([
      "builtin:compact", "builtin:new", "builtin:undo", "builtin:model", "builtin:mode", "builtin:memory",
      "builtin:review", "builtin:commit", "builtin:mcp",
      "prompt:init",
    ]);
    const m = slashItems("/mo", prompts, models)!;
    expect(m.map((i) => i.key)).toEqual(["builtin:model", "builtin:mode", "builtin:memory"]);
    expect(m[0].action).toEqual({ fill: "/model " });
    expect(slashItems("/comp", prompts, models)![0].action).toEqual({ run: "/compact" });
    // 同名自定义提示被内置 /review 盖住
    expect(slashItems("/rev", prompts, models)!.map((i) => i.key)).toEqual(["builtin:review"]);
    expect(slashItems("/commit", prompts, models)![0].action).toEqual({ run: "/commit" });
  });

  it("second level options for model and mode", () => {
    expect(slashItems("/model pro", prompts, models)!.map((i) => i.action)).toEqual([{ run: "/model deepseek-pro" }]);
    expect(slashItems("/mode ", prompts, models)!.length).toBe(3);
    expect(slashItems("/mode 全", prompts, models)![0].action).toEqual({ run: "/mode auto" });
  });

  it("hides menu for plain text, multi-line and args", () => {
    expect(slashItems("hi", prompts, models)).toBeNull();
    expect(slashItems("/review a.py", prompts, models)).toBeNull();
    expect(slashItems("/mo\nx", prompts, models)).toBeNull();
  });

  it("resolves modes", () => {
    expect(resolveMode("只读")).toBe("read-only");
    expect(resolveMode("AUTO")).toBe("auto");
    expect(resolveMode("xx")).toBeNull();
  });
});
