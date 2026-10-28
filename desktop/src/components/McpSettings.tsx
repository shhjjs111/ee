// 设置页「MCP」分区：服务器增删改、启用/信任、项目 .mcp.json 服务器允许、测试连接（列工具 + 状态点）。
import clsx from "clsx";
import { ChevronDown, ChevronRight, Globe, Plug, Plus, RefreshCw, ShieldCheck, Terminal, Trash2 } from "lucide-react";
import React, { useCallback, useEffect, useRef, useState } from "react";
import { formatArgs, formatKv, MCP_NAME_RE, parseArgs, parseKv } from "../lib/mcp";
import { useApp } from "../lib/store";
import type { McpListResponse, McpServerSettings, McpServerStatus, McpSettings as McpCfg, Settings, SettingsResponse } from "../types";
import { Button, Field, Input, Select, Spinner, Tip } from "./ui";

const DEFAULT_MCP: McpCfg = { enabled: true, startup_timeout: 20, tool_timeout: 120, project_json: true, allowed_project: [], trusted_project: [], servers: {} };

const STATUS: Record<string, [string, string]> = {
  ready: ["已连接", "bg-add"],
  connecting: ["连接中", "bg-accent"],
  error: ["连接失败", "bg-del"],
  idle: ["未连接（新对话开始时连接）", "bg-faint"],
  disabled: ["已停用", "bg-faint"],
  needs_approval: ["等待允许", "bg-warn"],
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

function Toggle({ on, onChange, label }: { on: boolean; onChange: (v: boolean) => void; label?: string }) {
  return (
    <button
      role="switch"
      aria-checked={on}
      aria-label={label}
      onClick={() => onChange(!on)}
      className={clsx("relative h-5 w-9 shrink-0 rounded-full transition-colors", on ? "bg-accent" : "border border-line bg-soft")}
    >
      <span className={clsx("absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-all", on ? "left-[18px]" : "left-0.5")} />
    </button>
  );
}

function Dot({ status, error }: { status: string; error?: string }) {
  const [label, cls] = STATUS[status] ?? [status, "bg-faint"];
  return (
    <Tip label={error ? `${label}：${error}` : label}>
      <span className="flex items-center gap-1.5 text-[11.5px] text-muted">
        <span className={clsx("h-2 w-2 rounded-full", cls)} />
        {label}
      </span>
    </Tip>
  );
}

function ToolList({ st }: { st: McpServerStatus }) {
  const [open, setOpen] = useState(false);
  if (st.status === "error") return <div className="mt-2 whitespace-pre-wrap break-all rounded-lg bg-del-bg px-3 py-2 text-[12px] text-del">{st.error || "连接失败"}</div>;
  if (!st.tools.length) return st.status === "ready" ? <div className="mt-2 text-[12px] text-faint">已连接，但这个服务器没有提供工具。</div> : null;
  return (
    <div className="mt-2">
      <button onClick={() => setOpen(!open)} className="flex items-center gap-1 text-[12px] text-muted hover:text-fg">
        {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        {st.tools.length} 个工具
        {st.server_info?.name && <span className="text-faint">· {st.server_info.name} {st.server_info.version ?? ""}</span>}
      </button>
      {open && (
        <ul className="mt-1.5 space-y-1 rounded-lg border border-line bg-bg px-3 py-2">
          {st.tools.map((t) => (
            <li key={t.name} className="text-[12px]">
              <span className="mono text-fg">{t.name}</span>
              {t.read_only && <span className="ml-1.5 rounded bg-soft px-1 text-[10.5px] text-muted">只读</span>}
              {t.description && <span className="ml-2 text-faint">{t.description}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

type TestState = { busy: boolean; result?: McpServerStatus; err?: string };

function ServerEditor({
  name,
  sv,
  live,
  test,
  onTest,
  onChange,
  onDelete,
}: {
  name: string;
  sv: McpServerSettings;
  live?: McpServerStatus;
  test?: TestState;
  onTest: () => void;
  onChange: (sv: McpServerSettings) => void;
  onDelete: () => void;
}) {
  const remote = sv.url !== undefined && sv.command === undefined ? true : !!sv.url;
  // 参数 / 环境变量 / 请求头 用本地文本，避免输入时被格式化打断
  const [argsText, setArgsText] = useState(() => formatArgs(sv.args ?? []));
  const [envText, setEnvText] = useState(() => formatKv(sv.env, "="));
  const [hdrText, setHdrText] = useState(() => formatKv(sv.headers, ":"));
  const shown = test?.result ?? live;
  const taCls = "mono h-20 w-full resize-y rounded-lg border border-line bg-bg px-2.5 py-1.5 text-[12.5px] text-fg outline-none placeholder:text-faint focus:border-accent";
  return (
    <div className={clsx("rounded-xl border p-4", sv.enabled ? "border-line" : "border-line opacity-70")}>
      <div className="mb-3 flex items-center gap-2">
        {remote ? <Globe size={14} className="text-muted" /> : <Terminal size={14} className="text-muted" />}
        <span className="mono text-[13px] font-semibold text-fg">{name}</span>
        {shown && <Dot status={sv.enabled ? shown.status : "disabled"} error={shown.error} />}
        <span className="flex-1" />
        <Button variant="outline" size="sm" className="h-7" disabled={test?.busy} onClick={onTest}>
          {test?.busy ? <Spinner size={12} /> : <Plug size={12} />} 测试连接
        </Button>
        <Tip label="删除服务器">
          <Button size="icon-sm" variant="danger" onClick={onDelete}>
            <Trash2 size={13} />
          </Button>
        </Tip>
      </div>
      <div className="mb-3 inline-flex rounded-lg border border-line bg-soft p-0.5">
        {[
          { v: false, label: "本地命令（stdio）" },
          { v: true, label: "远程（HTTP / SSE）" },
        ].map((o) => (
          <button
            key={String(o.v)}
            onClick={() => {
              if (o.v === remote) return;
              const { command, args, env, cwd, url, headers, type, ...rest } = sv;
              onChange(o.v ? { ...rest, url: url ?? "", headers, type } : { ...rest, command: command ?? "", args, env, cwd });
            }}
            className={clsx("h-7 rounded-md px-3 text-[12.5px] transition-colors", remote === o.v ? "bg-elev text-fg shadow-sm" : "text-muted hover:text-fg")}
          >
            {o.label}
          </button>
        ))}
      </div>
      {remote ? (
        <div className="grid grid-cols-4 gap-3">
          <Field label="地址 URL" className="col-span-3">
            <Input className="mono" value={sv.url ?? ""} placeholder="https://example.com/mcp" onChange={(e) => onChange({ ...sv, url: e.target.value })} />
          </Field>
          <Field label="传输方式">
            <Select
              value={sv.type || "auto"}
              options={[
                { value: "auto", label: "自动", hint: "地址以 /sse 结尾按旧式 SSE，否则 Streamable HTTP" },
                { value: "http", label: "Streamable HTTP" },
                { value: "sse", label: "旧式 SSE" },
              ]}
              onChange={(v) => onChange({ ...sv, type: v === "auto" ? "" : v })}
              className="h-8 w-full justify-between border border-line bg-bg px-2.5 text-fg"
            />
          </Field>
          <Field label="请求头（每行 名称: 值，可用 ${环境变量}）" className="col-span-4">
            <textarea
              className={taCls}
              value={hdrText}
              placeholder={"Authorization: Bearer ${MY_TOKEN}"}
              onChange={(e) => {
                setHdrText(e.target.value);
                onChange({ ...sv, headers: parseKv(e.target.value, ":") });
              }}
            />
          </Field>
        </div>
      ) : (
        <div className="grid grid-cols-4 gap-3">
          <Field label="命令" hint="程序名或完整路径，参数填右边" className="col-span-1">
            <Input className="mono" value={sv.command ?? ""} placeholder="npx" onChange={(e) => onChange({ ...sv, command: e.target.value })} />
          </Field>
          <Field label="参数" hint="空格分隔；含空格的参数用引号包住" className="col-span-3">
            <Input
              className="mono"
              value={argsText}
              placeholder="-y @modelcontextprotocol/server-filesystem D:\code"
              onChange={(e) => {
                setArgsText(e.target.value);
                onChange({ ...sv, args: parseArgs(e.target.value) });
              }}
            />
          </Field>
          <Field label="工作目录（可选）" className="col-span-4">
            <Input className="mono" value={sv.cwd ?? ""} placeholder="留空 = 当前项目目录" onChange={(e) => onChange({ ...sv, cwd: e.target.value })} />
          </Field>
          <Field label="环境变量（每行 名称=值，可用 ${环境变量}）" className="col-span-4">
            <textarea
              className={taCls}
              value={envText}
              placeholder={"GITHUB_TOKEN=${GITHUB_TOKEN}"}
              onChange={(e) => {
                setEnvText(e.target.value);
                onChange({ ...sv, env: parseKv(e.target.value, "=") });
              }}
            />
          </Field>
        </div>
      )}
      <div className="mt-3 flex flex-wrap items-center gap-x-6 gap-y-2 border-t border-line pt-3 text-[12.5px] text-muted">
        <label className="flex items-center gap-2">
          <Toggle on={sv.enabled} onChange={(v) => onChange({ ...sv, enabled: v })} label="启用" /> 启用
        </label>
        <label className="flex items-center gap-2">
          <Toggle on={sv.trusted} onChange={(v) => onChange({ ...sv, trusted: v })} label="信任" />
          <span>信任</span>
          <span className="text-faint">调用它的工具不再询问（只读模式下也放行它的只读工具）</span>
        </label>
      </div>
      {test?.err && <div className="mt-2 text-[12px] text-danger">{test.err}</div>}
      {shown && sv.enabled && <ToolList st={shown} />}
    </div>
  );
}

export function McpSettings({ draft, resp, up }: { draft: Settings; resp: SettingsResponse; up: (p: Partial<Settings>) => void }) {
  const s = useApp();
  const mcp: McpCfg = { ...DEFAULT_MCP, ...(draft.mcp ?? {}) };
  const setMcp = (p: Partial<McpCfg>) => up({ mcp: { ...mcp, ...p } });
  const cwd = (s.activeThreadId ? s.details[s.activeThreadId]?.cwd : null) ?? s.activeWorkspace ?? null;

  const [list, setList] = useState<McpListResponse | null>(null);
  const [listErr, setListErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [tests, setTests] = useState<Record<string, TestState>>({});
  const [newName, setNewName] = useState("");

  // 每次加载一个令牌：切换项目 / 保存后又加载时，迟到的旧结果丢掉
  const listGen = useRef(0);
  const reload = useCallback(() => {
    if (!s.rpc) return;
    const tok = ++listGen.current;
    setLoading(true);
    setListErr(null);
    s.rpc
      .call("mcp/list", { cwd })
      .then((r: McpListResponse) => {
        if (tok === listGen.current) setList(r);
      })
      .catch((e: Error) => {
        if (tok === listGen.current) setListErr(e.message);
      })
      .finally(() => {
        if (tok === listGen.current) setLoading(false);
      });
  }, [s.rpc, cwd]);
  // 保存后（resp 变化）重新拉状态
  useEffect(reload, [reload, resp]);

  const live = new Map((list?.servers ?? []).map((x) => [x.name, x]));
  const project = (list?.servers ?? []).filter((x) => x.source === "project");

  const runTest = async (name: string, server?: McpServerSettings) => {
    setTests((t) => ({ ...t, [name]: { busy: true } }));
    try {
      const r: McpServerStatus = await s.rpc!.call("mcp/test", server ? { name, server, cwd } : { name, cwd }, 120_000);
      setTests((t) => ({ ...t, [name]: { busy: false, result: r } }));
    } catch (e) {
      setTests((t) => ({ ...t, [name]: { busy: false, err: (e as Error).message } }));
    }
  };

  const name = newName.trim();
  const nameBad = !name || !MCP_NAME_RE.test(name) || name in mcp.servers;
  const allowed = new Set(mcp.allowed_project);
  const trusted = new Set(mcp.trusted_project ?? []);

  return (
    <>
      <h2 className="mb-1 text-[20px] font-semibold tracking-tight text-fg">MCP</h2>
      <p className="mb-5 text-[13px] text-muted">
        接入 MCP 服务器给模型加工具（工具名为 mcp__服务器__工具）。改动在新对话开始时生效；已经开始的对话保持原样。
      </p>

      <Card title="总开关">
        <Row label="启用 MCP" desc="关闭后新对话不连接任何 MCP 服务器">
          <Toggle on={mcp.enabled} onChange={(v) => setMcp({ enabled: v })} />
        </Row>
        <Row label="读取项目里的 .mcp.json" desc="从项目目录向上找到 git 根为止；项目里的服务器要你允许后才会连接">
          <Toggle on={mcp.project_json} onChange={(v) => setMcp({ project_json: v })} />
        </Row>
        <Row label="连接超时（秒）" desc="启动 / 握手最长等多久，3–300">
          <Input className="w-24 text-right" inputMode="numeric" value={mcp.startup_timeout} onChange={(e) => setMcp({ startup_timeout: Number(e.target.value) || 0 })} />
        </Row>
        <Row label="工具调用超时（秒）" desc="单次工具调用最长等多久，5–3600">
          <Input className="w-24 text-right" inputMode="numeric" value={mcp.tool_timeout} onChange={(e) => setMcp({ tool_timeout: Number(e.target.value) || 0 })} />
        </Row>
      </Card>

      <Card
        title="我的服务器"
        desc="保存在用户配置里，所有项目都能用。测试连接用的是当前填写的内容（不必先保存）。"
        action={
          <Tip label="刷新状态">
            <Button size="icon-sm" variant="ghost" onClick={reload} disabled={loading}>
              {loading ? <Spinner size={12} /> : <RefreshCw size={13} />}
            </Button>
          </Tip>
        }
      >
        <div className="space-y-3">
          {Object.keys(mcp.servers).length === 0 && <div className="text-[12.5px] text-faint">还没有服务器。在下面输入名字添加一个。</div>}
          {Object.entries(mcp.servers).map(([n, sv]) => (
            <ServerEditor
              key={n}
              name={n}
              sv={sv}
              live={live.get(n)?.source === "user" ? live.get(n) : undefined}
              test={tests[n]}
              onTest={() => runTest(n, sv)}
              onChange={(nsv) => {
                setTests((t) => {
                  const r = { ...t };
                  delete r[n];
                  return r;
                });
                setMcp({ servers: { ...mcp.servers, [n]: nsv } });
              }}
              onDelete={() => {
                const rest = { ...mcp.servers };
                delete rest[n];
                setMcp({ servers: rest });
              }}
            />
          ))}
          <div className="flex items-center gap-2">
            <Input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="新服务器名，如 github" className="mono max-w-[260px]" />
            <Button
              variant="outline"
              size="sm"
              className="h-8"
              disabled={nameBad}
              onClick={() => {
                setMcp({ servers: { ...mcp.servers, [name]: { command: "", args: [], enabled: true, trusted: false } } });
                setNewName("");
              }}
            >
              <Plus size={13} /> 添加
            </Button>
            {name && name in mcp.servers && <span className="text-[12px] text-danger">名字已存在</span>}
            {name && !MCP_NAME_RE.test(name) && <span className="text-[12px] text-danger">只能用字母、数字、_ 和 -（≤40 字）</span>}
          </div>
        </div>
      </Card>

      {mcp.project_json && (
        <Card
          title="项目服务器（.mcp.json）"
          desc={list?.project_json ? <span className="mono">{list.project_json}</span> : cwd ? "当前项目里没有找到 .mcp.json" : "先在左栏打开一个项目"}
        >
          {project.length === 0 ? (
            <div className="text-[12.5px] text-faint">没有项目服务器。{Object.keys(mcp.servers).length > 0 && "同名的「我的服务器」会覆盖项目里的配置。"}</div>
          ) : (
            <div className="space-y-3">
              {project.map((p) => {
                const ok = allowed.has(p.allow_id);
                const trustedOne = ok && trusted.has(p.allow_id);
                return (
                  <div key={p.name} className="rounded-xl border border-line p-3.5">
                    <div className="flex items-center gap-2">
                      {p.transport === "stdio" ? <Terminal size={14} className="text-muted" /> : <Globe size={14} className="text-muted" />}
                      <span className="mono text-[13px] font-semibold text-fg">{p.name}</span>
                      <Dot status={tests[`project:${p.name}`]?.result?.status ?? p.status} error={tests[`project:${p.name}`]?.result?.error ?? p.error} />
                      <span className="flex-1" />
                      {ok && (
                        <Button variant="outline" size="sm" className="h-7" disabled={tests[`project:${p.name}`]?.busy} onClick={() => runTestProject(p)}>
                          {tests[`project:${p.name}`]?.busy ? <Spinner size={12} /> : <Plug size={12} />} 测试连接
                        </Button>
                      )}
                      <Button
                        variant={ok ? "outline" : "primary"}
                        size="sm"
                        className="h-7"
                        onClick={() => setMcp({ allowed_project: ok ? mcp.allowed_project.filter((x) => x !== p.allow_id) : [...mcp.allowed_project, p.allow_id],
                          trusted_project: ok ? (mcp.trusted_project ?? []).filter((x) => x !== p.allow_id) : (mcp.trusted_project ?? []) })}
                      >
                        {ok ? "撤销允许" : (
                          <>
                            <ShieldCheck size={12} /> 允许
                          </>
                        )}
                      </Button>
                      {ok && (
                        <Button
                          variant="outline"
                          size="sm"
                          className="h-7"
                          title="调用它的工具不再询问（只读模式下也放行它的只读工具）。信任只记在你的配置里，项目文件改不了它。"
                          onClick={() => setMcp({ trusted_project: trustedOne
                            ? (mcp.trusted_project ?? []).filter((x) => x !== p.allow_id)
                            : [...(mcp.trusted_project ?? []), p.allow_id] })}
                        >
                          {trustedOne ? "取消信任" : "信任"}
                        </Button>
                      )}
                    </div>
                    <div className="mono mt-1.5 truncate text-[12px] text-faint">{p.transport === "stdio" ? [p.command, ...p.args].join(" ") : p.url}</div>
                    {!ok && <div className="mt-1 text-[12px] text-muted">项目文件里的命令会在本机运行，确认可信再允许。配置内容改了需要重新允许。</div>}
                    {ok && !trustedOne && <div className="mt-1 text-[12px] text-faint">允许只表示能连接；要免去每次调用的询问，另点「信任」（项目文件里的 trusted 不算数）。</div>}
                    {tests[`project:${p.name}`]?.err && <div className="mt-2 text-[12px] text-danger">{tests[`project:${p.name}`]?.err}</div>}
                    {tests[`project:${p.name}`]?.result && <ToolList st={tests[`project:${p.name}`]!.result!} />}
                  </div>
                );
              })}
            </div>
          )}
        </Card>
      )}

      {(listErr || (list?.errors.length ?? 0) > 0) && (
        <Card title="配置问题">
          <ul className="space-y-1 text-[12.5px] text-danger">
            {listErr && <li>读取 MCP 状态失败：{listErr}</li>}
            {list?.errors.map((e, i) => <li key={i}>{e}</li>)}
          </ul>
        </Card>
      )}
    </>
  );

  async function runTestProject(p: McpServerStatus) {
    const key = `project:${p.name}`;
    setTests((t) => ({ ...t, [key]: { busy: true } }));
    try {
      const r: McpServerStatus = await s.rpc!.call("mcp/test", { name: p.name, cwd }, 120_000);
      setTests((t) => ({ ...t, [key]: { busy: false, result: r } }));
    } catch (e) {
      setTests((t) => ({ ...t, [key]: { busy: false, err: (e as Error).message } }));
    }
  }
}
