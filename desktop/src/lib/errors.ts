// 失败提示的统一出口：别再 catch(() => undefined) 静默吞掉，至少给一条 toast，并写进日志。
import { useApp } from "./store";

export function errMsg(e: unknown): string {
  if (e instanceof Error) return e.message || e.name;
  if (typeof e === "string") return e;
  try {
    return JSON.stringify(e) ?? String(e);
  } catch {
    return String(e);
  }
}

/** 提示一个失败：`前缀：原因`。返回一个可直接给 .catch 用的函数。 */
export function reportError(prefix: string): (e: unknown) => void {
  return (e: unknown) => {
    const msg = `${prefix}：${errMsg(e)}`;
    try {
      window.ee?.log?.(`[ui] ${msg}`);
    } catch {
      /* 日志写不进去不影响提示 */
    }
    useApp.getState().showToast(msg, "error");
  };
}

/** 兜底：没被处理的 Promise 拒绝 / 运行时错误也要让用户看到，而不是什么都不发生。返回卸载函数。 */
export function installGlobalErrorHandlers(target: Window = window): () => void {
  const onRejection = (e: PromiseRejectionEvent) => {
    e.preventDefault?.();
    reportError("操作失败")(e.reason);
  };
  const onError = (e: ErrorEvent) => {
    if (!e.error && !e.message) return; // 资源加载失败等，无可读信息
    reportError("出错了")(e.error ?? e.message);
  };
  target.addEventListener("unhandledrejection", onRejection as EventListener);
  target.addEventListener("error", onError as EventListener);
  return () => {
    target.removeEventListener("unhandledrejection", onRejection as EventListener);
    target.removeEventListener("error", onError as EventListener);
  };
}
