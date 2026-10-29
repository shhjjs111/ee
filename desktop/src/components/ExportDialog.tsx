// 会话导出入口（T10-a U3）：把后端的 thread/export 接到界面上。
// 外壳统一走 ui/DialogFrame（Wave 5），业务逻辑与 RPC 一字未改。
import { Download, FileJson, FileText } from "lucide-react";
import { useEffect, useState } from "react";
import { useApp } from "../lib/store";
import { Button, Spinner } from "./ui";
import { DialogFrame } from "./ui/DialogFrame";

export function ExportDialog({ threadId: fixed }: { threadId?: string } = {}) {
  // 两个 hook 都无条件调用：`fixed ?? useApp(...)` 会让 useApp 只在 fixed 为空时执行，
  // 违反 Hooks 规则（hook 不能条件调用），表现是选择器拿不到状态、组件静默渲染 null。
  const fromStore = useApp((s) => s.exportDialog);
  const tid = fixed ?? fromStore;
  const rpc = useApp((s) => s.rpc);
  const s = useApp();
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [preview, setPreview] = useState<{ format: string; content: string } | null>(null);

  useEffect(() => {
    setErr(null);
    setPreview(null);
  }, [tid]);

  if (!tid) return null;
  const close = () => s.set({ exportDialog: null });

  const doExport = async (format: "markdown" | "json") => {
    setBusy(format);
    setErr(null);
    try {
      // 不传 path：只取内容做预览，绝不偷偷写盘。
      // 真要保存由用户另外确认（Electron 保存对话框未接，见任务书未做项）。
      const r = await rpc!.call("thread/export", { thread_id: tid, format }, 60000);
      setPreview({ format, content: r.content });
      s.showToast(`已生成 ${format === "json" ? "JSON" : "Markdown"}（${r.content.length} 字符）`);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const copy = async () => {
    if (!preview) return;
    try {
      await navigator.clipboard.writeText(preview.content);
      s.showToast("已复制到剪贴板");
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  return (
    <DialogFrame title="导出会话" icon={<Download size={15} />} onClose={close} width="sm">
      <div className="mb-3 text-[12px] leading-relaxed text-muted">
        导出当前会话的完整内容：每条消息、每次工具调用的名称与输出。
        JSON 里工具参数是结构化对象（不是字符串），便于程序处理。
      </div>

      {err && (
        <div className="mb-2 whitespace-pre-wrap rounded-lg bg-del-bg px-3 py-2 text-[12.5px] text-danger">
          {err}
        </div>
      )}

      <div className="mb-3 flex items-center gap-2">
        <Button size="sm" variant="primary" disabled={!!busy} onClick={() => doExport("markdown")}>
          {busy === "markdown" ? <Spinner size={12} /> : <FileText size={13} />} Markdown
        </Button>
        <Button size="sm" disabled={!!busy} onClick={() => doExport("json")}>
          {busy === "json" ? <Spinner size={12} /> : <FileJson size={13} />} JSON
        </Button>
        {preview && (
          <Button size="sm" onClick={copy}>
            复制
          </Button>
        )}
      </div>

      {preview && (
        <div className="min-h-0 flex-1 overflow-auto rounded-lg border border-line bg-code p-3">
          <pre className="whitespace-pre-wrap break-words text-[12px] leading-relaxed text-fg">
            {preview.content.slice(0, 8000)}
            {preview.content.length > 8000 ? "\n…（预览只显示前 8000 字符）" : ""}
          </pre>
        </div>
      )}
    </DialogFrame>
  );
}
