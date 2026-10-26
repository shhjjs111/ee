// 设置页「Hooks」分区（D5-4）：总开关、我的 Hooks（增删改、测试运行）、项目 Hooks（允许 / 撤销、测试运行）、写法说明。
import clsx from "clsx";
import { FlaskConical, Plus, RefreshCw, ShieldCheck, Trash2, Webhook } from "lucide-react";
import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  commandFromText,
  commandText,
  DEFAULT_HOOKS,
  HOOK_EVENT_INFO,
  HOOK_EVENTS,
  newHook,
  toggleAllow,
  validateHook,
  type HookEntry,
  type HookEvent,
  type HooksListResponse,
  type HooksSettings as HooksCfg,
  type HookSummary,
  type HookTestResult,
} from "../lib/hooks";
import { parseArgs } from "../lib/mcp";
import { useApp } from "../lib/store";
import type { Settings, SettingsResponse } from "../types";
import { Button, Field, Input, Select, Spinner, Tip, Toggle } from "./ui";

type WithHooks = Settings & { hooks?: HooksCfg };
type TestState = { busy: boolean; result?: HookTestResult; err?: string };

const SHELL_OPTIONS = [
  { value: "", label: "跟随设置", hint: "用「常规」里选的 shell" },
  { value: "pwsh", label: "PowerShell 7" },
  { value: "powershell", label: "Windows PowerShell 5.1" },
  { value: "cmd", label: "cmd" },
  { value: "git-bash", label: "Git Bash" },
  { value: "bash", label: "bash" },
];

function Card({ title, desc, children, action }: { title: string; desc?: React.ReactNode; children: React.ReactNode; action?: React.ReactNode }) {
  return (
    <section className="mb-6 rounded-2xl border border-line bg-elev">
      <div className="flex items-start gap-3 border-b border-line px-5 py-3.5">
        <div className="min-w-0 flex-1">
          <h3 className="text-[14px] font-semibold text-fg">{title}</h3>
          {desc && <p className="mt-0.5 text-[12.5px] text-muted">{desc}</p>}
        </div>
        {action}
      </div>
      <div className="px-5 py-4">{children}</div>
    </section>
  );
}

function TestResultView({ t }: { t?: TestState }) {
  if (!t) return null;
  if (t.err) return <div className="mt-2 text-[12px] text-danger">{t.err}</div>;
  const r = t.result;
  if (!r) return null;
  const bad = r.error || r.timed_out || (r.code !== 0 && r.code !== 2);
  const pre = "mono mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all rounded-lg border border-line bg-bg px-2.5 py-1.5 text-[11.5px] text-fg";
  return (
    <div className="mt-2 rounded-lg border border-line bg-soft/40 px-3 py-2 text-[12px]">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className={clsx("font-medium", bad ? "text-danger" : r.verdict === "拦截" ? "text-warn" : "text-fg")}>结果：{r.verdict}</span>
        <span className="text-muted">退出码 {r.code ?? "—"}</span>
        <span className="text-faint">{r.duration_ms} ms</span>
      </div>
      {r.error && <div className="mt-1 text-danger">{r.error}</div>}
      {r.reason && <div className="mt-1 text-muted">原因（会回给模型）：{r.reason}</div>}
      {r.context && <div className="mt-1 text-muted">补充上下文：{r.context}</div>}
      {r.permission && <div className="mt-1 text-muted">审批：{r.permission === "allow" ? "免审批" : "强制审批"}</div>}
      {r.updated_input && <div className="mt-1 text-muted">改写参数：{JSON.stringify(r.updated_input)}</div>}
      {r.stdout.trim() && (
        <div className="mt-1.5">
          <span className="text-faint">stdout</span>
          <pre className={pre}>{r.stdout}</pre>
        </div>
      )}
      {r.stderr.trim() && (
        <div className="mt-1.5">
          <span className="text-faint">stderr</span>
          <pre className={pre}>{r.stderr}</pre>
        </div>
      )}
    </div>
  );
}

