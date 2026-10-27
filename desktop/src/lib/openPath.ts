// 渲染进程统一入口：调用主进程“用默认程序打开”，把返回的提示（被拦截、改为定位、打开失败）用 toast 显示出来。
import { useApp } from "./store";

export async function openPath(p: string): Promise<void> {
  const toast = useApp.getState().showToast;
  if (!window.ee?.openPath) return;
  try {
    const msg = await window.ee.openPath(p);
    if (msg) toast(msg, msg.includes("已在资源管理器中定位") ? "info" : "error");
  } catch (e) {
    toast(`打开失败：${(e as Error).message}`, "error");
  }
}
