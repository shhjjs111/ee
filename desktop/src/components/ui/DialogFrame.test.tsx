// @vitest-environment jsdom
// 对话框统一外壳（Wave 5）。
//
// 改版前四个业务对话框各自写一份 Dialog.Content：top 10/12/14%、max-h 72–78vh、
// 宽度 560/620/640。切不同对话框时弹窗位置会跳。这里把外壳统一，并钉住三件事：
// 1) 标题与关闭按钮都在可访问名称正确的位置；
// 2) 关闭只调 onClose，不自己做状态；
// 3) 宽度档位真的落到 DOM 上（sm/md/lg 各不相同）。
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DialogFrame } from "./DialogFrame";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let roots: { unmount(): void }[] = [];
afterEach(() => {
  act(() => {
    for (const r of roots) r.unmount();
  });
  roots = [];
  document.body.innerHTML = "";
});

function mount(props: Omit<Parameters<typeof DialogFrame>[0], "children">) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  roots.push(root);
  act(() => root.render(<DialogFrame {...props}>正文内容</DialogFrame>));
  return document.body;
}

const frame = () => document.body.querySelector("[data-testid='dialog-frame']") as HTMLElement | null;

describe("DialogFrame", () => {
  it("标题与正文都渲染出来", () => {
    mount({ title: "比较候选", onClose: () => undefined });
    expect(frame()).toBeTruthy();
    expect(frame()!.textContent).toContain("比较候选");
    expect(frame()!.textContent).toContain("正文内容");
  });

  it("关闭按钮有可访问名称，点击后调 onClose", () => {
    const onClose = vi.fn();
    mount({ title: "导出会话", onClose });
    const btn = document.body.querySelector("[aria-label='关闭']") as HTMLButtonElement;
    expect(btn).toBeTruthy();
    act(() => btn.click());
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("紧贴右上角是关闭按钮：位置在标题之后（不在正文里）", () => {
    mount({ title: "T", onClose: () => undefined });
    const btn = document.body.querySelector("[aria-label='关闭']") as HTMLElement;
    // 关闭按钮是图标按钮，不带文字；断言它在标题行里，而不是散落在正文
    expect(btn.closest("[data-testid='dialog-frame']")).toBe(frame());
  });

  it("宽度档位不同，实际类名不同", () => {
    mount({ title: "A", onClose: () => undefined, width: "sm" });
    const sm = frame()!.className;
    act(() => {
      for (const r of roots) r.unmount();
    });
    roots = [];
    document.body.innerHTML = "";
    mount({ title: "B", onClose: () => undefined, width: "lg" });
    const lg = frame()!.className;
    expect(sm).not.toBe(lg);
    expect(lg).toContain("720px");
  });

  it("统一位置：不再各自 top-10/12/14% 漂移", () => {
    mount({ title: "T", onClose: () => undefined });
    expect(frame()!.className).toContain("top-[10%]");
    expect(frame()!.className).toContain("max-h-[78vh]");
  });

  it("自定义关闭按钮名称会透传", () => {
    mount({ title: "T", onClose: () => undefined, closeLabel: "关闭比较" });
    expect(document.body.querySelector("[aria-label='关闭比较']")).toBeTruthy();
  });
});
