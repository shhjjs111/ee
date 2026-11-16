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

import type { DeliveryInfo } from "../types";

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

/** 交付结论的文案。比 reason 更具体：同样是 exhausted，"复验没通过"和"验收依据变了"
 *  是两件不同的事，用户要据此决定是接着修还是先确认验收标准。 */
export function deliveryText(d: { status?: string } | undefined | null): string {
  if (!d) return "";
  switch (d.status) {
    case "verified":
      return "交付已复验";
    case "unverified":
      return "未独立验收";
    case "failed":
      return "交付复验失败";
    case "needs_review":
      return "验收依据发生变化";
    case "failed_apply":
      return "补丁未能写回";
    case "interrupted":
      return "验证被中断";
    default:
      return "";
  }
}

/** 交付结论的原因（一句话）。
 *  `deliveryText` 只回答"是哪一类结果"，用户还要知道"为什么"：写回失败要去候选分支合并，
 *  验收依据变了要先确认判据，复验没过则要接着修。这里只读后端已有的字段，不新造数据，
 *  拿不到具体原因时宁可不写，也不编一个像原因的字串。 */
export function deliveryReason(d: DeliveryInfo | undefined | null): string {
  if (!d || d.status === "verified") return "";
  const out: string[] = [];
  const show = (xs?: string[] | null) => (xs || []).slice(0, 3).join("、");
  const diff = d.acceptance?.diff;
  if (d.acceptance?.changed) {
    // needs_review 不是一种原因：改测试、扫描不完整、命令/目录变了、写回期间被外部改动，
    // 处置完全不同。按后端给的那一份指纹差异逐项说，拿不到就退回 detail。
    if (diff?.tests_changed?.length) out.push(`既有验收测试被改写：${show(diff.tests_changed)}`);
    if (diff?.tests_removed?.length) out.push(`既有验收测试不见了：${show(diff.tests_removed)}`);
    if (diff?.files_incomplete || diff?.tests_incomplete) {
      out.push("验收依据无法完整扫描（文件过多或有不可读项），不能当作完整校验");
    }
    if (diff?.unverified_files?.length) out.push(`无法完整比对：${show(diff.unverified_files)}`);
    if (diff?.tests_unverified?.length) out.push(`验收测试无法完整比对：${show(diff.tests_unverified)}`);
    if (diff?.command) out.push("判据命令变了");
    if (diff?.cwd) out.push("判据执行目录变了");
    const plain = [...(diff?.changed || []), ...(diff?.added || []), ...(diff?.removed || [])];
    if (plain.length) out.push(`验收配置文件变化：${show(plain)}`);
    if (!out.length) out.push(d.acceptance.detail ? `验收依据发生变化：${d.acceptance.detail}` : "验收依据发生变化");
  }
  // unexpected 三态是三种事实：数组空 = 检查过且干净；非空 = 真的被改了；
  // null/缺失 = 没检查成（快照缺失），不能说成"验收依据变化"或干脆不说。
  const ap = d.apply;
  if (ap && "unexpected" in ap) {
    if (ap.unexpected === null) {
      out.push("写回期间无法检查补丁之外的改动（工作区快照缺失），按未验证处理");
    } else if (Array.isArray(ap.unexpected) && ap.unexpected.length) {
      out.push(`写回期间补丁之外的文件被改动：${show(ap.unexpected)}`);
    }
  }
  if (d.state?.changed_after_delivery) out.push("写回后工作区又被改动");
  if (d.state?.changed_during_verify) out.push("复验期间源码被改动");
  const expected = d.apply?.expected;
  if (expected && expected.ok === false) {
    out.push(`写回内容无法核对${expected.reason ? `：${expected.reason}` : ""}`);
  }
  if (d.apply?.mismatches?.length) {
    out.push(`${d.apply.mismatches.length} 处写回内容与补丁不一致`);
  }
  if (d.status === "failed_apply") out.push("候选补丁没能写回主目录，改动仍在那个候选自己的 worktree 分支上");
  if (d.status === "unverified") out.push("没有独立判据，只有证据审阅");
  if (d.status === "interrupted") out.push("验证被中断（判据没跑完）");
  if (!out.length) {
    // 没命中具体分支时，用判据自己的说明兜底；两者都不是模型自述，而是命令输出。
    if (d.regression?.met === false && d.regression?.reason) out.push(`回归命令未通过：${d.regression.reason}`);
    else if (d.target?.met === false && d.target?.reason) out.push(`目标判据未通过：${d.target.reason}`);
  }
  if (!out.length && d.status === "needs_review") {
    // 后端给了 needs_review 但界面契约里没有对应字段：如实说"不知道原因"，
    // 不能随便安一个理由，也不能让用户以为验收依据变了。
    out.push("这次交付无法确认，原因未在界面数据中给出（看回合内的提示或后端日志）");
  }
  return out.join("；");
}
