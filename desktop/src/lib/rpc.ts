// WebSocket JSON-RPC 客户端：请求/响应按 id 配对，通知分发给订阅者；断线自动重连。
// 每个请求都有超时（默认 20 秒），失败会写进 logs/backend.log；连续重连失败后进入 "failed"，由界面重启内核。

type Pending = { resolve: (v: any) => void; reject: (e: Error) => void; timer: ReturnType<typeof setTimeout> };
export type NotificationHandler = (method: string, params: any) => void;
export type ConnState = "connecting" | "open" | "closed" | "failed";

export const DEFAULT_RPC_TIMEOUT = 20000;
const MAX_RECONNECTS = 6; // 约 10 秒连不上就放弃，交给界面重启内核

export function rpcLog(line: string): void {
  try {
    window.ee?.log?.(line);
  } catch {
    /* ignore */
  }
}

export class RpcClient {
  private ws: WebSocket | null = null;
  private seq = 0;
  private pending = new Map<number, Pending>();
  private handlers = new Set<NotificationHandler>();
  private stateHandlers = new Set<(s: ConnState) => void>();
  private queue: { id: number; msg: string }[] = []; // 连上之前的请求；超时 / 被拒绝时从这里删掉，不会迟到补发
  private retry = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private closedByUser = false;
  state: ConnState = "closed";

  constructor(private url: string) {}

  connect(): void {
    this.closedByUser = false;
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    const prev = this.ws;
    this.ws = null;
    if (prev) {
      // 先断开旧连接的回调：它迟到的 close 不能拒掉新连接上的请求、也不能再安排一次重连
      prev.onopen = prev.onmessage = prev.onclose = null;
      try {
        prev.close();
      } catch {
        /* ignore */
      }
    }
    this.setState("connecting");
    const ws = new WebSocket(this.url);
    this.ws = ws;
    ws.onopen = () => {
      if (this.ws !== ws) return;
      this.retry = 0;
      this.setState("open");
      for (const q of this.queue.splice(0)) ws.send(q.msg);
    };
    ws.onmessage = (ev) => {
      if (this.ws !== ws) return;
      let msg: any;
      try {
        msg = JSON.parse(ev.data as string);
      } catch {
        return;
      }
      if (msg.id !== undefined && msg.id !== null && this.pending.has(msg.id)) {
        const p = this.pending.get(msg.id)!;
        this.pending.delete(msg.id);
        clearTimeout(p.timer);
        if (msg.error) p.reject(new Error(msg.error.message || "请求失败"));
        else p.resolve(msg.result);
      } else if (msg.method) {
        for (const h of this.handlers) h(msg.method, msg.params);
      }
    };
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.setState("closed");
      for (const [id, p] of this.pending) {
        clearTimeout(p.timer);
        p.reject(new Error("与后端的连接断开了"));
        this.pending.delete(id);
      }
      this.queue = [];
      if (!this.closedByUser) {
        if (this.retry >= MAX_RECONNECTS) {
          rpcLog(`WebSocket 连续 ${this.retry} 次重连失败，放弃（等待界面重启内核）`);
          this.setState("failed");
          return;
        }
        const delay = Math.min(4000, 300 * 2 ** this.retry++);
        if (this.retry === 1) rpcLog("WebSocket 断开，正在重连");
        this.retryTimer = setTimeout(() => {
          this.retryTimer = null;
          if (!this.closedByUser) this.connect();
        }, delay);
      }
    };
  }

  close(): void {
    this.closedByUser = true;
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    this.ws?.close();
  }

  setUrl(url: string): void {
    this.url = url;
  }

  call<T = any>(method: string, params: Record<string, any> = {}, timeoutMs = DEFAULT_RPC_TIMEOUT): Promise<T> {
    const id = ++this.seq;
    const msg = JSON.stringify({ jsonrpc: "2.0", id, method, params });
    const t0 = Date.now();
    return new Promise<T>((resolve, reject) => {
      const fail = (e: Error) => {
        rpcLog(`RPC ${method} 失败（${Date.now() - t0}ms）：${e.message}`);
        reject(e);
      };
      const timer = setTimeout(() => {
        this.pending.delete(id);
        this.queue = this.queue.filter((q) => q.id !== id);
        fail(new Error(`内核 ${Math.round(timeoutMs / 1000)} 秒没有响应（${method}）。可在 设置 → 关于 打开日志目录查看 ee-backend.log。`));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject: fail, timer });
      if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(msg);
      else this.queue.push({ id, msg });
    });
  }

  onNotification(h: NotificationHandler): () => void {
    this.handlers.add(h);
    return () => this.handlers.delete(h);
  }

  onState(h: (s: ConnState) => void): () => void {
    this.stateHandlers.add(h);
    return () => this.stateHandlers.delete(h);
  }

  private setState(s: ConnState) {
    this.state = s;
    for (const h of this.stateHandlers) h(s);
  }
}