function HookEditor({ event, h, test, onChange, onDelete, onTest }: {
  event: HookEvent;
  h: HookEntry;
  test?: TestState;
  onChange: (h: HookEntry) => void;
  onDelete: () => void;
  onTest: () => void;
}) {
  const direct = Array.isArray(h.command);
  const info = HOOK_EVENT_INFO[event];
  // 直接运行模式下用本地文字编辑，避免输入引号 / 空格时被格式化打断
  const [text, setText] = useState(() => commandText(h.command));
  useEffect(() => {
    const cur = direct ? JSON.stringify(parseArgs(text)) : JSON.stringify(text);
    if (cur !== JSON.stringify(h.command)) setText(commandText(h.command));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [h.command]);
  const err = validateHook(h);
  return (
    <div className={clsx("rounded-xl border border-line p-3.5", !h.enabled && "opacity-70")}>
      <div className="mb-2.5 flex items-center gap-2">
        <Input className="h-7 max-w-[220px] text-[12.5px]" value={h.name} placeholder="名字（可选）" onChange={(e) => onChange({ ...h, name: e.target.value })} />
        <span className="flex-1" />
        <label className="flex items-center gap-1.5 text-[12px] text-muted">
          <Toggle on={h.enabled} onChange={(v) => onChange({ ...h, enabled: v })} /> 启用
        </label>
        <Button variant="outline" size="sm" className="h-7" disabled={!!err || test?.busy} onClick={onTest}>
          {test?.busy ? <Spinner size={12} /> : <FlaskConical size={12} />} 测试运行
        </Button>
        <Tip label="删除">
          <Button size="icon-sm" variant="danger" onClick={onDelete}>
            <Trash2 size={13} />
          </Button>
        </Tip>
      </div>
      <div className="grid grid-cols-6 gap-3">
        {info.matcher && (
          <Field label="匹配" hint={info.matcher} className="col-span-2">
            <Input className="mono" value={h.matcher} placeholder="留空 = 全部" onChange={(e) => onChange({ ...h, matcher: e.target.value })} />
          </Field>
        )}
        <Field
          label="命令"
          hint={direct ? "直接运行程序：空格分隔参数，含空格的参数用引号包住" : "经 shell 运行，可以用管道、重定向"}
          className={info.matcher ? "col-span-4" : "col-span-6"}
        >
          <Input
            className="mono"
            value={text}
            placeholder={direct ? 'python "D:\\hooks\\check.py"' : "python D:\\hooks\\check.py"}
            onChange={(e) => {
              setText(e.target.value);
              onChange({ ...h, command: commandFromText(e.target.value, direct) });
            }}
          />
        </Field>
        <Field label="运行方式" className="col-span-2">
          <Select
            value={direct ? "direct" : "shell"}
            options={[
              { value: "shell", label: "经 shell 运行" },
              { value: "direct", label: "直接运行程序", hint: "不经 shell，参数原样传递" },
            ]}
            onChange={(v) => onChange({ ...h, command: commandFromText(text, v === "direct"), shell: v === "direct" ? "" : h.shell })}
            className="h-8 w-full max-w-none justify-between border border-line bg-bg px-2.5 text-fg"
          />
        </Field>
        {!direct && (
          <Field label="shell" className="col-span-2">
            <Select value={h.shell} options={SHELL_OPTIONS} onChange={(v) => onChange({ ...h, shell: v })} className="h-8 w-full max-w-none justify-between border border-line bg-bg px-2.5 text-fg" />
          </Field>
        )}
        <Field label="超时（秒）" className="col-span-2">
          <Input inputMode="numeric" value={h.timeout} onChange={(e) => onChange({ ...h, timeout: Number(e.target.value) || 0 })} />
        </Field>
      </div>
      {err && (Array.isArray(h.command) ? h.command.length > 0 : h.command !== "") && <div className="mt-2 text-[12px] text-danger">{err}</div>}
      <TestResultView t={test} />
    </div>
  );
}

function ProjectHookRow({ h, canTest, test, onTest }: { h: HookSummary; canTest: boolean; test?: TestState; onTest: () => void }) {
  return (
    <div className="border-t border-line py-2 first:border-t-0">
      <div className="flex items-center gap-2 text-[12.5px]">
        <span className="rounded bg-soft px-1.5 py-px text-[11px] text-muted">{HOOK_EVENT_INFO[h.event]?.label ?? h.event}</span>
        {h.matcher && <span className="mono text-[11.5px] text-muted">{h.matcher}</span>}
        <span className="mono min-w-0 flex-1 truncate text-fg">{commandText(h.command)}</span>
        {!h.enabled && <span className="text-[11.5px] text-faint">已停用</span>}
        {canTest && (
          <Button variant="ghost" size="sm" className="h-6" disabled={test?.busy} onClick={onTest}>
            {test?.busy ? <Spinner size={11} /> : <FlaskConical size={11} />} 测试
          </Button>
        )}
      </div>
      <TestResultView t={test} />
    </div>
  );
}

export function HooksSettings({ draft, resp, up }: { draft: Settings; resp: SettingsResponse; up: (p: Partial<Settings>) => void }) {
  const s = useApp();
  const hooks: HooksCfg = { ...DEFAULT_HOOKS, ...((draft as WithHooks).hooks ?? {}) };
  const setHooks = (p: Partial<HooksCfg>) => up({ hooks: { ...hooks, ...p } } as unknown as Partial<Settings>);
  const setEvent = (ev: HookEvent, rows: HookEntry[]) => setHooks({ events: { ...hooks.events, [ev]: rows } });
  const cwd = (s.activeThreadId ? s.details[s.activeThreadId]?.cwd : null) ?? s.activeWorkspace ?? null;

  const [list, setList] = useState<HooksListResponse | null>(null);
  const [listErr, setListErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [tests, setTests] = useState<Record<string, TestState>>({});
  const [addEvent, setAddEvent] = useState<HookEvent>("PreToolUse");

  const reload = useCallback(() => {
    if (!s.rpc) return;
    setLoading(true);
    setListErr(null);
    s.rpc
      .call("hooks/list", { cwd })
      .then((r: HooksListResponse) => setList(r))
      .catch((e: Error) => setListErr(e.message))
      .finally(() => setLoading(false));
  }, [s.rpc, cwd]);
  useEffect(reload, [reload, resp]);

  // 每次测试一个令牌：测试期间改了 / 删了这个 Hook，旧结果回来时丢掉（不显示在新内容下面）
  const testGen = useRef(0);
  const testTok = useRef<Record<string, number>>({});
  const runTest = async (key: string, event: HookEvent, hook: HookEntry | HookSummary, allowId?: string) => {
    const tok = ++testGen.current;
    testTok.current[key] = tok;
    setTests((t) => ({ ...t, [key]: { busy: true } }));
    try {
      const { matcher, command, timeout, shell } = hook;
      const r: HookTestResult = await s.rpc!.call("hooks/test", { event, hook: { matcher, command, timeout, shell }, cwd, allow_id: allowId ?? null }, 660_000);
      if (testTok.current[key] !== tok) return;
      setTests((t) => ({ ...t, [key]: { busy: false, result: r } }));
    } catch (e) {
      if (testTok.current[key] !== tok) return;
      setTests((t) => ({ ...t, [key]: { busy: false, err: (e as Error).message } }));
    }
  };
  const clearTest = (key: string) => {
    delete testTok.current[key];
    setTests((t) => {
      const r = { ...t };
      delete r[key];
      return r;
    });
  };

  const allowed = new Set(hooks.allowed_project);
  const total = HOOK_EVENTS.reduce((n, ev) => n + (hooks.events[ev]?.length ?? 0), 0);

  return (
    <>
      <h2 className="mb-1 text-[20px] font-semibold tracking-tight text-fg">Hooks</h2>
      <p className="mb-5 text-[13px] text-muted">
        在会话的关键时刻自动运行你的命令：拦下危险操作、改完文件自动格式化 / 检查、给模型补充上下文、回合结束前做验收。改动从下一条消息开始生效。
      </p>

      <Card title="总开关">
        <div className="flex items-center gap-6">
          <div className="min-w-0 flex-1">
            <div className="text-[13.5px] text-fg">启用 Hooks</div>
            <div className="mt-0.5 text-[12px] text-faint">关闭后所有 Hook（包括已允许的项目 Hook）都不运行</div>
          </div>
          <Toggle on={hooks.enabled} onChange={(v) => setHooks({ enabled: v })} />
        </div>
      </Card>

      <Card title="我的 Hooks" desc="保存在用户配置里（~/.ee/config.toml 的 [[hooks.事件]]），所有项目都生效。测试运行用的是当前填写的内容，不必先保存。">
        <div className="space-y-5">
          {total === 0 && <div className="text-[12.5px] text-faint">还没有 Hook。在下面选一个事件添加。</div>}
          {HOOK_EVENTS.filter((ev) => (hooks.events[ev]?.length ?? 0) > 0).map((ev) => (
            <div key={ev}>
              <div className="mb-1.5 flex items-baseline gap-2">
                <span className="text-[13px] font-semibold text-fg">{HOOK_EVENT_INFO[ev].label}</span>
                <span className="mono text-[11.5px] text-faint">{ev}</span>
              </div>
              <p className="mb-2 text-[12px] text-muted">{HOOK_EVENT_INFO[ev].desc}</p>
              <div className="space-y-2.5">
                {(hooks.events[ev] ?? []).map((h, i) => {
                  const key = `user:${ev}:${i}`;
                  return (
                    <HookEditor
                      key={key}
                      event={ev}
                      h={h}
                      test={tests[key]}
                      onTest={() => runTest(key, ev, h)}
                      onChange={(nh) => {
                        clearTest(key);
                        setEvent(ev, (hooks.events[ev] ?? []).map((x, j) => (j === i ? nh : x)));
                      }}
                      onDelete={() => {
                        testTok.current = {};
                        setTests({});
                        setEvent(ev, (hooks.events[ev] ?? []).filter((_, j) => j !== i));
                      }}
                    />
                  );
                })}
              </div>
            </div>
          ))}
          <div className="flex items-center gap-2">
            <Select
              value={addEvent}
              options={HOOK_EVENTS.map((ev) => ({ value: ev, label: `${HOOK_EVENT_INFO[ev].label}（${ev}）`, hint: HOOK_EVENT_INFO[ev].desc }))}
              onChange={(v) => setAddEvent(v as HookEvent)}
              className="h-8 border border-line bg-bg px-2.5 text-fg"
            />
            <Button variant="outline" size="sm" className="h-8" onClick={() => setEvent(addEvent, [...(hooks.events[addEvent] ?? []), newHook()])}>
              <Plus size={13} /> 添加
            </Button>
          </div>
        </div>
      </Card>

      <Card
        title="项目 Hooks"
        desc={cwd ? "来自项目的 .ee/hooks.toml 和 .ee/config.toml。项目文件里的命令会在本机运行，默认不执行，确认可信后再允许；内容改了需要重新允许。" : "先在左栏打开一个项目"}
        action={
          <Tip label="刷新">
            <Button size="icon-sm" variant="ghost" onClick={reload} disabled={loading}>
              {loading ? <Spinner size={12} /> : <RefreshCw size={13} />}
            </Button>
          </Tip>
        }
      >
        {!list?.projects.length ? (
          <div className="text-[12.5px] text-faint">当前项目里没有 Hook。</div>
        ) : (
          <div className="space-y-3">
            {list.projects.map((p) => {
              const ok = allowed.has(p.allow_id);
              const saved = p.allowed;
              return (
                <div key={p.file} className="rounded-xl border border-line p-3.5">
                  <div className="flex items-center gap-2">
                    <Webhook size={14} className="text-muted" />
                    <span className="mono min-w-0 flex-1 truncate text-[12.5px] text-fg">{p.file}</span>
                    <span className={clsx("text-[11.5px]", ok ? "text-add" : "text-warn")}>
                      {ok ? (saved ? "已允许" : "已允许（保存后生效）") : saved ? "将撤销（保存后生效）" : "未允许，不会运行"}
                    </span>
                    <Button
                      variant={ok ? "outline" : "primary"}
                      size="sm"
                      className="h-7"
                      onClick={() => setHooks({ allowed_project: toggleAllow(hooks.allowed_project, p.allow_id, !ok) })}
                    >
                      {ok ? "撤销允许" : (
                        <>
                          <ShieldCheck size={12} /> 允许
                        </>
                      )}
                    </Button>
                  </div>
                  <div className="mt-2">
                    {p.hooks.map((h, i) => {
                      const key = `project:${p.allow_id}:${i}`;
                      return <ProjectHookRow key={key} h={h} canTest={saved && ok} test={tests[key]} onTest={() => runTest(key, h.event, h, p.allow_id)} />;
                    })}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </Card>

      {(listErr || (list?.errors.length ?? 0) > 0) && (
        <Card title="配置问题">
          <ul className="space-y-1 text-[12.5px] text-danger">
            {listErr && <li>读取 Hooks 失败：{listErr}</li>}
            {list?.errors.map((e, i) => <li key={i}>{e}</li>)}
          </ul>
        </Card>
      )}

      <Card title="写法说明">
        <div className="space-y-2 text-[12.5px] leading-relaxed text-muted">
          <p>
            <b className="text-fg">输入</b>：stdin 收到一个 JSON，含 <code className="mono">session_id</code>、<code className="mono">cwd</code>、
            <code className="mono">hook_event_name</code>，以及事件字段（<code className="mono">prompt</code>、<code className="mono">tool_name</code>、
            <code className="mono">tool_input</code>、<code className="mono">tool_response</code>、<code className="mono">stop_hook_active</code> 等）。环境变量
            <code className="mono">EE_PROJECT_DIR</code>、<code className="mono">EE_SESSION_ID</code>、<code className="mono">EE_HOOK_EVENT</code>。
          </p>
          <p>
            <b className="text-fg">退出码</b>：0 = 继续；2 = 拦截（stderr 作为原因回给模型）；其他 = 出错，只在对话里出一条提示，不影响继续。超时会结束整个进程树。
          </p>
          <p>
            <b className="text-fg">JSON 输出</b>（退出码 0 时 stdout 可选）：<code className="mono">{'{"decision": "block", "reason": "…"}'}</code> 拦截；
            <code className="mono">{'{"additionalContext": "…"}'}</code> 追加给模型；工具执行前还可以用
            <code className="mono">{'{"hookSpecificOutput": {"permissionDecision": "allow" | "deny" | "ask", "updatedInput": {…}}}'}</code> 免审批 / 拒绝 / 强制审批 / 改参数。
          </p>
        </div>
      </Card>
    </>
  );
}
