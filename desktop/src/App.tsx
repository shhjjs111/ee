import * as TT from "@radix-ui/react-tooltip";
import clsx from "clsx";
import { AlertTriangle, RotateCw } from "lucide-react";
import { useEffect, useState } from "react";
import { Conversation } from "./components/Conversation";
import { RightPanel } from "./components/RightPanel";
import { Dialogs } from "./components/ReviewDialogs";
import { SettingsPage } from "./components/SettingsPage";
import { AutomationsPage } from "./components/AutomationsPage";
import { useAutomations } from "./lib/automationStore";
import { Sidebar } from "./components/Sidebar";
import { Button, Spinner } from "./components/ui";
import { RpcClient, rpcLog } from "./lib/rpc";
import { applyTheme, useApp } from "./lib/store";

let autoOff: (() => void) | null = null;

type Boot = { state: "loading" } | { state: "error"; message: string } | { state: "ready" };

async function backendUrl(restart = false): Promise<string> {
  const qs = new URLSearchParams(location.search);
  if (qs.get("port")) return `ws://127.0.0.1:${qs.get("port")}/?token=${encodeURIComponent(qs.get("token") ?? "")}`;
  if (!window.ee) throw new Error("没有找到 Electron 桥接；在浏览器里调试请用 ?port=端口&token=口令 打开。");
  const r = restart ? await window.ee.restartBackend() : await window.ee.getBackend();
  if (!r.ok) throw new Error(r.error || "后端启动失败");
  return `ws://127.0.0.1:${r.port}/?token=${encodeURIComponent(r.token!)}`;
}

