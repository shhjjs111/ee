// 文件交接清单（T10-e 界面侧）：搬东西之前先看清要搬什么。
// 只做「看清」——复制/切换/删除是不可逆动作，留给用户确认后另行执行。
import { AlertTriangle, CheckCircle2, FileWarning, PackageOpen } from "lucide-react";
import { useEffect, useState } from "react";
import { useApp } from "../lib/store";
import type { HandoffEntry, HandoffManifest } from "../types";
import { Spinner } from "./ui";
import { DialogFrame } from "./ui/DialogFrame";

const CAT_LABEL: Record<string, string> = {
  staged: "已暂存",
  unstaged: "未暂存",
  untracked: "未跟踪",
  ignored: "被忽略",
  tracked: "已跟踪",
};

export function HandoffDialog() {
  const path = useApp((s) => s.handoffDialog);
  const rpc = useApp((s) => s.rpc);
  const s = useApp();
  const [data, setData] = useState<HandoffManifest | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setData(null);
    setErr(null);
    if (!path) return;
    let alive = true;
    setBusy(true);
    rpc!
      .call("handoff/manifest", { path }, 120000)
      .then((r) => {
        if (alive) setData(r as HandoffManifest);
      })
      .catch((e) => {
        if (alive) setErr((e as Error).message);
      })
      .finally(() => {
        if (alive) setBusy(false);
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path]);

  if (!path) return null;
  const close = () => s.set({ handoffDialog: null });

  return (
    <DialogFrame title="迁移清单" icon={<PackageOpen size={15} />} onClose={close} width="md">
          <div className="mb-3 text-[12px] leading-relaxed text-muted">
            把文件搬到别处之前，先看清要搬什么。每条都带内容哈希，
            复制后可以逐字节核对；被忽略的文件也会列出来（默认不搬，但你能看见）。
            <span className="text-faint">这里只生成清单，不动任何文件。</span>
          </div>

          {err && (
            <div className="mb-2 whitespace-pre-wrap rounded-lg bg-del-bg px-3 py-2 text-[12.5px] text-danger">
              {err}
            </div>
          )}

          {busy && <Spinner />}

          {data && (
            <>
              <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg bg-soft px-3 py-2 text-[12px] text-muted">
                <span>
                  共 <span className="text-fg">{data.summary.total}</span> 条，选中{" "}
                  <span className="text-fg">{data.summary.selected}</span> 条
                </span>
                <span>{data.summary.bytes} 字节</span>
                {data.summary.binary > 0 && (
                  <span className="flex items-center gap-1 text-warn">
                    <FileWarning size={12} /> {data.summary.binary} 个二进制
                  </span>
                )}
                <span className="text-faint">忽略文件策略：{data.ignored_policy === "include" ? "一并迁移" : "不迁移"}</span>
              </div>
              <div className="min-h-0 flex-1 overflow-y-auto rounded-lg border border-line">
                {data.entries.map((e: HandoffEntry) => (
                  <div
                    key={e.path}
                    data-category={e.category}
                    data-selected={e.selected ? "true" : "false"}
                    className="flex items-center gap-2 border-b border-line px-3 py-1.5 text-[12px] last:border-0"
                  >
                    <span className="w-4 shrink-0">
                      {e.selected ? (
                        <CheckCircle2 size={13} className="text-ok" />
                      ) : (
                        <span className="block h-3 w-3 rounded-full border border-line" />
                      )}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-fg" title={e.path}>
                      {e.path}
                    </span>
                    {e.binary && <span className="shrink-0 text-[11px] text-warn">bin</span>}
                    <span className="shrink-0 text-[11px] text-faint">{CAT_LABEL[e.category] ?? e.category}</span>
                    <span className="mono w-8 shrink-0 text-right text-[11px] text-faint">{e.size}B</span>
                    <span className="mono shrink-0 text-[10.5px] text-faint" title={e.sha256}>
                      {e.sha256.slice(0, 12)}
                    </span>
                  </div>
                ))}
              </div>
              {data.summary.total === 0 && (
                <div className="py-6 text-center text-[12.5px] text-faint">工作区是干净的，没有要搬的东西。</div>
              )}
            </>
          )}

          {!busy && !data && !err && (
            <div className="flex items-center gap-2 py-4 text-[12.5px] text-faint">
              <AlertTriangle size={13} /> 没有拿到清单
            </div>
          )}
    </DialogFrame>
  );
}
