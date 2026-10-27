// 设置页「回答与花费」（D5-9）：推理强度默认值与按模型记住的档位、回答风格、按用途分模型、花费上限。
import { reportError } from "../lib/errors";
import { Trash2 } from "lucide-react";
import React, { useEffect, useState } from "react";
import { EFFORT_OPTIONS, effortLabel } from "../lib/slash";
import { useApp } from "../lib/store";
import type { BudgetSettings, BudgetStatus, Settings, SettingsResponse } from "../types";
import { Button, Input, Select, Toggle } from "./ui";

export const ROLE_LABEL: Record<string, [string, string]> = {
  title: ["对话标题", "打开“用模型生成标题”后，第一回合结束时起标题"],
  compact: ["上下文压缩", "写交接摘要；和主模型相同时能复用缓存，换模型则按它的窗口决定带全量历史还是精简记录"],
  explore: ["只读子代理（explore）", "调研、查代码的只读子代理；调用时指定了模型则以调用为准"],
  plan: ["计划模式", "计划模式下主对话改用这个模型（换回其它模式自动用回主模型）"],
  review: ["代码审查", "/review 的审查回合"],
  commit: ["提交信息", "/commit 生成提交信息"],
};
const ROLES = ["title", "compact", "explore", "plan", "review", "commit"];
const SCOPES: [keyof BudgetSettings & ("session" | "daily" | "monthly"), string, string][] = [
  ["session", "每个会话", "单个对话（含它的子代理）累计"],
  ["daily", "每天", "所有对话、ee exec 和自动化合计，按本机日期"],
  ["monthly", "每月", "同上，按自然月"],
];
const DEFAULT_BUDGET: BudgetSettings = { session: 0, daily: 0, monthly: 0, action: "warn", warn_ratio: 0.8, currency: "¥" };

function Card({ title, desc, children }: { title: string; desc?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="mb-6 rounded-2xl border border-line bg-elev">
      <div className="border-b border-line px-5 py-3.5">
        <h3 className="text-[14px] font-semibold text-fg">{title}</h3>
        {desc && <p className="mt-0.5 text-[12.5px] text-muted">{desc}</p>}
      </div>
      <div className="px-5 py-4">{children}</div>
    </section>
  );
}

