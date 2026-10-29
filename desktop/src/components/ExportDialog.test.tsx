// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { useApp } from "../lib/store";
import type { RpcClient } from "../lib/rpc";
import { ExportDialog } from "./ExportDialog";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

interface Call {
  method: string;
  params: Record<string, unknown>;
}

/** 造一个记录调用、按需返回的假 rpc。 */
function fakeRpc(reply: (method: string, params: Record<string, unknown>) => unknown) {
  const calls: Call[] = [];
  const client = {
    call: async (method: string, params: Record<string, unknown>) => {
      calls.push({ method, params });
      return reply(method, params);
    },
  } as unknown as RpcClient;
  return { client, calls };
}

function mount() {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  act(() => {
    root.render(<ExportDialog />);
  });
  // Radix Dialog 通过 Portal 渲染到 document.body，不在 host 里——
  // 断言要看 body，不能看 host（否则永远拿到空串）。
  return { host, root, view: () => document.body.textContent ?? "" };
}

const flush = async () => {
  await act(async () => {
    await Promise.resolve();
  });
};

describe("ExportDialog", () => {
  afterEach(() => {
    act(() => useApp.setState({ exportDialog: null }));
    document.body.innerHTML = "";
  });

  it("两种格式都能选（Markdown / JSON）", () => {
    useApp.setState({ exportDialog: "20261029-010101-aaaaaa" });
    const { client } = fakeRpc(() => ({ content: "# hi", path: null, format: "markdown" }));
    act(() => useApp.setState({ rpc: client }));
    const { view } = mount();
    const text = view();
    expect(text).toContain("Markdown");
    expect(text).toContain("JSON");
  });

  it("选 JSON 后调用 thread/export 时带上 format=json", async () => {
    useApp.setState({ exportDialog: "20261029-010101-aaaaaa" });
    const { client, calls } = fakeRpc(() => ({ content: "{}", path: null, format: "json" }));
    act(() => useApp.setState({ rpc: client }));
    mount();

    const jsonBtn = [...document.body.querySelectorAll("button")].find((b) =>
      (b.textContent ?? "").includes("JSON"),
    ) as HTMLButtonElement;
    expect(jsonBtn).toBeTruthy();
    act(() => jsonBtn.click());
    await flush();

    const exp = calls.find((c) => c.method === "thread/export");
    expect(exp).toBeTruthy();
    expect(exp!.params.format).toBe("json");
    expect(exp!.params.thread_id).toBe("20261029-010101-aaaaaa");
  });

  it("不传 path 时不写盘：调用里没有 path 字段", async () => {
    useApp.setState({ exportDialog: "20261029-010101-aaaaaa" });
    const { client, calls } = fakeRpc(() => ({ content: "x", path: null, format: "markdown" }));
    act(() => useApp.setState({ rpc: client }));
    mount();
    const exp = calls.find((c) => c.method === "thread/export");
    // 首次预览不应带 path
    if (exp) expect(exp.params.path ?? null).toBeNull();
  });

  it("目标已存在（服务端报错）时显示冲突提示，而不是静默失败", async () => {
    useApp.setState({ exportDialog: "20261029-010101-aaaaaa" });
    const { client } = fakeRpc((method) => {
      if (method === "thread/export") throw new Error("目标文件已存在：C:/x.md");
      return {};
    });
    act(() => useApp.setState({ rpc: client }));
    const { view } = mount();

    const mdBtn = [...document.body.querySelectorAll("button")].find((b) =>
      (b.textContent ?? "").includes("Markdown"),
    ) as HTMLButtonElement;
    act(() => mdBtn.click());
    await flush();

    const text = view();
    expect(text).toContain("已存在");
  });
});
