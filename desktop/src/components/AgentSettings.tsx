// 设置页「工具与记忆」：网页搜索后端与 key、子代理默认最大步数、记忆开关与编辑、技能列表（D5-5）。
import clsx from "clsx";
import { FolderOpen, RefreshCw } from "lucide-react";
import React, { useCallback, useEffect, useState } from "react";
import type { SkillInfo } from "../lib/slash";
import { useApp } from "../lib/store";
import type { MemoryInfo, Settings, SettingsResponse, SkillsSettings, WebKeySettings, WebSettings } from "../types";
import { keySourceText } from "../types";
import { Button, Input, Select, Spinner, Toggle } from "./ui";

const SEARCH_LABEL: Record<string, [string, string]> = {
  exa: ["Exa（免费，默认）", "免费、无需 key（Exa 公共 MCP 端点）"],
  duckduckgo: ["DuckDuckGo（免费）", "免费、无需 key，抓取 DuckDuckGo 网页版结果；请求多了可能被限流"],
  deepseek: ["DeepSeek（付费）", "复用 DeepSeek 的 API key；北京时间 2026-11-01 起 $0.75 / 千次"],
  tavily: ["Tavily", "需要 Tavily API key"],
  brave: ["Brave Search", "需要 Brave Search API key"],
  off: ["关闭", "不提供 web_search 工具"],
};
const FETCH_LABEL: Record<string, [string, string]> = {
  direct: ["直接抓取（免费，默认）", "本机直接下载网页并转成文本，免费"],
  auto: ["自动", "搜索后端是 DeepSeek 且有 key 时用 DeepSeek Fetch（付费），否则直接抓取"],
  deepseek: ["DeepSeek Fetch（付费）", "北京时间 2026-11-01 起 $0.75 / 千次，失败自动改直连"],
  off: ["关闭", "不提供 web_fetch 工具"],
};

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

