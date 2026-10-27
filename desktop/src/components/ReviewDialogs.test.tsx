// @vitest-environment jsdom
import * as TT from "@radix-ui/react-tooltip";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { useApp } from "../lib/store";
import { Dialogs } from "./ReviewDialogs";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const roots: { unmount(): void }[] = [];
afterEach(async () => {
  for (const r of roots.splice(0)) await act(async () => r.unmount());
  act(() => useApp.setState({ dialog: null }));
  document.body.innerHTML = "";
});

async function mount() {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  roots.push(root);
  await act(async () =>
    root.render(
      <TT.Provider>
        <Dialogs />
      </TT.Provider>,
    ),
  );
}

describe("审查 / 提交弹窗", () => {
  it("没有打开项目时 /review 不会一直转圈，而是提示先选项目", async () => {
    useApp.setState({ rpc: { call: () => new Promise(() => undefined) } as never, activeThreadId: null, activeWorkspace: null, dialog: { kind: "review" } });
    await mount();
    expect(document.body.textContent).toContain("先在左侧选择");
  });

  it("没有打开项目时 /commit 给出提示", async () => {
    useApp.setState({ rpc: { call: () => new Promise(() => undefined) } as never, activeThreadId: null, activeWorkspace: null, dialog: { kind: "commit" } });
    await mount();
    expect(document.body.textContent).toContain("先在左侧选择");
  });

  it("切换项目后旧项目迟到的 review/targets 不覆盖新项目", async () => {
    const pending: Record<string, (v: unknown) => void> = {};
    useApp.setState({
      rpc: { call: (_m: string, p: { cwd: string }) => new Promise((r) => (pending[p.cwd] = r)) } as never,
      activeThreadId: null,
      activeWorkspace: "/A",
      dialog: { kind: "review" },
    });
    await mount();
    await act(async () => useApp.setState({ activeWorkspace: "/B" }));
    await act(async () => pending["/B"]({ is_repo: true, branch: "main-b", dirty: true, branches: ["main-b"], commits: [] }));
    await act(async () => pending["/A"]({ is_repo: true, branch: "main-a", dirty: true, branches: ["main-a", "dev-a"], commits: [] }));
    expect(document.body.textContent).toContain("main-b");
    expect(document.body.textContent).not.toContain("main-a");
  });
});
