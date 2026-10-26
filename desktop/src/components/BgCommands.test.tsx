// @vitest-environment jsdom
import * as TT from "@radix-ui/react-tooltip";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { useApp } from "../lib/store";
import type { BgProc } from "../types";
import { OutputDialog } from "./BgCommands";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

const proc = { id: "b1", command: "npm run dev", status: "exited", exit_code: 0, started: 0, duration: 1, log_path: "x.log" } as unknown as BgProc;

afterEach(() => {
  document.body.innerHTML = "";
});

describe("OutputDialog", () => {
  it("先发出的读取晚返回时，不覆盖较新的输出", async () => {
    const calls: { resolve: (v: unknown) => void }[] = [];
    useApp.setState({
      rpc: { call: () => { const d = deferred<unknown>(); calls.push(d); return d.promise; } } as never,
      bgProcs: { t1: [proc] },
    });
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    await act(async () => {
      root.render(<TT.Provider><OutputDialog threadId="t1" proc={proc} onClose={() => undefined} /></TT.Provider>);
    });
    expect(calls.length).toBe(1);                                  // 打开时读一次
    const refresh = document.querySelector('button[aria-label="刷新"]') as HTMLButtonElement;
    await act(async () => refresh.click());                         // 手动刷新：第二次读
    expect(calls.length).toBe(2);
    await act(async () => calls[1].resolve({ output: "新的输出" }));
    await act(async () => calls[0].resolve({ output: "旧的输出" }));
    expect(document.querySelector("pre")!.textContent).toBe("新的输出");
    await act(async () => root.unmount());
  });
});
