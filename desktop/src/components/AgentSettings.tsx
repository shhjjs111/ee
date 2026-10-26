// 设置页「工具与记忆」：网页搜索后端与 key、子代理默认最大步数、记忆开关与编辑。
import clsx from "clsx";
import React, { useEffect, useState } from "react";
import { useApp } from "../lib/store";
import type { MemoryInfo, Settings, SettingsResponse, WebKeySettings, WebSettings } from "../types";
import { Button, Input, Select, Spinner } from "./ui";

const SEARCH_LABEL: Record<string, [string, string]> = {
  deepseek: ["DeepSeek（默认）", "复用 DeepSeek 的 API key；约 $0.75 / 千次"],
  tavily: ["Tavily", "需要 Tavily API key"],
  brave: ["Brave Search", "需要 Brave Search API key"],
  off: ["关闭", "不提供 web_search 工具"],
};
const FETCH_LABEL: Record<string, [string, string]> = {
  auto: ["自动", "DeepSeek 搜索且有 key 时用 DeepSeek Fetch，失败自动改直连"],
  deepseek: ["DeepSeek Fetch", "按次计费，失败自动改直连"],
  direct: ["直接抓取", "本机直接下载网页并转成文本，免费"],
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

function Toggle({ on, onChange }: { on: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      role="switch"
      aria-checked={on}
      onClick={() => onChange(!on)}
      className={clsx("relative h-5 w-9 rounded-full transition-colors", on ? "bg-accent" : "bg-soft border border-line")}
    >
      <span className={clsx("absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-all", on ? "left-[18px]" : "left-0.5")} />
    </button>
  );
}

const DEFAULT_WEB: WebSettings = {
  search_backend: "deepseek",
  fetch_backend: "auto",
  timeout: 20,
  max_results: 8,
  keys: { tavily: {}, brave: {} },
};

function KeyInput({ k, onChange }: { k: WebKeySettings; onChange: (k: WebKeySettings) => void }) {
  const status = k.api_key_set ? `已保存 ${k.api_key_preview ?? ""}` : k.env_present ? `使用环境变量 ${k.api_key_env}` : "未设置";
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

export function AgentSettings({ draft, resp, up }: { draft: Settings; resp: SettingsResponse; up: (p: Partial<Settings>) => void }) {
  const s = useApp();
  const web = { ...DEFAULT_WEB, ...(draft.web ?? {}) };
  const sub = draft.subagent ?? { enabled: true, max_steps: 30 };
  const mem = draft.memory ?? { enabled: true, workspace: true, max_bytes: 8192 };
  const setWeb = (p: Partial<WebSettings>) => up({ web: { ...web, ...p } });
  const backends = resp.choices.search_backends ?? ["deepseek", "tavily", "brave", "off"];
  const fetches = resp.choices.fetch_backends ?? ["auto", "deepseek", "direct", "off"];
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
  return (
    <>
      <h2 className="mb-1 text-[20px] font-semibold tracking-tight text-fg">工具与记忆</h2>
      <p className="mb-5 text-[13px] text-muted">这些开关决定新对话里模型能用哪些工具；已经开始的对话保持原样（为了不破坏前缀缓存）。</p>
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

      <Card title="子代理" desc="模型可以用 task 工具派生子代理处理独立的子任务（只读探索或继承当前权限），只把最终报告交回来。">
        <Row label="启用子代理">
          <Toggle on={sub.enabled} onChange={(v) => up({ subagent: { ...sub, enabled: v } })} />
        </Row>
        <Row label="默认最大步数" desc="子代理最多连续调用工具的轮数（1～100），模型也可以单次指定">
          <Input className="w-24 text-right" inputMode="numeric" value={sub.max_steps} onChange={(e) => up({ subagent: { ...sub, max_steps: Number(e.target.value) || 0 } })} />
        </Row>
      </Card>

      <Card title="记忆" desc="长期记忆在每次新对话开始时读入；你可以让 ee “记住…”，也可以直接在这里编辑。每条一行，以 - 开头。">
        <Row label="启用记忆" desc="关闭后不读入记忆，也不提供 memory 工具">
          <Toggle on={mem.enabled} onChange={(v) => up({ memory: { ...mem, enabled: v } })} />
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
    </>
  );
}
