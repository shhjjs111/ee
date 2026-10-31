// @vitest-environment jsdom
/**
 * 发送失败要把文字退回输入框（ACC-24）的**组件层**验证。
 *
 * 为什么需要这一层：store 层已有断言（`store.test.ts` 的「空闲时 turn/start 失败」）
 * 只证明 `returnedDraft` 这个**状态被写入**，不证明**输入框真的拿到了文字**。
 * 中间还有一段：Composer 用 `useEffect` 监听 `returnedDraft.nonce`，
 * 把它消费掉并 merge 进本地 `text`。这段断了，store 测试照样绿，用户照样丢字——
 * 正是「加字段要查赋值点、也要查消费点」的那个坑。
 *
 * 这层不需要真实 Electron：Composer 是普通 React 组件，jsdom + createRoot 足够。
 * 唯一的环境要求是 radix 的 Tooltip 需要 Provider（生产里由 App.tsx:176 提供），
 * 测试里补上即可。
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import * as TT from "@radix-ui/react-tooltip";
import { Composer } from "./Composer";
import { useApp } from "../lib/store";
import { useLoops } from "../lib/loopStore";
import { useAutomations } from "../lib/automationStore";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let mounted: Root | null = null;

afterEach(() => {
  // 必须真卸载，不能只清 DOM：残留组件仍订阅 store，下一个用例改 store 时
  // 它的 effect 在 act 之外触发更新。实测三个用例连着跑会冒出 6 条
  // "not wrapped in act"，单独跑却各是 0 条——是跨用例污染，不是被测代码有问题。
  if (mounted) act(() => mounted!.unmount());
  mounted = null;
  document.body.innerHTML = "";
});

beforeEach(() => {
  // Composer 依赖这两个旁路 store；不清空会串上别的用例
  useLoops.setState({ loops: [] } as never);
  useAutomations.setState({ automations: [] } as never);
});

/** App.tsx 里 Tip 需要 TooltipProvider；单独渲染 Composer 时必须自己补。 */
function render(node: React.ReactNode) {
  const el = document.createElement("div");
  document.body.appendChild(el);
  const root = createRoot(el);
  mounted = root;
  act(() => root.render(<TT.Provider>{node}</TT.Provider>));
  return el;
}

const baseState = {
  rpc: null, // 无连接：组件跳过全部 RPC 调用，测的正是纯本地状态流
  activeThreadId: "t1",
  activeWorkspace: "/w",
  workspaces: ["/w"],
  threads: [{ id: "t1", title: "t1", status: "idle", cwd: "/w" }],
  details: { t1: { id: "t1", cwd: "/w", title: "t1", status: "idle", items: [] } },
  returnedDraft: null,
  composerDraft: null,
};

function textarea(el: HTMLElement) {
  const ta = el.querySelector("textarea");
  expect(ta, "Composer 没有渲染出 textarea").toBeTruthy();
  return ta as HTMLTextAreaElement;
}

describe("发送失败保文字（ACC-24）", () => {
  it("returnedDraft 落到当前对话时，文字真的出现在输入框里", () => {
    act(() => useApp.setState(baseState as never));
    const el = render(<Composer />);
    expect(textarea(el).value).toBe("");

    // 模拟 store.send() 失败后的写入（store.ts:441 catch 分支）
    act(() => {
      useApp.setState({
        returnedDraft: { threadId: "t1", texts: ["帮我改一下 README"], nonce: Date.now() },
      } as never);
    });

    expect(textarea(el).value).toContain("帮我改一下 README");
  });

  it("输入框里已有草稿时，退回的文字并进去而不是覆盖用户正在写的内容", () => {
    act(() => useApp.setState(baseState as never));
    const el = render(<Composer />);
    const ta = textarea(el);

    act(() => {
      // 用户正在打字
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
      setter.call(ta, "我自己打的字");
      ta.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(textarea(el).value).toBe("我自己打的字");

    act(() => {
      useApp.setState({
        returnedDraft: { threadId: "t1", texts: ["失败退回的那句"], nonce: Date.now() },
      } as never);
    });

    const merged = textarea(el).value;
    expect(merged).toContain("失败退回的那句");
    expect(merged, "用户正在打的字被覆盖了").toContain("我自己打的字");
  });

  it("退回内容属于别的对话时不插进当前输入框", () => {
    act(() => useApp.setState(baseState as never));
    const el = render(<Composer />);

    act(() => {
      useApp.setState({
        returnedDraft: { threadId: "另一个会话", texts: ["不该出现在这里"], nonce: Date.now() },
      } as never);
    });

    expect(textarea(el).value).toBe("");
  });
});

describe("子会话卡在审批时，父界面要看得见（P1-8）", () => {
  it("当前对话的子会话等审批时，输入区上方出现横幅并带子会话名与工具", () => {
    act(() =>
      useApp.setState({
        ...baseState,
        pendingChildApprovals: {
          t1: {
            parent_id: "t1",
            thread_id: "c1",
            kind: "approval",
            child_title: "子任务甲",
            call_id: "call_1",
            tool: "shell",
            summary: "rm -rf build",
          },
        },
      } as never),
    );
    const el = render(<Composer />);
    const text = el.textContent ?? "";
    expect(text).toContain("子任务甲");
    expect(text).toContain("shell");
  });

  it("提示条要能直接批准/拒绝，不走侧栏", async () => {
    const calls: [string, unknown][] = [];
    act(() =>
      useApp.setState({
        ...baseState,
        rpc: {
          call: async (m: string, p: unknown) => {
            calls.push([m, p]);
            return {};
          },
        },
        pendingChildApprovals: {
          t1: {
            parent_id: "t1",
            thread_id: "c1",
            kind: "approval",
            child_title: "子任务甲",
            call_id: "call_1",
            tool: "shell",
            summary: "rm -rf build",
          },
        },
      } as never),
    );
    const el = render(<Composer />);
    const btn = Array.from(el.querySelectorAll("button")).find((b) =>
      (b.textContent ?? "").includes("批准"),
    );
    expect(btn, "横幅里没有批准按钮").toBeTruthy();
    await act(async () => {
      btn!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    const hit = calls.find(([m]) => m === "approval/respond");
    expect(hit, "没调 approval/respond").toBeTruthy();
    // 后端签名是 (thread_id, call_id, choice, reason)；参数名写错会被
    // test_rpc_contract 抓住，这里提前在组件层挡住。
    const body = hit![1] as Record<string, unknown>;
    expect(body.choice).toBe("once");
    expect(body.thread_id).toBe("c1");
    // 点了之后提示必须消失，否则用户会反复点同一条
    expect(useApp.getState().pendingChildApprovals["t1"]).toBeFalsy();
  });
});
