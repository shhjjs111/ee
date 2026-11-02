// 「关于」里的更新卡片：显示当前状态 + 手动检查 + 下载完后的重启安装。
//
// 为什么单独一个组件：SettingsPage 已经 678 行，而更新有自己的一整套状态
// （订阅 IPC、进度、错误分支），塞进去只会让那个文件更难读。这里只负责渲染，
// 状态全部来自主进程（electron/updater.ts），自己不留业务状态。
import { useEffect, useState } from "react";
import type { UpdateState } from "../types";
import { Button, Spinner } from "./ui";

const EMPTY: UpdateState = { phase: "idle", current: "" };

export function UpdateCard() {
  const bridge = window.ee?.update;
  const [st, setSt] = useState<UpdateState>(EMPTY);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!bridge) return;
    let alive = true;
    // 先拉快照：窗口可能是更新下载完之后才打开的，只靠事件会漏掉
    bridge.state().then((s) => alive && setSt(s)).catch(() => undefined);
    const off = bridge.onState((s) => alive && setSt(s));
    return () => {
      alive = false;
      off();
    };
  }, [bridge]);

  if (!bridge) return null; // 浏览器调试 / 老版本壳：没有更新能力就不显示

  const checking = st.phase === "checking" || busy;
  const downloading = st.phase === "downloading";

  async function check() {
    if (!bridge) return;
    setBusy(true);
    try {
      setSt(await bridge.check());
    } finally {
      setBusy(false);
    }
  }

  async function install() {
    if (!bridge) return;
    // 主进程会连着后端一起退出再重启，这里不需要额外处理
    await bridge.install();
  }

  return (
    <section className="mb-6 rounded-2xl border border-line bg-elev">
      <div className="flex items-start gap-3 border-b border-line px-5 py-3.5">
        <div className="min-w-0 flex-1">
          <h3 className="text-[14px] font-semibold text-fg">更新</h3>
          <p className="mt-0.5 text-[12.5px] text-muted">
            从 GitHub Releases 检查新版本。下载在后台进行，装不装、什么时候装由你决定。
          </p>
        </div>
      </div>
      <div className="px-5 py-4">
        <div className="flex items-center gap-6 border-b border-line py-3 first:pt-0 last:border-b-0 last:pb-0">
          <div className="min-w-0 flex-1">
            <div className="text-[13.5px] text-fg">当前版本</div>
            <div className="mt-0.5 text-[12px] text-faint">
              {st.phase === "idle" && "还没有检查过更新"}
              {st.phase === "checking" && "正在检查…"}
              {st.phase === "up-to-date" && (st.message ?? "已是最新版本")}
              {st.phase === "error" && <span className="text-danger">检查失败：{st.message}</span>}
              {(st.phase === "available" || downloading) && (st.message ?? `发现新版本 ${st.version}`)}
              {st.phase === "downloaded" && (st.message ?? `新版本 ${st.version} 已就绪`)}
            </div>
            {downloading && (
              // 进度条：用固定宽度 + 内层百分比，避免宽度随数字抖动
              <div className="mt-2 h-1 w-[220px] overflow-hidden rounded-full bg-line">
                <div
                  className="h-full rounded-full bg-accent transition-[width] duration-200"
                  style={{ width: `${st.percent ?? 0}%` }}
                />
              </div>
            )}
          </div>
          <div className="shrink-0">
            {st.phase === "downloaded" ? (
              <Button variant="primary" size="sm" onClick={() => void install()}>
                重启并安装
              </Button>
            ) : downloading ? (
              <span className="mono text-[12px] text-muted">{st.percent ?? 0}%</span>
            ) : (
              <Button variant="outline" size="sm" onClick={() => void check()} disabled={checking}>
                {checking ? <Spinner size={12} /> : null} 检查更新
              </Button>
            )}
          </div>
        </div>
        <div className="pt-3 text-[12px] text-faint">
          也可以
          <button className="ml-1 text-accent hover:underline" onClick={() => void bridge.openRelease()}>
            打开发布页
          </button>
          手动下载。装新版不会动你的配置和会话（都在 ~/.ee）。
        </div>
      </div>
    </section>
  );
}
