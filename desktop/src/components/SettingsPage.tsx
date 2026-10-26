import clsx from "clsx";
import { ArrowLeft, Check, Cpu, Info, KeyRound, Monitor, Moon, Plus, Server, SlidersHorizontal, Star, Sun, Trash2, Wrench } from "lucide-react";
import React, { useEffect, useMemo, useState } from "react";
import { useApp, type Theme } from "../lib/store";
import type { ModelSettings, NetworkSettings, ProviderSettings, Settings, SettingsResponse } from "../types";
import { AgentSettings } from "./AgentSettings";
import { Button, Field, Input, Select, Spinner, Tip } from "./ui";

const SECTIONS = ["general", "models", "tools", "about"] as const;
type Section = (typeof SECTIONS)[number];

const SHELL_LABEL: Record<string, string> = {
  auto: "自动（pwsh → Windows PowerShell）",
  pwsh: "PowerShell 7（pwsh）",
  powershell: "Windows PowerShell 5.1",
  "git-bash": "Git Bash",
  cmd: "命令提示符（cmd）",
  bash: "bash（WSL / macOS / Linux）",
};
const MODE_LABEL: Record<string, [string, string]> = {
  "read-only": ["只读", "只能读文件和搜索，不改任何东西"],
  default: ["默认", "工作区内读写免问；其他命令、工作区外写入先问你"],
  auto: ["全自动", "都不问；deny 规则和危险命令仍会拦下"],
};
const FAMILY_LABEL: Record<string, string> = {
  auto: "自动识别", deepseek: "DeepSeek", qwen: "Qwen", kimi: "Kimi", glm: "GLM", minimax: "MiniMax",
  doubao: "豆包", gpt: "GPT", claude: "Claude", gemini: "Gemini", default: "通用",
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

function SettingRow({ label, desc, children }: { label: string; desc?: string; children: React.ReactNode }) {
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

function Segmented<T extends string>({ value, options, onChange }: { value: T; options: { value: T; label: string; icon?: React.ReactNode }[]; onChange: (v: T) => void }) {
  return (
    <div className="inline-flex rounded-lg border border-line bg-soft p-0.5">
      {options.map((o) => (
        <button
          key={o.value}
          onClick={() => onChange(o.value)}
          className={clsx(
            "flex h-7 items-center gap-1.5 rounded-md px-3 text-[12.5px] transition-colors",
            value === o.value ? "bg-elev text-fg shadow-sm" : "text-muted hover:text-fg",
          )}
        >
          {o.icon}
          {o.label}
        </button>
      ))}
    </div>
  );
}

function net(d: Settings): NetworkSettings {
  return d.network ?? { proxy_mode: "system", proxy_url: "", connect_timeout: 15, read_timeout: 90, retries: 2 };
}

function num(v: any): string {
  return v === null || v === undefined ? "" : String(v);
}

function ProviderEditor({ name, p, onChange, onDelete, usedBy }: { name: string; p: ProviderSettings; onChange: (p: ProviderSettings) => void; onDelete: () => void; usedBy: number }) {
  const [keyDraft, setKeyDraft] = useState("");
  const keyState =
    p.api_key === "" ? "将清除" : p.api_key ? "将保存新 key" : p.api_key_set ? `已保存（${p.api_key_preview}）` : p.env_present ? `使用环境变量 ${p.api_key_env}` : "未配置";
  const ok = (p.api_key_set && p.api_key !== "") || !!p.api_key || p.env_present;
  return (
    <div className="rounded-xl border border-line p-4">
      <div className="mb-3 flex items-center gap-2">
        <Server size={14} className="text-muted" />
        <span className="mono text-[13px] font-semibold text-fg">{name}</span>
        <span
          className={clsx(
            "ml-1 rounded-full px-2 py-px text-[11px]",
            ok ? "bg-add-bg text-add" : "bg-del-bg text-del",
          )}
        >
          {ok ? "key 可用" : "缺少 key"}
        </span>
        <span className="flex-1" />
        {!p.builtin && (
          <Tip label={usedBy ? `有 ${usedBy} 个模型在用，先删模型` : "删除供应商"}>
            <span>
              <Button size="icon-sm" variant="danger" disabled={usedBy > 0} onClick={onDelete}>
                <Trash2 size={13} />
              </Button>
            </span>
          </Tip>
        )}
      </div>
      <div className="grid grid-cols-2 gap-3">
        <Field label="接口地址 base_url" className="col-span-2">
          <Input value={p.base_url} onChange={(e) => onChange({ ...p, base_url: e.target.value })} placeholder="https://api.example.com/v1" />
        </Field>
        <Field label="API key" hint={keyState}>
          <div className="flex gap-1.5">
            <Input
              type="password"
              value={keyDraft}
              placeholder={p.api_key_set ? `已保存 ${p.api_key_preview}，输入以替换` : "sk-…（保存在本机 config.toml）"}
              onChange={(e) => {
                setKeyDraft(e.target.value);
                onChange({ ...p, api_key: e.target.value || null });
              }}
            />
            {p.api_key_set && (
              <Button
                variant="outline"
                size="sm"
                className="h-8"
                onClick={() => {
                  setKeyDraft("");
                  onChange({ ...p, api_key: "" });
                }}
              >
                清除
              </Button>
            )}
          </div>
        </Field>
        <Field label="或：key 所在的环境变量" hint={p.api_key_env ? (p.env_present ? "当前环境里已设置" : "当前环境里没有这个变量") : "留空则只用上面保存的 key"}>
          <Input value={p.api_key_env} onChange={(e) => onChange({ ...p, api_key_env: e.target.value })} placeholder="DEEPSEEK_API_KEY" className="mono" />
        </Field>
      </div>
    </div>
  );
}

function ModelEditor({
  name,
  m,
  providers,
  families,
  isDefault,
  onChange,
  onDelete,
  onDefault,
}: {
  name: string;
  m: ModelSettings;
  providers: string[];
  families: string[];
  isDefault: boolean;
  onChange: (m: ModelSettings) => void;
  onDelete: () => void;
  onDefault: () => void;
}) {
  const dp = m.default_price;
  const setPrice = (k: "input" | "output" | "cache_read" | "currency", v: string) => onChange({ ...m, price: { ...m.price, [k]: v === "" ? null : v } });
  return (
    <div className={clsx("rounded-xl border p-4", isDefault ? "border-accent/50" : "border-line")}>
      <div className="mb-3 flex items-center gap-2">
        <Cpu size={14} className="text-muted" />
        <span className="mono text-[13px] font-semibold text-fg">{name}</span>
        {isDefault ? (
          <span className="flex items-center gap-1 rounded-full bg-accent/15 px-2 py-px text-[11px] text-accent">
            <Star size={10} fill="currentColor" /> 默认
          </span>
        ) : (
          <button onClick={onDefault} className="rounded-full px-2 py-px text-[11px] text-faint hover:bg-hover hover:text-fg">
            设为默认
          </button>
        )}
        <span className="flex-1" />
        {!m.builtin && !isDefault && (
          <Tip label="删除模型">
            <Button size="icon-sm" variant="danger" onClick={onDelete}>
              <Trash2 size={13} />
            </Button>
          </Tip>
        )}
      </div>
      <div className="grid grid-cols-4 gap-3">
        <Field label="供应商">
          <Select
            value={m.provider}
            options={providers.map((p) => ({ value: p, label: p }))}
            onChange={(v) => onChange({ ...m, provider: v })}
            side="bottom"
            className="h-8 w-full justify-between border border-line bg-bg px-2.5 text-fg"
          />
        </Field>
        <Field label="模型 ID" className="col-span-2">
          <Input value={m.model} onChange={(e) => onChange({ ...m, model: e.target.value })} className="mono" placeholder="deepseek-chat" />
        </Field>
        <Field label="家族">
          <Select
            value={m.family || "auto"}
            options={families.map((f) => ({
              value: f,
              label: f === "auto" ? `自动（${FAMILY_LABEL[m.detected_family ?? "default"] ?? m.detected_family}）` : FAMILY_LABEL[f] ?? f,
            }))}
            onChange={(v) => onChange({ ...m, family: v })}
            side="bottom"
            className="h-8 w-full justify-between border border-line bg-bg px-2.5 text-fg"
          />
        </Field>
        <Field label="输入单价 / 百万">
          <Input inputMode="decimal" value={num(m.price.input)} placeholder={num(dp?.input) || "—"} onChange={(e) => setPrice("input", e.target.value)} />
        </Field>
        <Field label="输出单价 / 百万">
          <Input inputMode="decimal" value={num(m.price.output)} placeholder={num(dp?.output) || "—"} onChange={(e) => setPrice("output", e.target.value)} />
        </Field>
        <Field label="缓存命中单价 / 百万">
          <Input inputMode="decimal" value={num(m.price.cache_read)} placeholder={num(dp?.cache_read) || "—"} onChange={(e) => setPrice("cache_read", e.target.value)} />
        </Field>
        <Field label="币种 · 上下文窗口">
          <div className="flex gap-1.5">
            <Input className="w-12 text-center" value={num(m.price.currency)} placeholder={dp?.currency ?? "¥"} onChange={(e) => setPrice("currency", e.target.value)} />
            <Input
              inputMode="numeric"
              value={num(m.context_window)}
              placeholder={num(m.default_context_window)}
              onChange={(e) => onChange({ ...m, context_window: e.target.value || null })}
            />
          </div>
        </Field>
        <Field label="看图（图片输入）">
          <Select
            value={m.vision === true ? "on" : m.vision === false ? "off" : "auto"}
            options={[
              { value: "auto", label: `自动（${m.default_vision ? "支持" : "不支持"}）` },
              { value: "on", label: "支持" },
              { value: "off", label: "不支持" },
            ]}
            onChange={(v) => onChange({ ...m, vision: v === "on" ? true : v === "off" ? false : null })}
            className="h-8 w-full justify-between border border-line bg-bg px-2.5 text-fg"
          />
        </Field>
      </div>
    </div>
  );
}

function AddRow({ placeholder, onAdd, exists }: { placeholder: string; onAdd: (name: string) => void; exists: (n: string) => boolean }) {
  const [v, setV] = useState("");
  const name = v.trim();
  const bad = !name || exists(name) || !/^[A-Za-z0-9._-]+$/.test(name);
  return (
    <div className="flex items-center gap-2">
      <Input value={v} onChange={(e) => setV(e.target.value)} placeholder={placeholder} className="mono max-w-[260px]" />
      <Button
        variant="outline"
        size="sm"
        className="h-8"
        disabled={bad}
        onClick={() => {
          onAdd(name);
          setV("");
        }}
      >
        <Plus size={13} /> 添加
      </Button>
      {name && exists(name) && <span className="text-[12px] text-danger">名字已存在</span>}
    </div>
  );
}

export function SettingsPage() {
  const s = useApp();
  const [section, setSection] = useState<Section>(() => {
    const want = useApp.getState().settingsSection;
    return want && (SECTIONS as readonly string[]).includes(want) ? (want as Section) : "general";
  });
  useEffect(() => {
    if (s.settingsSection) {
      if ((SECTIONS as readonly string[]).includes(s.settingsSection)) setSection(s.settingsSection as Section);
      s.set({ settingsSection: null });
    }
  }, [s.settingsSection]);
  const [resp, setResp] = useState<SettingsResponse | null>(null);
  const [draft, setDraft] = useState<Settings | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [appInfo, setAppInfo] = useState<{ version: string; logs: string } | null>(null);

  const [loadError, setLoadError] = useState<string | null>(null);
  const [loadNonce, setLoadNonce] = useState(0);

  useEffect(() => {
    setLoadError(null);
    s.rpc
      ?.call("settings/get")
      .then((r: SettingsResponse) => {
        setResp(r);
        setDraft(structuredClone(r.settings));
      })
      .catch((e: Error) => setLoadError(e.message));
    window.ee?.appInfo().then(setAppInfo).catch(() => undefined);
  }, [s.rpc, loadNonce]);

  const dirty = useMemo(() => {
    if (!resp || !draft) return false;
    return JSON.stringify(resp.settings) !== JSON.stringify(draft);
  }, [resp, draft]);

  if (!draft || !resp) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 text-muted">
        {loadError ? (
          <>
            <div className="max-w-[460px] text-center text-[13px] text-danger">读取设置失败：{loadError}</div>
            <div className="flex gap-2">
              <Button variant="outline" size="sm" onClick={() => setLoadNonce((n) => n + 1)}>
                重试
              </Button>
              <Button variant="outline" size="sm" onClick={() => s.set({ view: "chat" })}>
                返回
              </Button>
            </div>
          </>
        ) : (
          <Spinner />
        )}
      </div>
    );
  }

  const up = (p: Partial<Settings>) => {
    setDraft({ ...draft, ...p });
    setSaved(false);
  };
  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const r: SettingsResponse = await s.rpc!.call("settings/set", { settings: draft });
      setResp(r);
      setDraft(structuredClone(r.settings));
      s.setTheme(r.settings.ui.theme as Theme);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const nav: { id: Section; label: string; icon: React.ReactNode }[] = [
    { id: "general", label: "常规", icon: <SlidersHorizontal size={15} /> },
    { id: "models", label: "模型与供应商", icon: <KeyRound size={15} /> },
    { id: "tools", label: "工具与记忆", icon: <Wrench size={15} /> },
    { id: "about", label: "关于", icon: <Info size={15} /> },
  ];
  const providerNames = Object.keys(draft.providers);

  return (
    <div className="flex min-w-0 flex-1 bg-bg">
      <div className="flex w-[220px] shrink-0 flex-col border-r border-line px-2 pt-0">
        <div className="drag flex h-11 items-center px-2">
          <button onClick={() => s.set({ view: "chat" })} className="no-drag flex items-center gap-1.5 rounded-md px-1.5 py-1 text-[12.5px] text-muted hover:bg-hover hover:text-fg">
            <ArrowLeft size={14} /> 返回
          </button>
        </div>
        <div className="px-2.5 pb-2 pt-1 text-[16px] font-semibold text-fg">设置</div>
        {nav.map((n) => (
          <button
            key={n.id}
            onClick={() => setSection(n.id)}
            className={clsx(
              "mb-px flex h-8 items-center gap-2.5 rounded-lg px-2.5 text-[13px] transition-colors",
              section === n.id ? "bg-active text-fg" : "text-muted hover:bg-hover hover:text-fg",
            )}
          >
            {n.icon}
            {n.label}
          </button>
        ))}
      </div>
      <div className="relative flex min-w-0 flex-1 flex-col">
        <div className="drag h-11 shrink-0" style={{ marginRight: "var(--overlay-w)" }} />
        <div className="flex-1 overflow-y-auto">
          <div className="mx-auto max-w-[820px] px-8 pb-28 pt-2">
            {section === "general" && (
              <>
                <h2 className="mb-5 text-[20px] font-semibold tracking-tight text-fg">常规</h2>
                <Card title="外观">
                  <SettingRow label="主题" desc="跟随系统，或固定为深色 / 浅色">
                    <Segmented<Theme>
                      value={draft.ui.theme}
                      onChange={(v) => {
                        up({ ui: { theme: v } });
                        s.setTheme(v);
                      }}
                      options={[
                        { value: "system", label: "系统", icon: <Monitor size={13} /> },
                        { value: "light", label: "浅色", icon: <Sun size={13} /> },
                        { value: "dark", label: "深色", icon: <Moon size={13} /> },
                      ]}
                    />
                  </SettingRow>
                </Card>
                <Card title="执行">
                  <SettingRow label="默认 shell" desc="ee 运行命令时用的 shell，模型会按它的语法写命令">
                    <Select
                      value={draft.shell}
                      options={resp.choices.shells.map((v) => ({ value: v, label: SHELL_LABEL[v] ?? v }))}
                      onChange={(v) => up({ shell: v })}
                      side="bottom"
                      align="end"
                      className="h-8 min-w-[220px] justify-between border border-line bg-bg px-2.5 text-fg"
                    />
                  </SettingRow>
                  <SettingRow label="默认权限模式" desc="新对话使用的模式；输入框下方可以随时切换">
                    <Select
                      value={draft.permissions.mode}
                      options={resp.choices.modes.map((v) => ({ value: v, label: MODE_LABEL[v]?.[0] ?? v, hint: MODE_LABEL[v]?.[1] }))}
                      onChange={(v) => up({ permissions: { mode: v } })}
                      side="bottom"
                      align="end"
                      className="h-8 min-w-[220px] justify-between border border-line bg-bg px-2.5 text-fg"
                    />
                  </SettingRow>
                  <SettingRow label="每回合最大步数" desc="一个请求里模型最多连续调用工具的轮数">
                    <Input className="w-24 text-right" inputMode="numeric" value={draft.max_steps} onChange={(e) => up({ max_steps: Number(e.target.value) || 0 })} />
                  </SettingRow>
                  <SettingRow label="命令超时（秒）" desc="单条命令的默认超时">
                    <Input className="w-24 text-right" inputMode="numeric" value={draft.bash_timeout} onChange={(e) => up({ bash_timeout: Number(e.target.value) || 0 })} />
                  </SettingRow>
                </Card>
              </>
            )}

            {section === "models" && (
              <>
                <h2 className="mb-1 text-[20px] font-semibold tracking-tight text-fg">模型与供应商</h2>
                <p className="mb-5 text-[13px] text-muted">只支持 OpenAI Chat Completions 协议。DeepSeek、通义千问、Kimi、智谱 GLM、MiniMax、豆包和中转站都可以接。</p>
                <Card title="默认模型" desc="新对话默认使用的模型">
                  <Select
                    value={draft.default_model}
                    options={Object.entries(draft.models).map(([n, m]) => ({ value: n, label: n, hint: `${m.provider} · ${m.model}` }))}
                    onChange={(v) => up({ default_model: v })}
                    side="bottom"
                    className="h-8 min-w-[260px] justify-between border border-line bg-bg px-2.5 text-fg"
                  />
                </Card>
                <Card title="网络" desc="模型请求怎么走代理、等多久算超时。连不上或没反应时先看这里。">
                  <SettingRow label="代理" desc={draft.network?.proxy_mode === "system" ? `当前：${draft.network?.system_proxy || "未检测"}` : "本机地址（127.0.0.1）永远直连"}>
                    <Segmented
                      value={draft.network?.proxy_mode ?? "system"}
                      options={[
                        { value: "system", label: "跟随系统" },
                        { value: "direct", label: "直连" },
                        { value: "custom", label: "自定义" },
                      ]}
                      onChange={(v) => up({ network: { ...net(draft), proxy_mode: v } })}
                    />
                  </SettingRow>
                  {draft.network?.proxy_mode === "custom" && (
                    <SettingRow label="代理地址" desc="HTTP 代理，例如 Clash / v2rayN 的 http://127.0.0.1:7890">
                      <Input
                        className="mono w-[260px]"
                        value={draft.network?.proxy_url ?? ""}
                        placeholder="http://127.0.0.1:7890"
                        onChange={(e) => up({ network: { ...net(draft), proxy_url: e.target.value } })}
                      />
                    </SettingRow>
                  )}
                  <SettingRow label="连接超时（秒）" desc="建立连接（含代理握手、TLS）最长等多久">
                    <Input className="w-24 text-right" inputMode="numeric" value={num(draft.network?.connect_timeout)} onChange={(e) => up({ network: { ...net(draft), connect_timeout: Number(e.target.value) || 0 } })} />
                  </SettingRow>
                  <SettingRow label="读取超时（秒）" desc="多久收不到模型任何数据就判定失败（含等第一个字）">
                    <Input className="w-24 text-right" inputMode="numeric" value={num(draft.network?.read_timeout)} onChange={(e) => up({ network: { ...net(draft), read_timeout: Number(e.target.value) || 0 } })} />
                  </SettingRow>
                  <SettingRow label="失败重试次数" desc="连接失败、超时、429/5xx 时自动重试">
                    <Input className="w-24 text-right" inputMode="numeric" value={num(draft.network?.retries)} onChange={(e) => up({ network: { ...net(draft), retries: Number(e.target.value) || 0 } })} />
                  </SettingRow>
                </Card>
                <Card title="供应商" desc="接口地址和 key。key 可以直接保存在本机，也可以只填环境变量名。">
                  <div className="space-y-3">
                    {Object.entries(draft.providers).map(([n, p]) => (
                      <ProviderEditor
                        key={n}
                        name={n}
                        p={p}
                        usedBy={Object.values(draft.models).filter((m) => m.provider === n).length}
                        onChange={(np) => up({ providers: { ...draft.providers, [n]: np } })}
                        onDelete={() => {
                          const rest = { ...draft.providers };
                          delete rest[n];
                          up({ providers: rest });
                        }}
                      />
                    ))}
                    <AddRow
                      placeholder="新供应商名，如 dashscope"
                      exists={(n) => n in draft.providers}
                      onAdd={(n) => up({ providers: { ...draft.providers, [n]: { base_url: "", api_key_env: "", api_key: null } } })}
                    />
                  </div>
                </Card>
                <Card title="模型" desc="单价留空则用家族档案的默认价（灰色提示），用于计算花费。">
                  <div className="space-y-3">
                    {Object.entries(draft.models).map(([n, m]) => (
                      <ModelEditor
                        key={n}
                        name={n}
                        m={m}
                        providers={providerNames}
                        families={resp.choices.families}
                        isDefault={draft.default_model === n}
                        onDefault={() => up({ default_model: n })}
                        onChange={(nm) => up({ models: { ...draft.models, [n]: nm } })}
                        onDelete={() => {
                          const rest = { ...draft.models };
                          delete rest[n];
                          up({ models: rest });
                        }}
                      />
                    ))}
                    <AddRow
                      placeholder="新模型名，如 qwen-coder"
                      exists={(n) => n in draft.models}
                      onAdd={(n) =>
                        up({
                          models: {
                            ...draft.models,
                            [n]: { provider: providerNames[0] ?? "", model: n, family: "auto", context_window: null, price: { input: null, output: null, cache_read: null, currency: null } },
                          },
                        })
                      }
                    />
                  </div>
                </Card>
              </>
            )}

            {section === "tools" && <AgentSettings draft={draft} resp={resp} up={up} />}

            {section === "about" && (
              <>
                <h2 className="mb-5 text-[20px] font-semibold tracking-tight text-fg">关于</h2>
                <Card title="ee">
                  <SettingRow label="版本">
                    <span className="mono text-[12.5px] text-muted">{appInfo?.version ?? "开发版"}</span>
                  </SettingRow>
                  <SettingRow label="配置文件" desc="设置页修改的是用户级配置；项目里的 .ee/config.toml 会覆盖它">
                    <button className="mono max-w-[360px] truncate text-[12px] text-accent hover:underline" onClick={() => window.ee?.showItem(resp.path)}>
                      {resp.path}
                    </button>
                  </SettingRow>
                  {appInfo && (
                    <SettingRow label="日志目录">
                      <button className="mono max-w-[360px] truncate text-[12px] text-accent hover:underline" onClick={() => window.ee?.openPath(appInfo.logs)}>
                        {appInfo.logs}
                      </button>
                    </SettingRow>
                  )}
                </Card>
              </>
            )}
          </div>
        </div>
        {(dirty || error || saved) && section !== "about" && (
          <div className="pointer-events-none absolute bottom-6 left-0 right-0 flex justify-center">
            <div className="pointer-events-auto flex items-center gap-3 rounded-2xl border border-line bg-elev py-2 pl-4 pr-2 shadow-[var(--shadow)] fadein">
              <span className={clsx("max-w-[420px] truncate text-[13px]", error ? "text-danger" : "text-muted")}>
                {error ?? (saved ? "已保存到 config.toml" : "有未保存的修改")}
              </span>
              {dirty && (
                <>
                  <Button size="sm" onClick={() => { setDraft(structuredClone(resp.settings)); setError(null); }}>
                    放弃
                  </Button>
                  <Button variant="primary" size="sm" onClick={save} disabled={saving}>
                    {saving ? <Spinner size={12} /> : <Check size={13} />} 保存
                  </Button>
                </>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
