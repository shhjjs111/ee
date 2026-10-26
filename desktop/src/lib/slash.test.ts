import { describe, expect, it } from "vitest";
import { isSkillCommand, parseSlash, resolveMode, slashItems, type SkillInfo } from "./slash";

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
      "builtin:compact", "builtin:new", "builtin:undo", "builtin:model", "builtin:mode", "builtin:effort", "builtin:style", "builtin:plan", "builtin:memory",
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
    expect(slashItems("/mode ", prompts, models)!.length).toBe(4);
    expect(resolveMode("计划")).toBe("plan");
    expect(slashItems("/mode 全", prompts, models)![0].action).toEqual({ run: "/mode auto" });
  });

  it("hides menu for plain text, multi-line and args", () => {
    expect(slashItems("hi", prompts, models)).toBeNull();
    expect(slashItems("/review a.py", prompts, models)).toBeNull();
    expect(slashItems("/mo\nx", prompts, models)).toBeNull();
  });

  it("lists enabled skills after prompts with a badge", () => {
    const skills: SkillInfo[] = [
      { name: "pdf-tools", description: "处理 PDF", path: "/h/skills/pdf-tools/SKILL.md", dir: "/h/skills/pdf-tools", source: "user", origin: "ee", enabled: true },
      { name: "init", description: "和自定义提示同名", path: "x", dir: "x", source: "project", origin: "claude", enabled: true },
      { name: "undo", description: "和内置命令同名", path: "x", dir: "x", source: "user", origin: "ee", enabled: true },
      { name: "off", description: "已禁用", path: "x", dir: "x", source: "user", origin: "ee", enabled: false },
      { name: "中文技能", description: "中文名", path: "x", dir: "x", source: "project", origin: "ee", enabled: true },
    ];
    const all = slashItems("/", prompts, models, skills)!;
    expect(all.slice(-3).map((i) => i.key)).toEqual(["prompt:init", "skill:pdf-tools", "skill:中文技能"]);
    const pdf = all.find((i) => i.key === "skill:pdf-tools")!;
    expect(pdf.badge).toBe("技能");
    expect(pdf.hint).toBe("处理 PDF");
    expect(pdf.action).toEqual({ fill: "/pdf-tools " });
    expect(slashItems("/pd", prompts, models, skills)!.map((i) => i.key)).toEqual(["skill:pdf-tools"]);
    expect(slashItems("/中", prompts, models, skills)!.map((i) => i.key)).toEqual(["skill:中文技能"]);
    expect(slashItems("/off", prompts, models, skills)).toEqual([]);
    expect(isSkillCommand("pdf-tools", prompts, skills)).toBe(true);
    expect(isSkillCommand("init", prompts, skills)).toBe(false);
    expect(isSkillCommand("off", prompts, skills)).toBe(false);
    // 不传技能时和以前一样
    expect(slashItems("/", prompts, models)!.some((i) => i.key.startsWith("skill:"))).toBe(false);
  });

  it("resolves modes", () => {
    expect(resolveMode("只读")).toBe("read-only");
    expect(resolveMode("AUTO")).toBe("auto");
    expect(resolveMode("xx")).toBeNull();
  });
});

describe("D5-9 /effort 与 /style", () => {
  it("解析推理强度与风格", async () => {
    const { resolveEffort, resolveStyle, effortLabel } = await import("./slash");
    expect(resolveEffort("高")).toBe("high");
    expect(resolveEffort("low")).toBe("low");
    expect(resolveEffort("默认")).toBe("");
    expect(resolveEffort("关闭")).toBe("off");
    expect(resolveEffort("超级")).toBeNull();
    expect(effortLabel("medium")).toBe("中");
    const styles = [{ id: "default", label: "默认" }, { id: "tutor", label: "作业辅导" }];
    expect(resolveStyle("作业辅导", styles)).toBe("tutor");
    expect(resolveStyle("tutor", styles)).toBe("tutor");
    expect(resolveStyle("nope", styles)).toBeNull();
  });
  it("子菜单列出档位和风格", async () => {
    const { slashItems } = await import("./slash");
    const eff = slashItems("/effort ", [], [], [], [])!;
    expect(eff.map((x) => x.label)).toEqual(["默认", "低", "中", "高", "关闭思考"]);
    expect(eff[3].action).toEqual({ run: "/effort high" });
    const st = slashItems("/style 作业", [], [], [], [{ id: "tutor", label: "作业辅导", description: "引导" }])!;
    expect(st).toHaveLength(1);
    expect(st[0].action).toEqual({ run: "/style tutor" });
    expect(slashItems("/eff", [], [])!.some((x) => x.label.startsWith("/effort"))).toBe(true);
  });
});
