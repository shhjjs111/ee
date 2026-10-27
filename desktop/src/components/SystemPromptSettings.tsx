// 设置页「系统提示词」分区：当前生效的来源、信任项目文件、模型补丁开关、查看最终系统提示词。
import clsx from "clsx";
import { Eye, FileText, RefreshCw, ShieldCheck, ShieldOff } from "lucide-react";
import React, { useCallback, useEffect, useRef, useState } from "react";
import { useApp } from "../lib/store";
import {
  differsFromFrozen,
  FILE_NAME,
  fileStatus,
  sourceLabel,
  type PromptFileInfo,
  type SysPromptInfo,
  type SysPromptPreview,
} from "../lib/sysprompt";
import { Button, Spinner, Tip, Toggle } from "./ui";

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

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start gap-4 border-b border-line py-2.5 first:pt-0 last:border-b-0 last:pb-0">
      <div className="w-[120px] shrink-0 text-[13px] text-muted">{label}</div>
      <div className="mono min-w-0 flex-1 break-all text-[12.5px] text-fg">{children}</div>
    </div>
  );
}

export function SystemPromptSettings() {
  const s = useApp();
  const threadId = s.activeThreadId;
  const cwd = (threadId ? s.details[threadId]?.cwd : null) ?? s.activeWorkspace ?? null;
  const [info, setInfo] = useState<SysPromptInfo | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [preview, setPreview] = useState<SysPromptPreview | null>(null);
  const gen = useRef(0);

  const reload = useCallback(() => {
    if (!s.rpc) return;
    const tok = ++gen.current;
    setErr(null);
    s.rpc
      .call("sysprompt/info", { cwd, thread_id: threadId })
      .then((r: SysPromptInfo) => {
        if (tok === gen.current) setInfo(r);
      })
      .catch((e: Error) => {
        if (tok === gen.current) setErr(e.message);
      });
  }, [s.rpc, cwd, threadId]);
  useEffect(() => {
    setPreview(null);
    reload();
  }, [reload]);

  const act = async (key: string, method: string, params: Record<string, unknown>) => {
    setBusy(key);
    setErr(null);
    try {
      await s.rpc!.call(method, params);
      setPreview(null);
      reload();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const showPreview = async () => {
    setBusy("preview");
    setErr(null);
    try {
      setPreview(await s.rpc!.call("sysprompt/preview", { cwd, thread_id: threadId }));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const cur = info?.current;
  const projectFiles = cur?.files.filter((f) => f.scope === "project") ?? [];
  const userFiles = cur?.files.filter((f) => f.scope === "user") ?? [];

  const fileRow = (f: PromptFileInfo) => {
    const st = fileStatus(f);
    return (
      <div key={f.path} className="flex items-center gap-2 rounded-xl border border-line px-3 py-2.5">
        <FileText size={14} className="shrink-0 text-muted" />
        <div className="min-w-0 flex-1">
          <div className="text-[12.5px] text-fg">{FILE_NAME[f.kind]}</div>
          <div className="mono truncate text-[11.5px] text-faint" title={f.path}>
            {f.path}
          </div>
        </div>
        <span className={clsx("shrink-0 text-[12px]", st.tone === "ok" ? "text-fg" : st.tone === "warn" ? "text-warn" : "text-faint")}>{st.text}</span>
        {f.scope === "project" && !f.error && f.allow_id && !f.trusted && (
          <Button variant="outline" size="sm" className="h-7" disabled={!!busy} onClick={() => act(`trust:${f.allow_id}`, "sysprompt/trust", { cwd, allow_id: f.allow_id })}>
            {busy === `trust:${f.allow_id}` ? <Spinner size={12} /> : <ShieldCheck size={12} />} 信任
          </Button>
        )}
        {f.scope === "project" && f.trusted && (
          <Button variant="ghost" size="sm" className="h-7" disabled={!!busy} onClick={() => act(`untrust:${f.allow_id}`, "sysprompt/untrust", { allow_id: f.allow_id })}>
            <ShieldOff size={12} /> 撤销信任
          </Button>
        )}
      </div>
    );
  };

  return (
    <>
      <h2 className="mb-1 text-[20px] font-semibold tracking-tight text-fg">系统提示词</h2>
      <p className="mb-5 text-[13px] text-muted">
        用 SYSTEM.md 替换内置的基础提示词（按启用工具生成的工具使用规则仍会自动追加），或用 APPEND_SYSTEM.md 在后面追加要求（例如写作业专用的规则）。系统提示词在对话开始时固定下来，改了文件从新对话生效。
      </p>

      {err && <div className="mb-4 rounded-lg border border-line bg-soft/40 px-3 py-2 text-[12.5px] text-danger">{err}</div>}

      <Card
        title="当前生效"
        desc={info?.frozen ? "新对话会用下面这些；当前对话用的是它开始时固定的版本" : "新对话会用下面这些"}
        action={
          <Tip label="刷新">
            <Button size="icon-sm" variant="ghost" onClick={reload}>
              <RefreshCw size={13} />
            </Button>
          </Tip>
        }
      >
        {!cur ? (
          <div className="flex justify-center py-2">{err ? <span className="text-[12.5px] text-faint">读取失败</span> : <Spinner />}</div>
        ) : (
          <>
            <Row label="基础提示词">{sourceLabel(cur, "system")}</Row>
            <Row label="模型补丁">{cur.model_patch ? "按模型家族追加" : "已关闭"}</Row>
            <Row label="追加提示词">{sourceLabel(cur, "append")}</Row>
            {info && differsFromFrozen(info) && (
              <div className="mt-3 text-[12px] text-warn">当前对话开始时用的来源和现在不同，新建对话后生效。</div>
            )}
            {cur.pending.length > 0 && (
              <div className="mt-3 text-[12px] text-warn">项目里有还没信任的系统提示词文件，它们现在不生效。确认内容可信后在下面点「信任」。</div>
            )}
            <div className="mt-4 flex items-center gap-2">
              <Button variant="outline" size="sm" className="h-8" disabled={busy === "preview"} onClick={showPreview}>
                {busy === "preview" ? <Spinner size={12} /> : <Eye size={13} />} 查看最终系统提示词
              </Button>
              {preview && (
                <span className="text-[12px] text-faint">
                  {preview.frozen ? "当前对话实际发给模型的版本" : "新对话会用的版本"} · {preview.model} · {preview.chars.toLocaleString()} 字
                </span>
              )}
            </div>
            {preview && (
              <pre className="mono mt-3 max-h-[420px] overflow-auto whitespace-pre-wrap break-words rounded-lg border border-line bg-bg px-3 py-2 text-[11.5px] text-fg">
                {preview.text}
              </pre>
            )}
          </>
        )}
      </Card>

      <Card title="模型补丁" desc="内置的按模型家族的小段提示（例如 DeepSeek 专用的工具调用提醒），追加在基础提示词后面。">
        <div className="flex items-center gap-6">
          <div className="min-w-0 flex-1">
            <div className="text-[13.5px] text-fg">追加模型补丁</div>
            <div className="mt-0.5 text-[12px] text-faint">自己写了完整的 SYSTEM.md、不想再带内置补丁时关掉</div>
          </div>
          <Toggle on={cur?.model_patch ?? true} onChange={(v) => act("patch", "sysprompt/set", { model_patch: v })} />
        </div>
      </Card>

      <Card
        title="项目文件"
        desc={
          cwd
            ? "来自项目的 .ee/SYSTEM.md 和 .ee/APPEND_SYSTEM.md。仓库里的文件可能被别人改过，默认不生效，确认可信后再信任；内容一改需要重新信任。"
            : "先在左栏打开一个项目"
        }
      >
        {projectFiles.length ? <div className="space-y-2">{projectFiles.map(fileRow)}</div> : <div className="text-[12.5px] text-faint">当前项目里没有系统提示词文件。</div>}
      </Card>

      <Card title="我的文件" desc={info ? `放在 ${info.user_dir} 下，所有项目都生效（项目里有已信任的同名文件时以项目为准）。` : undefined}>
        {userFiles.length ? <div className="space-y-2">{userFiles.map(fileRow)}</div> : <div className="text-[12.5px] text-faint">还没有。新建 SYSTEM.md 或 APPEND_SYSTEM.md 即可。</div>}
      </Card>

      <Card title="说明">
        <ul className="list-disc space-y-1.5 pl-5 text-[12.5px] text-muted">
          <li>加载顺序：项目 .ee/SYSTEM.md（已信任）→ 用户目录 SYSTEM.md → 内置基础提示词；APPEND_SYSTEM.md 同样二选一。</li>
          <li>最终顺序：基础提示词 → 工具使用规则（随启用的工具变化，SYSTEM.md 不替换它）→ 模型补丁 → 追加提示词 → 回答风格。AGENTS.md、记忆、技能清单照常单独加载，不受影响。</li>
          <li>文件用 UTF-8，单个不超过 {Math.round((info?.max_bytes ?? 65536) / 1024)} KiB；空文件会被忽略。</li>
          <li>
            <b className="text-fg">系统提示词不是安全边界</b>：权限模式、审批、拒绝规则和危险命令确认都在程序里执行，提示词里写什么都绕不过去。
          </li>
        </ul>
      </Card>
    </>
  );
}
