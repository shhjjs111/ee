// 斜杠命令（D3-B）：内置命令 + 后端 prompts/list 的自定义提示。纯函数，便于测试。

export type PromptInfo = { name: string; description: string; argument_hint: string; source: string; path: string };

export type SlashCommand = {
  name: string;
  description: string;
  argHint?: string;
  takesArgs: boolean; // true：选中后补成 "/name " 等用户继续输入；false：选中即执行
  kind: "builtin" | "prompt";
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
  { name: "mode", description: "切换权限模式", argHint: "只读 | 默认 | 全自动", takesArgs: true, kind: "builtin" },
  { name: "memory", description: "记住一句话（不带文字则打开记忆设置）", argHint: "[要记住的话]", takesArgs: true, kind: "builtin" },
  { name: "mcp", description: "管理 MCP 服务器（D3-A 开发中）", takesArgs: false, kind: "builtin" },
];

export const MODE_OPTIONS = [
  { value: "read-only", label: "只读", hint: "只能读文件和运行只读命令" },
  { value: "default", label: "默认", hint: "工作区内改文件免问；其他命令先问你" },
  { value: "auto", label: "全自动", hint: "都不问（deny 规则和危险命令仍会拦）" },
];

export function allCommands(prompts: PromptInfo[]): SlashCommand[] {
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
  return [...BUILTIN, ...custom];
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
export function slashItems(text: string, prompts: PromptInfo[], models: { name: string; hint?: string }[]): SlashItem[] | null {
  if (!text.startsWith("/") || text.includes("\n")) return null;
  const head = /^\/([A-Za-z0-9_-]*)$/.exec(text);
  if (head) {
    const q = head[1].toLowerCase();
    const all = allCommands(prompts);
    const pre = all.filter((c) => c.name.toLowerCase().startsWith(q));
    const mid = q.length > 1 ? all.filter((c) => !c.name.toLowerCase().startsWith(q) && c.name.toLowerCase().includes(q)) : [];
    const cmds = [...pre, ...mid];
    return cmds.map((c) => ({
      key: `${c.kind}:${c.name}`,
      label: `/${c.name}` + (c.argHint ? ` ${c.argHint}` : ""),
      hint: c.description,
      badge: c.kind === "prompt" ? SOURCE_BADGE[c.source ?? ""] ?? c.source : undefined,
      action: c.takesArgs ? { fill: `/${c.name} ` } : { run: `/${c.name}` },
    }));
  }
  const sub = /^\/(model|mode)\s+(\S*)$/.exec(text);
  if (sub) {
    const q = sub[2].toLowerCase();
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