export default function App() {
  const [boot, setBoot] = useState<Boot>({ state: "loading" });
  const view = useApp((s) => s.view);
  const toast = useApp((s) => s.toast);
  const theme = useApp((s) => s.theme);
  const conn = useApp((s) => s.conn);

  const start = async (restart = false) => {
    setBoot({ state: "loading" });
    try {
      const url = await backendUrl(restart);
      const old = useApp.getState().rpc;
      if (old) old.close();
      const rpc = new RpcClient(url);
      await useApp.getState().init(rpc);
      autoOff?.();
      autoOff = useAutomations.getState().init(rpc); // 自动化（D4-10）
      setBoot({ state: "ready" });
    } catch (e) {
      setBoot({ state: "error", message: (e as Error).message });
    }
  };

  useEffect(() => {
    const plat = window.ee?.platform;
    document.documentElement.style.setProperty("--overlay-w", window.ee && plat !== "darwin" ? "138px" : "0px");
    document.documentElement.style.setProperty("--traffic-w", plat === "darwin" ? "80px" : "16px");
    applyTheme(theme);
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onMq = () => applyTheme(useApp.getState().theme);
    mq.addEventListener("change", onMq);
    start();
    // 内核意外退出 / WebSocket 连不回来：自动重启内核（2 分钟内最多 3 次，超过就显示错误页）
    const restarts: number[] = [];
    let restarting = false;
    const autoRestart = async (why: string) => {
      if (restarting) return;
      const now = Date.now();
      while (restarts.length && now - restarts[0] > 120000) restarts.shift();
      if (restarts.length >= 3) {
        rpcLog(`自动重启已达上限：${why}`);
        setBoot({ state: "error", message: `${why}\n已自动重启 3 次仍失败，请查看日志或手动重启。` });
        return;
      }
      restarts.push(now);
      restarting = true;
      rpcLog(`自动重启内核：${why}`);
      useApp.getState().showToast(`${why.split("\n")[0]}，正在自动重启内核…`, "error");
      try {
        await start(true);
      } finally {
        restarting = false;
      }
    };
    const off = window.ee?.onBackendExit((info) => {
      autoRestart(`内核意外退出（退出码 ${info.code}）\n${info.tail}`);
    });
    const offConn = useApp.subscribe((st, prev) => {
      if (st.conn === "failed" && prev.conn !== "failed") autoRestart("与内核的连接断开且重连失败");
    });
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "n") {
        e.preventDefault();
        useApp.getState().newThread();
      } else if ((e.ctrlKey || e.metaKey) && e.key === ",") {
        e.preventDefault();
        useApp.getState().set({ view: "settings" });
      }
    };
    window.addEventListener("keydown", onKey);
    // D4-5：点通知打开对应会话；托盘菜单动作；托盘提示里显示进行中的会话数
    const offOpen = window.ee?.onOpenThread?.((id) => {
      useApp.getState().openThread(id).catch(() => undefined);
    });
    const offTray = window.ee?.onTrayAction?.((a) => {
      if (a === "new") useApp.getState().newThread();
      else if (a === "settings") useApp.getState().openSettings("general");
    });
    let lastTray = "";
    const offStatus = useApp.subscribe((st) => {
      const running = st.threads.filter((t) => t.status === "running" || t.status === "compacting").length;
      const waiting = st.threads.filter((t) => t.status === "waiting_approval").length;
      const key = `${running}/${waiting}`;
      if (key !== lastTray) {
        lastTray = key;
        window.ee?.setTrayStatus?.({ running, waiting });
      }
    });
    return () => {
      offOpen?.();
      offTray?.();
      offStatus();
      mq.removeEventListener("change", onMq);
      off?.();
      offConn();
      window.removeEventListener("keydown", onKey);
    };
  }, []);

  if (boot.state !== "ready") {
    return (
      <div className="drag flex h-full items-center justify-center bg-bg">
        {boot.state === "loading" ? (
          <div className="flex flex-col items-center gap-3 text-muted">
            <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-fg text-[17px] font-semibold text-bg">ee</div>
            <div className="flex items-center gap-2 text-[13px]">
              <Spinner size={13} /> 正在启动内核…
            </div>
          </div>
        ) : (
          <div className="no-drag max-w-[560px] rounded-2xl border border-line bg-elev p-6 shadow-[var(--shadow)]">
            <div className="mb-2 flex items-center gap-2 text-[15px] font-semibold text-fg">
              <AlertTriangle size={17} className="text-danger" /> 无法连接 ee 内核
            </div>
            <pre className="mono mb-4 max-h-[300px] overflow-auto whitespace-pre-wrap rounded-lg bg-code p-3 text-[12px] text-muted">{boot.message}</pre>
            <div className="text-[12.5px] text-faint">
              开发模式需要 Python 3.11+ 并已执行 <code className="mono">pip install -e ee</code>；可用环境变量 EE_PYTHON 指定 python 路径。
            </div>
            <div className="mt-4 flex justify-end">
              <Button variant="primary" onClick={() => start(true)}>
                <RotateCw size={13} /> 重新启动内核
              </Button>
            </div>
          </div>
        )}
      </div>
    );
  }

  return (
    <TT.Provider>
      {(conn === "closed" || conn === "connecting") && (
        <div className="fixed left-1/2 top-2 z-50 -translate-x-1/2 rounded-full border border-warn/40 bg-elev px-3 py-1 text-[12px] text-warn shadow-[var(--shadow)]">
          与内核的连接断开，正在重连…
        </div>
      )}
      <div className="flex h-full overflow-hidden">
        <Sidebar />
        {view === "settings" ? <SettingsPage /> : view === "automations" ? <AutomationsPage /> : <Conversation />}
        <RightPanel />
      </div>
      <Dialogs />
      {toast && (
        <div
          className={clsx(
            "fixed bottom-6 left-1/2 z-50 -translate-x-1/2 rounded-xl border px-4 py-2 text-[13px] shadow-[var(--shadow)] fadein",
            toast.kind === "error" ? "border-danger/40 bg-elev text-danger" : "border-line bg-elev text-fg",
          )}
        >
          {toast.text}
        </div>
      )}
    </TT.Provider>
  );
}
