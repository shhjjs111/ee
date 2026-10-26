// WebSocket JSON-RPC 客户端：请求/响应按 id 配对，通知分发给订阅者；断线自动重连。

type Pending = { resolve: (v: any) => void; reject: (e: Error) => void; timer: ReturnType<typeof setTimeout> };
export type NotificationHandler = (method: string, params: any) => void;
export type ConnState = "connecting" | "open" | "closed";

export class RpcClient {
  private ws: WebSocket | null = null;
  private seq = 0;
  private pending = new Map<number, Pending>();
  private handlers = new Set<NotificationHandler>();
  private stateHandlers = new Set<(s: ConnState) => void>();
  private queue: string[] = [];
  private retry = 0;
  private closedByUser = false;
  state: ConnState = "closed";

  constructor(private url: string) {}

  connect(): void {
    this.closedByUser = false;
    this.setState("connecting");
    const ws = new WebSocket(this.url);
    this.ws = ws;
    ws.onopen = () => {
      this.retry = 0;
      this.setState("open");
      for (const m of this.queue.splice(0)) ws.send(m);
    };
    ws.onmessage = (ev) => {
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
      this.ws = null;
      this.setState("closed");
      for (const [id, p] of this.pending) {
        clearTimeout(p.timer);
        p.reject(new Error("与后端的连接断开了"));
        this.pending.delete(id);
      }
      if (!this.closedByUser) {
        const delay = Math.min(5000, 300 * 2 ** this.retry++);
        setTimeout(() => this.connect(), delay);
      }
    };
  }

  close(): void {
    this.closedByUser = true;
    this.ws?.close();
  }

  setUrl(url: string): void {
    this.url = url;
  }

  call<T = any>(method: string, params: Record<string, any> = {}, timeoutMs = 60000): Promise<T> {
    const id = ++this.seq;
    const msg = JSON.stringify({ jsonrpc: "2.0", id, method, params });
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`请求超时：${method}`));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(msg);
      else this.queue.push(msg);
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