function Row({ label, desc, children }: { label: string; desc?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-6 border-b border-line py-3 first:pt-0 last:border-b-0 last:pb-0">
      <div className="min-w-0 flex-1">
        <div className="text-[13.5px] text-fg">{label}</div>
        {desc && <div className="mt-0.5 text-[12px] text-faint">{desc}</div>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

export function money(cur: string, v: number): string {
  return `${cur}${v >= 1 ? v.toFixed(2) : v.toFixed(4)}`;
}

export function AnswerSettings({ draft, resp, up }: { draft: Settings; resp: SettingsResponse; up: (p: Partial<Settings>) => void }) {
  const s = useApp();
  const [remembered, setRemembered] = useState<Record<string, string>>({});
  const [status, setStatus] = useState<BudgetStatus | null>(null);
  useEffect(() => {
    if (!s.rpc) return;
    s.rpc.call("reasoning/prefs").then((r) => setRemembered(r.remembered ?? {})).catch(reportError("读取思考强度记忆失败"));
    s.rpc.call("budget/status", { thread_id: s.activeThreadId ?? undefined }).then(setStatus).catch(reportError("读取预算状态失败"));
  }, [s.rpc, resp]);

  const effort = draft.reasoning?.effort ?? "";
  const roles = draft.roles ?? {};
  const budget = { ...DEFAULT_BUDGET, ...(draft.budget ?? {}) };
  const styles = resp.choices.styles ?? [{ id: "default", label: "默认", description: "", source: "builtin", path: "" }];
  const modelNames = Object.keys(draft.models);
  const setBudget = (p: Partial<BudgetSettings>) => up({ budget: { ...budget, ...p } });

  return (
    <>
      <h2 className="mb-5 text-[20px] font-semibold tracking-tight text-fg">回答与花费</h2>

      <Card title="推理强度" desc="新对话的默认档位。输入框旁的“推理”或 /effort 可以只改当前会话，并会按模型记住。">
        <Row label="全局默认" desc="“默认”= 不指定，用模型自己的默认；模型不支持的档位不会发送（输入框里置灰）">
          <Select
            value={effort}
            options={EFFORT_OPTIONS}
            onChange={(v) => up({ reasoning: { effort: v } })}
            side="bottom"
            align="end"
            className="border border-line"
          />
        </Row>
        <Row
          label="按模型记住的档位"
          desc={Object.keys(remembered).length ? "这些模型的新会话会用记住的档位（优先于全局默认）" : "还没有。在对话里切换过推理强度的模型会出现在这里"}
        >
          <div className="flex max-w-[360px] flex-wrap justify-end gap-1.5">
            {Object.entries(remembered).map(([m, v]) => (
              <span key={m} className="inline-flex items-center gap-1 rounded-md bg-soft px-2 py-0.5 text-[12px] text-muted">
                {m} · {effortLabel(v)}
                <button
                  title="忘掉，跟随全局默认"
                  className="text-faint hover:text-danger"
                  onClick={() =>
                    s.rpc
                      ?.call("reasoning/forget", { model: m })
                      .then(() => setRemembered((cur) => Object.fromEntries(Object.entries(cur).filter(([k]) => k !== m))))
                      .catch(reportError("忘记该模型的思考强度失败"))
                  }
                >
                  <Trash2 size={11} />
                </button>
              </span>
            ))}
          </div>
        </Row>
      </Card>

      <Card
        title="回答风格"
        desc="附加在系统提示末尾，整个会话固定不变（不影响缓存）；会话中途用输入框旁的“风格”或 /style 切换时只追加一条说明。自定义风格放在 ~/.ee/styles/名字.md（首行 # 显示名）。"
      >
        <Row label="新对话的默认风格" desc={styles.find((x) => x.id === (draft.style?.default ?? "default"))?.description || "常规回答"}>
          <Select
            value={draft.style?.default ?? "default"}
            options={styles.map((x) => ({ value: x.id, label: x.label, hint: x.description || undefined }))}
            onChange={(v) => up({ style: { default: v } })}
            side="bottom"
            align="end"
            className="border border-line"
          />
        </Row>
      </Card>

      <Card title="按用途分模型" desc="留空 = 跟随当前对话的主模型。花费按实际使用的模型单价统计。">
        {ROLES.map((r) => (
          <Row key={r} label={ROLE_LABEL[r][0]} desc={ROLE_LABEL[r][1]}>
            <Select
              value={roles[r] ?? ""}
              options={[{ value: "", label: "跟随主模型" }, ...modelNames.map((m) => ({ value: m, label: m, hint: draft.models[m]?.model }))]}
              onChange={(v) => up({ roles: { ...roles, [r]: v } })}
              side="bottom"
              align="end"
              className="border border-line"
            />
          </Row>
        ))}
        <Row label="用模型生成对话标题" desc="关闭时取第一条消息的开头作标题（不花钱）">
          <Toggle on={!!draft.title?.ai} onChange={(v) => up({ title: { ai: v } })} />
        </Row>
      </Card>

      <Card
        title="花费上限"
        desc={`0 = 不限。只统计单价币种为 ${budget.currency || "¥"} 的模型；ee exec 和自动化到上限一律停止，并在运行记录里写明原因。`}
      >
        {SCOPES.map(([k, label, desc]) => (
          <Row
            key={k}
            label={label}
            desc={
              <>
                {desc}
                {status && status.currency === (budget.currency || "¥") && (k !== "session" || s.activeThreadId) && (
                  <span className="ml-1 text-muted">
                    · {k === "session" ? "当前对话" : k === "daily" ? "今天" : "本月"}已花 {money(status.currency, status.scopes[k].spent)}
                  </span>
                )}
              </>
            }
          >
            <div className="flex items-center gap-1.5">
              <span className="text-[12px] text-faint">{budget.currency || "¥"}</span>
              <Input
                className="w-24 text-right"
                inputMode="decimal"
                value={String(budget[k] ?? 0)}
                onChange={(e) => setBudget({ [k]: e.target.value } as Partial<BudgetSettings>)}
              />
            </div>
          </Row>
        ))}
        <Row label="达到上限时" desc="达到提醒比例时先提示一次">
          <Select
            value={budget.action}
            options={[
              { value: "warn", label: "仅提醒", hint: "提示一次后继续运行" },
              { value: "pause", label: "暂停并询问", hint: "弹出卡片：继续这一回合 / 本会话不再询问 / 停止" },
            ]}
            onChange={(v) => setBudget({ action: v })}
            side="bottom"
            align="end"
            className="border border-line"
          />
        </Row>
        <Row label="提醒比例" desc="用到上限的这个比例时提示">
          <div className="flex items-center gap-1.5">
            <Input
              className="w-16 text-right"
              inputMode="numeric"
              value={String(Math.round(Number(budget.warn_ratio || 0.8) * 100))}
              onChange={(e) => setBudget({ warn_ratio: Number(e.target.value || 80) / 100 })}
            />
            <span className="text-[12px] text-faint">%</span>
          </div>
        </Row>
        <Row label="币种" desc="与模型单价里的币种一致（DeepSeek 内置单价是 ¥）">
          <Input className="w-14 text-center" value={budget.currency} onChange={(e) => setBudget({ currency: e.target.value })} />
        </Row>
        {status && (
          <div className="mt-3 flex justify-end">
            <Button
              size="sm"
              variant="ghost"
              onClick={() =>
                s.rpc
                  ?.call("budget/status", { thread_id: s.activeThreadId ?? undefined })
                  .then(setStatus)
                  .catch((e: Error) => s.showToast(`刷新已花费失败：${e.message}`, "error"))
              }
            >
              刷新已花费
            </Button>
          </div>
        )}
      </Card>
    </>
  );
}