function Row({ label, desc, children }: { label: string; desc?: string; children: React.ReactNode }) {
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


const DEFAULT_WEB: WebSettings = {
  search_backend: "exa",
  fetch_backend: "direct",
  search_fallback: ["duckduckgo"],
  timeout: 20,
  max_results: 8,
  keys: { tavily: {}, brave: {} },
};

function KeyInput({ k, onChange }: { k: WebKeySettings; onChange: (k: WebKeySettings) => void }) {
  const status = keySourceText(k.api_key_source ?? (k.api_key_set ? "store" : k.env_present ? "env" : ""), k.api_key_preview, k.api_key_env);
  return (
    <div className="flex items-center gap-2">
      <span className="text-[12px] text-faint">{status}</span>
      <Input
        type="password"
        className="w-56"
        placeholder={k.api_key_set ? "留空保持不变" : "粘贴 API key"}
        value={k.api_key ?? ""}
        onChange={(e) => onChange({ ...k, api_key: e.target.value || null })}
      />
      {k.api_key_set && (
        <Button variant="outline" size="sm" onClick={() => onChange({ ...k, api_key: "" })}>
          清除
        </Button>
      )}
    </div>
  );
}

type SkillsList = {
  enabled: boolean;
  compat: boolean;
  skills: SkillInfo[];
  errors: { path: string; error: string }[];
  dirs: { user: string; project: string | null };
};
const SOURCE_LABEL: Record<string, string> = { user: "我的", project: "项目" };
const DEFAULT_SKILLS: SkillsSettings = { enabled: true, compat: true, disabled: [], max_chars: 8000 };

/** 技能：列出 ~/.ee/skills、<项目>/.ee/skills（及兼容的 .claude/.agents）里的技能，逐个启用/禁用（写进 config，新对话生效）。 */
function SkillsCard({ draft, up, cwd }: { draft: Settings; up: (p: Partial<Settings>) => void; cwd: string | null }) {
  const s = useApp();
  const sk = { ...DEFAULT_SKILLS, ...(draft.skills ?? {}) };
  const [list, setList] = useState<SkillsList | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    if (!s.rpc) return;
    setBusy(true);
    s.rpc
      .call("skills/list", { cwd })
      .then((r: SkillsList) => {
        setList(r);
        setErr(null);
      })
      .catch((e: Error) => setErr(`读取技能失败：${e.message}`))
      .finally(() => setBusy(false));
  }, [s.rpc, cwd]);
  useEffect(load, [load]);

  const openDir = async (scope: "user" | "project") => {
    try {
      const r: { path: string } = await s.rpc!.call("skills/dir", { scope, cwd });
      if (window.ee?.openPath) await window.ee.openPath(r.path);
      else s.showToast(r.path);
    } catch (e) {
      s.showToast((e as Error).message, "error");
    }
  };
  const setSk = (p: Partial<SkillsSettings>) => up({ skills: { ...sk, ...p } });
  const toggle = (name: string, on: boolean) =>
    setSk({ disabled: on ? sk.disabled.filter((n) => n !== name) : [...new Set([...sk.disabled, name])] });

  return (
    <Card
      title="技能"
      desc={
        <>
          技能是一个文件夹 + SKILL.md（开头写 name 和 description），可以带脚本和参考资料。新对话开始时模型只看到名字和说明，用到时才读全文；也可以在输入框用 <span className="mono">/名字</span> 或 <span className="mono">$名字</span> 点名使用。
        </>
      }
      action={
        <Button variant="outline" size="sm" disabled={busy} onClick={load}>
          <RefreshCw size={13} className={clsx(busy && "animate-spin")} /> 刷新
        </Button>
      }
    >
      <Row label="启用技能" desc="关闭后新对话不读入技能清单，也不提供 skill 工具">
        <Toggle on={sk.enabled} onChange={(v) => setSk({ enabled: v })} />
      </Row>
      <Row label="兼容 .claude / .agents 目录" desc="同时读取 ~/.claude/skills、~/.agents/skills 和项目里的 .claude/skills、.agents/skills（保存后点刷新）">
        <Toggle on={sk.compat} onChange={(v) => setSk({ compat: v })} />
      </Row>
      <Row label="技能目录" desc={list ? `我的：${list.dirs.user}` + (list.dirs.project ? `；项目：${list.dirs.project}` : "") : undefined}>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => openDir("user")}>
            <FolderOpen size={13} /> 我的技能目录
          </Button>
          {cwd && (
            <Button variant="outline" size="sm" onClick={() => openDir("project")}>
              <FolderOpen size={13} /> 项目技能目录
            </Button>
          )}
        </div>
      </Row>
      {!list ? (
        <div className="py-3">{err ? <span className="text-[12.5px] text-danger">{err}</span> : <Spinner />}</div>
      ) : (
        <div className="pt-3">
          {list.skills.length === 0 ? (
            <div className="text-[12.5px] text-faint">还没有技能。在技能目录里新建「名字/SKILL.md」即可，ee 也会读取 Claude Code、Codex 等放在 .claude/skills、.agents/skills 里的技能。</div>
          ) : (
            <ul className="divide-y divide-line rounded-xl border border-line">
              {list.skills.map((x) => {
                const on = !sk.disabled.includes(x.name);
                return (
                  <li key={x.name} className="flex items-start gap-3 px-3 py-2.5">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className={clsx("mono text-[13px]", on ? "text-fg" : "text-faint line-through")}>{x.name}</span>
                        <span className="shrink-0 rounded border border-line px-1 text-[10.5px] text-faint">
                          {SOURCE_LABEL[x.source] ?? x.source} · .{x.origin}
                        </span>
                        {(x.shadows?.length ?? 0) > 0 && <span className="shrink-0 text-[11px] text-faint">覆盖了 {x.shadows!.length} 个同名技能</span>}
                      </div>
                      <div className="mt-0.5 line-clamp-2 text-[12px] text-muted">{x.description}</div>
                      <button className="mono mt-0.5 block max-w-full truncate text-left text-[11.5px] text-faint hover:text-fg" onClick={() => window.ee?.openPath(x.path)} title="打开 SKILL.md">
                        {x.path}
                      </button>
                    </div>
                    <div className={clsx("shrink-0 pt-0.5", !sk.enabled && "pointer-events-none opacity-50")}>
                      <Toggle on={on} onChange={(v) => toggle(x.name, v)} />
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
          {list.errors.length > 0 && (
            <div className="mt-3 space-y-1">
              <div className="text-[12.5px] text-danger">以下技能没有加载：</div>
              {list.errors.map((e) => (
                <div key={e.path} className="text-[12px] text-muted">
                  <span className="mono text-faint">{e.path}</span>：{e.error}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

export function AgentSettings({ draft, resp, up }: { draft: Settings; resp: SettingsResponse; up: (p: Partial<Settings>) => void }) {
  const s = useApp();
  const web = { ...DEFAULT_WEB, ...(draft.web ?? {}) };
  const sub = { max_parallel: 3, worktree: true, ...(draft.subagent ?? { enabled: true, max_steps: 30 }) };
  const mem = draft.memory ?? { enabled: true, workspace: true, max_bytes: 8192 };
  const setWeb = (p: Partial<WebSettings>) => up({ web: { ...web, ...p } });
  const backends = resp.choices.search_backends ?? ["exa", "duckduckgo", "deepseek", "tavily", "brave", "off"];
  const fetches = resp.choices.fetch_backends ?? ["direct", "auto", "deepseek", "off"];
  const cwd = (s.activeThreadId ? s.details[s.activeThreadId]?.cwd : null) ?? s.activeWorkspace ?? null;

  const [memInfo, setMemInfo] = useState<MemoryInfo | null>(null);
  const [memText, setMemText] = useState<{ user: string; workspace: string }>({ user: "", workspace: "" });
  const [memBusy, setMemBusy] = useState(false);
  const [memMsg, setMemMsg] = useState<string | null>(null);

  useEffect(() => {
    s.rpc
      ?.call("memory/get", { cwd })
      .then((r: MemoryInfo) => {
        setMemInfo(r);
        setMemText({ user: r.user.text, workspace: r.workspace?.text ?? "" });
      })
      .catch((e: Error) => setMemMsg(`读取记忆失败：${e.message}`));
  }, [s.rpc, cwd]);

  const saveMem = async (scope: "user" | "workspace") => {
    setMemBusy(true);
    setMemMsg(null);
    try {
      const r: MemoryInfo = await s.rpc!.call("memory/save", { scope, text: memText[scope], cwd });
      setMemInfo(r);
      setMemMsg("已保存，新对话开始时生效");
    } catch (e) {
      setMemMsg(`保存失败：${(e as Error).message}`);
    } finally {
      setMemBusy(false);
    }
  };

  const selCls = "h-8 min-w-[220px] justify-between border border-line bg-bg px-2.5 text-fg";
  const lean = draft.profile === "lean";
  return (
    <>
      <h2 className="mb-1 text-[20px] font-semibold tracking-tight text-fg">工具与记忆</h2>
      <p className="mb-5 text-[13px] text-muted">这些开关决定新对话里模型能用哪些工具；已经开始的对话保持原样（为了不破坏前缀缓存）。</p>
      <Card title="模式" desc="精简模式只给模型 read / write / edit / shell 四个工具和一份很短的系统提示词，固定开销约少八成；权限审批、Hooks 完全不变。只对新对话生效。">
        <Row label="工具模式" desc={lean ? "精简：下面的工具开关在精简模式下不生效" : "完整：按下面的开关提供工具"}>
          <Select
            value={lean ? "lean" : "full"}
            options={[
              { value: "full", label: "完整（默认）" },
              { value: "lean", label: "精简" },
            ]}
            onChange={(v) => up({ profile: v === "lean" ? "lean" : "full" })}
            side="bottom"
            align="end"
            className={selCls}
          />
        </Row>
        <Row label="任务清单（todo）" desc="多步任务时模型用清单列计划、标进度">
          <Toggle on={draft.todo ?? true} onChange={(v) => up({ todo: v })} />
        </Row>
        <Row label="计划提交（present_plan）" desc="计划模式下用它提交方案等你批准；关闭后计划模式直接用文字给方案">
          <Toggle on={draft.present_plan ?? true} onChange={(v) => up({ present_plan: v })} />
        </Row>
      </Card>
      <Card title="网页搜索" desc="web_search 搜索网页、web_fetch 打开网页。每个请求最多等待下面的超时时间，代理跟随「模型与供应商 → 网络」。">
        <Row label="搜索后端" desc={SEARCH_LABEL[web.search_backend]?.[1]}>
          <Select
            value={web.search_backend}
            options={backends.map((v) => ({ value: v, label: SEARCH_LABEL[v]?.[0] ?? v, hint: SEARCH_LABEL[v]?.[1] }))}
            onChange={(v) => setWeb({ search_backend: v })}
            side="bottom"
            align="end"
            className={selCls}
          />
        </Row>
        {web.search_backend !== "off" && (
          <Row label="出错时改用" desc="主搜索后端报错（限流、没 key、超时）时自动改用的后端">
            <Select
              value={(web.search_fallback ?? []).find((b) => b !== web.search_backend) ?? "none"}
              options={[
                { value: "none", label: "不改用" },
                ...backends
                  .filter((v) => v !== "off" && v !== web.search_backend)
                  .map((v) => ({ value: v, label: SEARCH_LABEL[v]?.[0] ?? v })),
              ]}
              onChange={(v) => setWeb({ search_fallback: v === "none" ? [] : [v] })}
              side="bottom"
              align="end"
              className={selCls}
            />
          </Row>
        )}
        {web.search_backend === "deepseek" && (
          <Row label="DeepSeek key" desc="在「模型与供应商」里的 deepseek 供应商填写（或环境变量 DEEPSEEK_API_KEY）">
            <span className={clsx("text-[12.5px]", web.deepseek_key_available ? "text-muted" : "text-danger")}>
              {web.deepseek_key_available ? "已可用" : "还没有 key"}
            </span>
          </Row>
        )}
        {web.search_backend === "tavily" && (
          <Row label="Tavily API key" desc="也可以设置环境变量 TAVILY_API_KEY">
            <KeyInput k={web.keys.tavily} onChange={(k) => setWeb({ keys: { ...web.keys, tavily: k } })} />
          </Row>
        )}
        {web.search_backend === "brave" && (
          <Row label="Brave API key" desc="也可以设置环境变量 BRAVE_API_KEY">
            <KeyInput k={web.keys.brave} onChange={(k) => setWeb({ keys: { ...web.keys, brave: k } })} />
          </Row>
        )}
        <Row label="网页抓取" desc={FETCH_LABEL[web.fetch_backend]?.[1]}>
          <Select
            value={web.fetch_backend}
            options={fetches.map((v) => ({ value: v, label: FETCH_LABEL[v]?.[0] ?? v, hint: FETCH_LABEL[v]?.[1] }))}
            onChange={(v) => setWeb({ fetch_backend: v })}
            side="bottom"
            align="end"
            className={selCls}
          />
        </Row>
        <Row label="请求超时（秒）" desc="3～120">
          <Input className="w-24 text-right" inputMode="numeric" value={web.timeout} onChange={(e) => setWeb({ timeout: Number(e.target.value) || 0 })} />
        </Row>
        <Row label="默认结果条数" desc="1～20">
          <Input className="w-24 text-right" inputMode="numeric" value={web.max_results} onChange={(e) => setWeb({ max_results: Number(e.target.value) || 0 })} />
        </Row>
      </Card>

      <Card title="子代理" desc="模型可以用 task 工具派生子代理处理独立的子任务（只读探索或继承当前权限），只把最终报告交回来。只读的子代理可以同时跑多个。">
        <Row label="启用子代理">
          <Toggle on={sub.enabled} onChange={(v) => up({ subagent: { ...sub, enabled: v } })} />
        </Row>
        <Row label="默认最大步数" desc="子代理最多连续调用工具的轮数（1～100），模型也可以单次指定">
          <Input className="w-24 text-right" inputMode="numeric" value={sub.max_steps} onChange={(e) => up({ subagent: { ...sub, max_steps: Number(e.target.value) || 0 } })} />
        </Row>
        <Row label="同时运行数" desc="同一步里最多几个子代理一起跑（1～8），多出来的排队">
          <Input className="w-24 text-right" inputMode="numeric" value={sub.max_parallel} onChange={(e) => up({ subagent: { ...sub, max_parallel: Number(e.target.value) || 0 } })} />
        </Row>
        <Row label="写型子代理用 worktree 并行" desc="多个会改文件的子代理各自在独立的 git 分支和目录里同时干活，结束后由你决定是否合并；关闭或不是 git 仓库时按顺序在主目录执行">
          <Toggle on={sub.worktree} onChange={(v) => up({ subagent: { ...sub, worktree: v } })} />
        </Row>
      </Card>

      <Card title="记忆" desc="长期记忆在每次新对话开始时读入；你可以让 ee “记住…”，也可以直接在这里编辑。每条一行，以 - 开头。">
        <Row label="启用记忆" desc="总开关：关闭后不读入记忆，也不提供 memory 工具">
          <Toggle on={mem.enabled} onChange={(v) => up({ memory: { ...mem, enabled: v } })} />
        </Row>
        <Row label="新对话读入记忆" desc="对话开始时把记忆放进上下文">
          <Toggle on={mem.inject ?? true} onChange={(v) => up({ memory: { ...mem, inject: v } })} />
        </Row>
        <Row label="memory 工具" desc="让模型按你的要求增删记忆（精简模式下不提供）">
          <Toggle on={mem.tool ?? true} onChange={(v) => up({ memory: { ...mem, tool: v } })} />
        </Row>
        <Row label="工作区记忆" desc="额外读入项目目录下的 .ee/memory.md">
          <Toggle on={mem.workspace} onChange={(v) => up({ memory: { ...mem, workspace: v } })} />
        </Row>
        {!memInfo ? (
          <div className="py-3">{memMsg ? <span className="text-[12.5px] text-danger">{memMsg}</span> : <Spinner />}</div>
        ) : (
          <div className="space-y-4 pt-3">
            {(["user", "workspace"] as const).map((scope) => {
              const f = scope === "user" ? memInfo.user : memInfo.workspace;
              if (!f) return null;
              const changed = memText[scope] !== f.text;
              return (
                <div key={scope}>
                  <div className="mb-1.5 flex items-center gap-2 text-[12.5px] text-muted">
                    <span className="text-fg">{scope === "user" ? "用户级" : "工作区级"}</span>
                    <span className="mono truncate text-faint">{f.path}</span>
                    <span className="ml-auto shrink-0 text-faint">{f.entries.length} 条</span>
                  </div>
                  <textarea
                    className="mono h-32 w-full resize-y rounded-xl border border-line bg-bg px-3 py-2 text-[12.5px] text-fg outline-none focus:border-accent"
                    value={memText[scope]}
                    placeholder={"- 回答用中文，简洁\n- 这个项目用 pnpm"}
                    onChange={(e) => setMemText({ ...memText, [scope]: e.target.value })}
                  />
                  {changed && (
                    <div className="mt-1.5 flex justify-end gap-2">
                      <Button variant="outline" size="sm" onClick={() => setMemText({ ...memText, [scope]: f.text })}>
                        还原
                      </Button>
                      <Button size="sm" disabled={memBusy} onClick={() => saveMem(scope)}>
                        保存记忆
                      </Button>
                    </div>
                  )}
                </div>
              );
            })}
            {memMsg && <div className="text-[12.5px] text-muted">{memMsg}</div>}
          </div>
        )}
      </Card>

      <SkillsCard draft={draft} up={up} cwd={cwd} />

      <PermanentRules />
    </>
  );
}

/** 审批时选「本项目以后都允许」留下的永久规则（D4-8，~/.ee/permissions.json）。 */
function PermanentRules() {
  const rpc = useApp((s) => s.rpc);
  const [rules, setRules] = useState<{ project: string; rule: string; added?: number; title?: string }[] | null>(null);
  const [path, setPath] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const load = () =>
    rpc!
      .call("permissions/list", {})
      .then((r) => {
        setRules(r.rules);
        setPath(r.path);
      })
      .catch((e) => setErr((e as Error).message));
  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const remove = async (project: string, rule: string) => {
    try {
      await rpc!.call("permissions/remove", { project, rule });
      load();
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  const groups = new Map<string, { rule: string; title?: string }[]>();
  for (const r of rules ?? []) groups.set(r.project, [...(groups.get(r.project) ?? []), r]);
  return (
    <Card
      title="永久允许的操作"
      desc={<>审批时点「本项目以后都允许」记下的规则，只在对应项目（及其子目录）生效，保存在 <span className="mono">{path || "~/.ee/permissions.json"}</span>，不会写进项目仓库。项目里不想让 ee 读写的文件，写进项目根目录的 <span className="mono">.eeignore</span>（语法同 .gitignore）。</>}
    >
      {err && <div className="pb-2 text-[12.5px] text-danger">{err}</div>}
      {rules === null ? (
        <Spinner />
      ) : rules.length === 0 ? (
        <div className="text-[12.5px] text-faint">还没有永久允许的规则</div>
      ) : (
        <div className="space-y-3">
          {[...groups.entries()].map(([proj, list]) => (
            <div key={proj}>
              <div className="mono mb-1 truncate text-[12px] text-muted" title={proj}>
                {proj}
              </div>
              {list.map((r) => (
                <div key={r.rule} className="flex items-center gap-2 border-b border-line py-1.5 last:border-b-0">
                  <span className="mono min-w-0 flex-1 truncate text-[12.5px] text-fg" title={r.title}>
                    {r.rule}
                  </span>
                  <Button size="sm" variant="ghost" onClick={() => remove(proj, r.rule)}>
                    删除
                  </Button>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}
