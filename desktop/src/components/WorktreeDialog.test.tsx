// @vitest-environment jsdom
import * as Tooltip from "@radix-ui/react-tooltip";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { useApp } from "../lib/store";
import type { RpcClient } from "../lib/rpc";
import { WorktreeDialog } from "./WorktreeDialog";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const wt = (repo: string, branch: string) => ({ name: branch, path: `${repo}/wt/${branch}`, repo, branch, base: "x", created: 0 });

describe("WorktreeDialog", () => {
  afterEach(() => {
    act(() => useApp.setState({ worktreeDialog: null }));
    document.body.innerHTML = "";
  });

  it("切换项目后，旧项目迟到的 worktree/list 结果不会覆盖新项目的列表", async () => {
    const pending: Record<string, (v: unknown) => void> = {};
    const rpc = {
      call: (method: string, params: { cwd: string }) =>
        new Promise((resolve) => {
          if (method === "worktree/list") pending[params.cwd] = resolve;
        }),
    } as unknown as RpcClient;
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    await act(async () => {
      useApp.setState({ rpc, worktreeDialog: "/A" });
      root.render(
        <Tooltip.Provider>
          <WorktreeDialog />
        </Tooltip.Provider>,
      );
    });
    await act(async () => useApp.setState({ worktreeDialog: "/B" }));
    await act(async () => pending["/B"]({ repo: "/B", worktrees: [wt("/B", "ee/b-branch")] }));
    await act(async () => pending["/A"]({ repo: "/A", worktrees: [wt("/A", "ee/a-branch")] }));
    expect(document.body.textContent).toContain("ee/b-branch");
    expect(document.body.textContent).not.toContain("ee/a-branch");
    await act(async () => root.unmount());
  });
});
