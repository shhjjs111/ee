/**
 * 停止原因的取值集合只有一个来源：`ee/core/goal.py` 的 MET / EXHAUSTED / BUDGET /
 * INTERRUPTED / ERROR，加上回合层的 completed / max_steps / loop / blocked。
 *
 * 前端原本有三张各自维护的映射表（TurnEnd 的三元链、ChildThreadCard 的 switch、
 * notify 的 REASON），各自漏了不同的 key，于是：
 * - `exhausted`（跑满轮次但目标没通过）在对话里显示「完成」、在子会话卡片显示「空闲」
 * - `budget`（撞到花费上限）、`blocked`（被 Hook 拦截）不弹桌面通知
 * - `met`（目标达成）落进子会话卡片的默认分支，显示「空闲」
 *
 * 用户会把失败当成功，离开电脑时尤其容易漏判。这里把取值集合与三张表放在一处，
 * 并用 Record<StopReason, …> 让 typecheck 在漏 key 时直接报错。
 */

export const STOP_REASONS = [
  "completed",
  "met",
  "exhausted",
  "max_steps",
  "budget",
  "blocked",
  "interrupted",
  "error",
  "loop",
  "done",
] as const;

export type StopReason = (typeof STOP_REASONS)[number];

/** 对话里一条回合结束时显示的文字（Items.tsx 的 TurnEnd）。 */
export const TURN_END_TEXT: Record<StopReason, string> = {
  completed: "完成",
  done: "完成",
  met: "完成",
  exhausted: "跑满轮次，目标未达成",
  max_steps: "达到步数上限",
  budget: "达到花费上限，已停止",
  blocked: "消息被 Hook 拦截",
  interrupted: "已停止",
  error: "出错",
  loop: "检测到重复操作，已停止",
};

/** 子会话 / 运行记录卡片上的状态词（ChildThreadCard 的 terminalText）。 */
export const CARD_TEXT: Record<StopReason, string> = {
  completed: "完成",
  done: "完成",
  met: "目标已达成",
  exhausted: "跑满轮次未达成",
  max_steps: "步数上限",
  budget: "超预算停止",
  blocked: "被拦截",
  interrupted: "已中断",
  error: "出错",
  loop: "循环停止",
};

/** 桌面通知的类型；null = 设计上不弹（用户主动停止不该打扰）。 */
export const NOTIFY_KIND: Record<StopReason, "done" | "error" | "loop" | null> = {
  completed: "done",
  done: "done",
  met: "done",
  exhausted: "error",
  max_steps: "done",
  budget: "error",
  blocked: "error",
  interrupted: null,
  error: "error",
  loop: "loop",
};

/** 桌面通知的标题词。 */
export const NOTIFY_HEAD: Record<StopReason, string> = {
  completed: "已完成",
  done: "已完成",
  met: "目标已达成",
  exhausted: "跑满轮次，目标未达成",
  max_steps: "已达到最大步数，已停下",
  budget: "达到花费上限，已停下",
  blocked: "被 Hook 拦截，已停下",
  interrupted: "已停止",
  error: "出错了",
  loop: "检测到重复操作，已停止",
};

/** 失败类结束方式：界面不得显示成「完成」或「空闲」。 */
export const FAILED_REASONS: ReadonlySet<string> = new Set(["exhausted", "budget", "blocked", "error"]);

export function isStopReason(x: string | null | undefined): x is StopReason {
  return !!x && (STOP_REASONS as readonly string[]).includes(x);
}

/** 未知 reason 用调用方给的默认词（旧后端可能发来没见过的值）。 */
export function turnEndText(reason: string | null | undefined): string {
  return isStopReason(reason) ? TURN_END_TEXT[reason] : "完成";
}

export function cardText(reason: string | null | undefined): string {
  return isStopReason(reason) ? CARD_TEXT[reason] : "空闲";
}
