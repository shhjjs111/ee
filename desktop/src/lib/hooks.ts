// 设置页「Hooks」用的类型和纯函数（D5-4）。类型放这里，不动公共的 types.ts。
import { formatArgs, parseArgs } from "./mcp";

export const HOOK_EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "Notification"] as const;
export type HookEvent = (typeof HOOK_EVENTS)[number];

export const HOOK_EVENT_INFO: Record<HookEvent, { label: string; desc: string; matcher?: string }> = {
  SessionStart: { label: "会话开始", desc: "新对话第一次运行时。输出会作为补充上下文发给模型。", matcher: "来源：startup / resume / clear / compact" },
  UserPromptSubmit: { label: "提交消息", desc: "你发出消息、模型收到之前。退出码 2 = 拦下这条消息；输出会追加给模型。" },
  PreToolUse: { label: "工具执行前", desc: "退出码 2 = 不执行，并把 stderr 作为原因告诉模型；JSON 可改参数或免审批。", matcher: "工具名，如 shell|write、mcp__github__*" },
  PostToolUse: { label: "工具执行后", desc: "退出码 2 = 把 stderr 追加到工具结果里给模型看（如 lint / 格式化结果）。", matcher: "工具名，如 write|edit|apply_patch" },
  Stop: { label: "回合结束", desc: "模型准备结束时。退出码 2 = 把 stderr 当作新要求让模型继续（每回合最多 3 次）。" },
  Notification: { label: "需要审批", desc: "弹出审批时在后台运行（不等待、不能拦截），可用来发提醒。", matcher: "类型：permission_prompt" },
};

/** 设置里的一条用户级 hook。command：字符串 = 经 shell 运行；数组 = 直接运行程序。 */
export type HookEntry = { matcher: string; command: string | string[]; timeout: number; shell: string; enabled: boolean; name: string };
export type HooksSettings = { enabled: boolean; allowed_project: string[]; events: Partial<Record<HookEvent, HookEntry[]>> };

export type HookSummary = HookEntry & { event: HookEvent; source: "user" | "project"; file: string; label: string };
export type ProjectHooks = { file: string; allow_id: string; allowed: boolean; hooks: HookSummary[] };
export type HooksListResponse = { enabled: boolean; user: HookSummary[]; projects: ProjectHooks[]; errors: string[]; events: { name: HookEvent; label: string }[] };
export type HookTestResult = {
  label: string;
  code: number | null;
  stdout: string;
  stderr: string;
  timed_out: boolean;
  error: string;
  duration_ms: number;
  json: Record<string, unknown> | null;
  verdict: string;
  reason: string;
  context: string;
  permission: string | null;
  updated_input: Record<string, unknown> | null;
  notices: string[];
};

export const DEFAULT_HOOKS: HooksSettings = { enabled: true, allowed_project: [], events: {} };

export function newHook(): HookEntry {
  return { matcher: "", command: "", timeout: 60, shell: "", enabled: true, name: "" };
}

/** 命令显示成一行文字（数组按空格拼，含空格的参数加引号）。 */
export function commandText(cmd: string | string[]): string {
  return Array.isArray(cmd) ? formatArgs(cmd) : cmd;
}

/** 编辑框文字 → 配置：direct=true 时拆成数组（直接运行，不经 shell）。 */
export function commandFromText(text: string, direct: boolean): string | string[] {
  return direct ? parseArgs(text) : text;
}

/** 允许记录是“路径指纹:内容指纹”。允许新内容时去掉同一文件的旧记录；撤销时去掉这一条。 */
export function toggleAllow(list: string[], allowId: string, on: boolean): string[] {
  const path = allowId.split(":")[0];
  const rest = list.filter((x) => x !== allowId && (!on || x.split(":")[0] !== path));
  return on ? [...rest, allowId] : rest;
}

/** 保存前的检查，返回错误文字（没有问题返回 null）。 */
export function validateHook(h: HookEntry): string | null {
  const empty = Array.isArray(h.command) ? !h.command.length || !h.command[0].trim() : !h.command.trim();
  if (empty) return "命令不能为空";
  if (!(h.timeout >= 1 && h.timeout <= 600)) return "超时应在 1–600 秒之间";
  const m = h.matcher.trim();
  if (m && !/^[A-Za-z0-9_\-*?|. ]*$/.test(m)) {
    try {
      new RegExp(m);
    } catch {
      return "匹配规则不是合法的正则";
    }
  }
  return null;
}

export function hookCount(h: HooksSettings): number {
  return Object.values(h.events).reduce((n, xs) => n + (xs?.length ?? 0), 0);
}
