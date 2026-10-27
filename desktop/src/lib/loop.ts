// 会话内 /loop（D5-3）的纯函数：子命令解析、循环说明文字、转为自动化时的预填值。
import { defaultAutomation, formatNextRun, type Automation, type Schedule } from "./schedule";

export type SessionLoop = {
  id: string;
  thread_id: string;
  prompt: string;
  schedule: Schedule;
  schedule_text: string;
  note?: string;
  created: number;
  next_run: number | null;
  last_run: number | null;
  runs: number;
  skipped: number;
  pending: boolean;
  last_error?: string;
};

export type LoopCommand = { kind: "list" } | { kind: "stop"; id: string } | { kind: "create"; text: string } | { kind: "help" };

/** /loop 后面的参数 → 子命令。 */
export function parseLoopArgs(args: string): LoopCommand {
  const a = args.trim();
  if (!a || /^(help|帮助|\?)$/i.test(a)) return { kind: "help" };
  if (/^(list|ls|列表|查看)$/i.test(a)) return { kind: "list" };
  const m = /^(stop|停止|cancel|rm)(?:\s+#?(\S+))?$/i.exec(a);
  if (m) return { kind: "stop", id: m[2] ?? "" };
  return { kind: "create", text: a };
}

export const LOOP_HELP = "用法：/loop 10m 检查CI是否通过、/loop 每天 9:00 总结昨天的提交；/loop list 查看，/loop stop <编号> 停止。关闭 ee 后循环不保留，要长期运行请转为自动化";

/** 一个循环的状态说明：“下次 今天 09:00 · 已发 3 次” / “等这一回合结束后发送”。 */
export function loopStatus(lp: SessionLoop, now: number = Date.now() / 1000): string {
  const parts: string[] = [];
  if (lp.pending) parts.push("到点了，等这一回合结束后发送");
  else if (lp.next_run) parts.push(`下次 ${formatNextRun(lp.next_run, now)}`);
  if (lp.runs) parts.push(`已发 ${lp.runs} 次`);
  if (lp.skipped) parts.push(`忙时跳过 ${lp.skipped} 次`);
  if (lp.last_error) parts.push(`上次发送失败：${lp.last_error}`);
  return parts.join(" · ");
}

/** /loop list 的一行文字汇总。 */
export function loopListText(loops: SessionLoop[]): string {
  if (!loops.length) return "这个会话没有循环。" + LOOP_HELP;
  return `本会话 ${loops.length} 个循环：` + loops.map((l) => `#${l.id} ${l.schedule_text} · ${l.prompt.slice(0, 30)}`).join("；");
}

/** “转为自动化”的预填：名字取提示词开头、同一项目（worktree 会话用主仓库）、同一调度；权限默认只读，用户在弹窗里再改。 */
export function loopToAutomation(lp: SessionLoop, cwd: string, model = ""): Omit<Automation, "id"> & { fromLoop: { id: string; threadId: string } } {
  const name = lp.prompt.replace(/\s+/g, " ").trim().slice(0, 24);
  return { ...defaultAutomation(cwd), name, prompt: lp.prompt, schedule: lp.schedule, model, fromLoop: { id: lp.id, threadId: lp.thread_id } };
}
