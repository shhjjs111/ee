// 会话内 /loop（D5-3）的前端状态：订阅 loop/updated、loop/removed；连上后端时拉一次（后端重启后自然清空）。
import { create } from "zustand";
import { reportError } from "./errors";
import type { RpcClient } from "./rpc";
import type { SessionLoop } from "./loop";

type State = { loops: SessionLoop[] };
type Actions = {
  init(rpc: RpcClient): () => void;
  refresh(rpc: RpcClient): Promise<void>;
  upsert(lp: SessionLoop): void;
  drop(ids: string[]): void;
};

const loopErr = (e: unknown) => reportError("加载会话内循环失败")(e);
const byId = (a: SessionLoop, b: SessionLoop) => Number(a.id) - Number(b.id);

export const useLoops = create<State & Actions>((set, get) => ({
  loops: [],
  upsert: (lp) => set((s) => ({ loops: [...s.loops.filter((x) => x.id !== lp.id), lp].sort(byId) })),
  drop: (ids) => set((s) => ({ loops: s.loops.filter((x) => !ids.includes(x.id)) })),
  async refresh(rpc) {
    const r = await rpc.call("loop/list", {});
    set({ loops: [...(r.loops ?? [])].sort(byId) });
  },
  init(rpc) {
    const offN = rpc.onNotification((method, p: any) => {
      if (method === "loop/updated" && p?.loop) get().upsert(p.loop as SessionLoop);
      else if (method === "loop/removed") get().drop((p?.ids ?? []).map(String));
    });
    const offS = rpc.onState((st) => {
      if (st === "open") get().refresh(rpc).catch(loopErr);
    });
    get().refresh(rpc).catch(loopErr);
    return () => {
      offN();
      offS();
    };
  },
}));
