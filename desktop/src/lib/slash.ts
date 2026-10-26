// 斜杠命令（D3-B）：内置命令 + 后端 prompts/list 的自定义提示 + skills/list 的技能（D5-5）。纯函数，便于测试。

export type PromptInfo = { name: string; description: string; argument_hint: string; source: string; path: string };
/** skills/list 返回的一个技能（ee/core/skills.py Skill.summary）。 */
export type SkillInfo = {
  name: string;
  description: string;
  path: string;
  dir: string;
  source: "user" | "project" | string;
  origin: "ee" | "claude" | "agents" | string;
  enabled: boolean;
  shadows?: string[];
};

export type SlashCommand = {
  name: string;
  description: string;
  argHint?: string;
  takesArgs: boolean; // true：选中后补成 "/name " 等用户继续输入；false：选中即执行
  kind: "builtin" | "prompt" | "skill";
  source?: string;
};

export type SlashItem = {
  key: string;
  label: string;
  hint?: string;
  badge?: string;
  // 选中后：fill = 把输入框改成这段文字；run = 直接执行这段命令文字
  action: { fill: string } | { run: string };
};

export const BUILTIN: SlashCommand[] = [
  { name: "compact", description: "压缩上下文（总结之前的对话，腾出空间）", takesArgs: false, kind: "builtin" },
  { name: "new", description: "开始新对话", takesArgs: false, kind: "builtin" },
  { name: "undo", description: "撤销上一回合的文件改动", takesArgs: false, kind: "builtin" },
  { name: "model", description: "切换模型", argHint: "<模型名>", takesArgs: true, kind: "builtin" },
  { name: "mode", description: "切换权限模式", argHint: "只读 | 默认 | 全自动 | 计划", takesArgs: true, kind: "builtin" },
  { name: "effort", description: "推理强度（本会话，并按模型记住）", argHint: "默认 | 低 | 中 | 高 | 关闭", takesArgs: true, kind: "builtin" },
  { name: "style", description: "回答风格（本会话）", argHint: "默认 | 简洁 | 讲解 | 作业辅导", takesArgs: true, kind: "builtin" },
  { name: "plan", description: "计划模式：先调研出方案，批准后再改（可直接跟任务）", argHint: "[任务]", takesArgs: true, kind: "builtin" },
  { name: "memory", description: "记住一句话（不带文字则打开记忆设置）", argHint: "[要记住的话]", takesArgs: true, kind: "builtin" },
  { name: "review", description: "代码审查：未提交的改动、相对分支、某个提交或按要求（只读）", argHint: "[额外要求]", takesArgs: true, kind: "builtin" },
  { name: "commit", description: "让模型根据改动写提交信息，确认后 git commit", takesArgs: false, kind: "builtin" },
  { name: "mcp", description: "管理 MCP 服务器", takesArgs: false, kind: "builtin" },
];

export const MODE_OPTIONS = [
  { value: "read-only", label: "只读", hint: "只能读文件和运行只读命令" },
  { value: "default", label: "默认", hint: "工作区内改文件免问；其他命令先问你" },
  { value: "auto", label: "全自动", hint: "都不问（deny 规则和危险命令仍会拦）" },
  { value: "plan", label: "计划", hint: "只调研出方案，批准后再改" },
];

export const EFFORT_OPTIONS = [
  { value: "", label: "默认", hint: "不指定，用模型自己的默认强度" },
  { value: "low", label: "低", hint: "想得少、快、省 token" },
  { value: "medium", label: "中", hint: "平衡" },
  { value: "high", label: "高", hint: "想得更久，适合难题" },
  { value: "off", label: "关闭思考", hint: "不输出思考过程（模型支持时）" },
];

export function effortLabel(v: string | undefined | null): string {
  return EFFORT_OPTIONS.find((o) => o.value === (v ?? ""))?.label ?? String(v);
}

/** /effort 的参数 → 档位；认不出返回 null。 */
export function resolveEffort(arg: string): string | null {
  const a = arg.trim().toLowerCase();
  const hit = EFFORT_OPTIONS.find((o) => o.value === a || o.label === arg.trim());
  if (hit) return hit.value;
  const alias: Record<string, string> = { default: "", auto: "", "": "", 关: "off", 关闭: "off", none: "off", max: "high", minimal: "low" };
  return a in alias ? alias[a] : arg.trim() in alias ? alias[arg.trim()] : null;
}

/** /style 的参数 → 风格 id（按 id 或显示名）；认不出返回 null。 */
export function resolveStyle(arg: string, styles: { id: string; label: string }[]): string | null {
  const a = arg.trim();
  if (!a) return null;
  const hit = styles.find((s) => s.id === a || s.label === a) ?? styles.find((s) => s.id.toLowerCase() === a.toLowerCase());
  return hit ? hit.id : null;
}

