// e2e 里直接调后端 RPC：从页面拿 preload 暴露的后端信息，自己连一条 WS。
//
// 为什么绕开 store：spec 需要造前置状态（打开项目、改设置），而 store 的对应动作
// 都绑在界面上。走真实 WS 仍然覆盖 preload → 后端整条链路，只跳过点击。

import type { Page } from "@playwright/test";

export function pageRpc<T = any>(win: Page, method: string, params: Record<string, unknown> = {}, timeout = 20_000): Promise<T> {
  return win.evaluate(
    async ({ method, params, timeout }) => {
      const info = await (window as any).ee.getBackend();
      if (!info?.ok) throw new Error("后端不可用");
      const ws = new WebSocket(`ws://127.0.0.1:${info.port}/?token=${encodeURIComponent(info.token)}`);
      await new Promise<void>((r, j) => {
        ws.readyState === WebSocket.OPEN ? r() : ws.addEventListener("open", () => r(), { once: true });
        ws.addEventListener("error", () => j(new Error("ws error")), { once: true });
      });
      const result = await new Promise<any>((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error(`${method} 超时`)), timeout);
        ws.addEventListener("message", (ev) => {
          try {
            const m = JSON.parse(ev.data as string);
            if (!m.id) return;
            clearTimeout(timer);
            m.error ? reject(new Error(m.error.message)) : resolve(m.result);
          } catch {
            /* 非 JSON 推送忽略 */
          }
        });
        ws.send(JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }));
      });
      ws.close();
      return result;
    },
    { method, params, timeout },
  ) as Promise<T>;
}
