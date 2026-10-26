// 系统提示词（SYSTEM.md / APPEND_SYSTEM.md）：后端 sysprompt/* RPC 的类型和显示用的小工具。

export type PromptKind = "system" | "append";
export type PromptScope = "project" | "user";

export interface PromptFileInfo {
  kind: PromptKind;
  scope: PromptScope;
  path: string;
  allow_id: string;
  trusted: boolean;
  active: boolean;
  error: string;
  chars: number;
}

export interface PromptSources {
  files: PromptFileInfo[];
  model_patch: boolean;
  system: string;
  append: string;
  pending: string[];
}

export interface SysPromptInfo {
  cwd: string;
  user_dir: string;
  current: PromptSources;
  frozen: PromptSources | null;
  max_bytes: number;
}

export interface SysPromptPreview {
  text: string;
  frozen: boolean;
  model: string;
  chars: number;
}

export const FILE_NAME: Record<PromptKind, string> = { system: "SYSTEM.md", append: "APPEND_SYSTEM.md" };

/** 一行说明某类文件现在用的是哪个来源。 */
export function sourceLabel(src: PromptSources, kind: PromptKind): string {
  const f = src.files.find((x) => x.kind === kind && x.active);
  if (!f) return kind === "system" ? "内置基础提示词" : "无";
  return `${f.scope === "project" ? "项目" : "用户"}：${f.path}`;
}

/** 状态文字：生效 / 未信任 / 内容有问题 / 被项目文件覆盖。 */
export function fileStatus(f: PromptFileInfo): { text: string; tone: "ok" | "warn" | "muted" } {
  if (f.error) return { text: f.error, tone: "warn" };
  if (f.active) return { text: "生效中", tone: "ok" };
  if (f.scope === "project" && !f.trusted) return { text: "未信任（不生效）", tone: "warn" };
  return { text: "未使用（被项目文件覆盖）", tone: "muted" };
}

/** 对话冻结的来源和新对话会用的是否不同（不同就提示“新对话生效”）。 */
export function differsFromFrozen(info: SysPromptInfo): boolean {
  const f = info.frozen;
  if (!f) return false;
  const c = info.current;
  return f.system !== c.system || f.append !== c.append || f.model_patch !== c.model_patch;
}