export function allCommands(prompts: PromptInfo[], skills: SkillInfo[] = []): SlashCommand[] {
  const builtinNames = new Set(BUILTIN.map((c) => c.name));
  const custom: SlashCommand[] = prompts
    .filter((p) => !builtinNames.has(p.name))
    .map((p) => ({
      name: p.name,
      description: p.description,
      argHint: p.argument_hint || undefined,
      takesArgs: true,
      kind: "prompt",
      source: p.source,
    }));
  // 技能：/名字 原样发给后端，后端把 SKILL.md 全文附在消息前（与内置命令、自定义提示同名时让给它们，可改用 $名字）
  const taken = new Set([...builtinNames, ...custom.map((c) => c.name)]);
  const sk: SlashCommand[] = skills
    .filter((x) => x.enabled && !taken.has(x.name))
    .map((x) => ({ name: x.name, description: x.description, argHint: "[任务]", takesArgs: true, kind: "skill", source: x.source }));
  return [...BUILTIN, ...custom, ...sk];
}

/** 是否是会发给后端的技能命令（/名字 不是内置命令或自定义提示，而是已启用的技能）。 */
export function isSkillCommand(name: string, prompts: PromptInfo[], skills: SkillInfo[]): boolean {
  return allCommands(prompts, skills).some((c) => c.kind === "skill" && c.name === name);
}

/** 解析 "/name 参数"；不是斜杠命令返回 null。 */
export function parseSlash(text: string): { name: string; args: string } | null {
  const m = /^\/([A-Za-z0-9_-]+)(?:\s+([\s\S]*))?$/.exec(text.trim());
  return m ? { name: m[1], args: (m[2] ?? "").trim() } : null;
}

export function resolveMode(arg: string): string | null {
  const a = arg.trim().toLowerCase();
  const hit = MODE_OPTIONS.find((o) => o.value === a || o.label === arg.trim());
  if (hit) return hit.value;
  if (a === "readonly" || a === "ro") return "read-only";
  return null;
}

const SOURCE_BADGE: Record<string, string> = { builtin: "内置", user: "我的", project: "项目" };

/** 输入框内容 → 菜单项；不该显示菜单时返回 null。 */
export function slashItems(
  text: string,
  prompts: PromptInfo[],
  models: { name: string; hint?: string }[],
  skills: SkillInfo[] = [],
  styles: { id: string; label: string; description?: string }[] = [],
): SlashItem[] | null {
  if (!text.startsWith("/") || text.includes("\n")) return null;
  const head = /^\/([\p{L}\p{N}_.-]*)$/u.exec(text); // 技能名可以是中文
  if (head) {
    const q = head[1].toLowerCase();
    const all = allCommands(prompts, skills);
    const pre = all.filter((c) => c.name.toLowerCase().startsWith(q));
    const mid = q.length > 1 ? all.filter((c) => !c.name.toLowerCase().startsWith(q) && c.name.toLowerCase().includes(q)) : [];
    const cmds = [...pre, ...mid];
    return cmds.map((c) => ({
      key: `${c.kind}:${c.name}`,
      label: `/${c.name}` + (c.argHint ? ` ${c.argHint}` : ""),
      hint: c.description,
      badge: c.kind === "prompt" ? SOURCE_BADGE[c.source ?? ""] ?? c.source : c.kind === "skill" ? "技能" : undefined,
      action: c.takesArgs ? { fill: `/${c.name} ` } : { run: `/${c.name}` },
    }));
  }
  const sub = /^\/(model|mode|effort|style)\s+(\S*)$/.exec(text);
  if (sub) {
    const q = sub[2].toLowerCase();
    if (sub[1] === "effort") {
      return EFFORT_OPTIONS.filter((o) => !q || o.value.includes(q) || o.label.includes(sub[2])).map((o) => ({
        key: `effort:${o.value || "default"}`,
        label: o.label,
        hint: o.hint,
        action: { run: `/effort ${o.value || "默认"}` },
      }));
    }
    if (sub[1] === "style") {
      return styles
        .filter((x) => !q || x.id.toLowerCase().includes(q) || x.label.includes(sub[2]))
        .map((x) => ({ key: `style:${x.id}`, label: x.label, hint: x.description, action: { run: `/style ${x.id}` } }));
    }
    if (sub[1] === "model") {
      return models
        .filter((m) => m.name.toLowerCase().includes(q))
        .map((m) => ({ key: `model:${m.name}`, label: m.name, hint: m.hint, action: { run: `/model ${m.name}` } }));
    }
    return MODE_OPTIONS.filter((o) => !q || o.value.includes(q) || o.label.includes(sub[2])).map((o) => ({
      key: `mode:${o.value}`,
      label: o.label,
      hint: o.hint,
      action: { run: `/mode ${o.value}` },
    }));
  }
  return null;
}
