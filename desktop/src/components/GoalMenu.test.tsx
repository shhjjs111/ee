// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { GoalMenu } from "./GoalMenu";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// 不能直接 document.body.innerHTML = ""：React 会失去它挂载的根，
// 卸载时报 "The node to be removed is not a child of this node"。
// 留着根引用，用 root.unmount() 正确卸载。
let roots: { unmount: () => void }[] = [];

afterEach(() => {
  act(() => {
    for (const r of roots) r.unmount();
  });
  roots = [];
  document.body.innerHTML = "";
});

function mount(props: Parameters<typeof GoalMenu>[0]) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  roots.push(root);
  act(() => {
    root.render(<GoalMenu {...props} />);
  });
  // Radix 菜单经 Portal 渲染到 body，断言一律看 body。
  return {
    view: () => document.body.textContent ?? "",
    buttons: () => [...document.body.querySelectorAll("button")] as HTMLButtonElement[],
    textInputs: () =>
      [...document.body.querySelectorAll("input")].filter(
        (i) => i.type === "text" || i.type === "",
      ) as HTMLInputElement[],
  };
}

const flush = async () => {
  await act(async () => {
    await Promise.resolve();
  });
};

function setInput(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  act(() => {
    setter.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

/** Radix 的 Trigger 在 jsdom 里由 pointerdown 触发（click 不生效，实测 0 → 14 字符）。 */
function press(btn: HTMLButtonElement) {
  act(() => {
    btn.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, button: 0, pointerType: "mouse" }));
  });
}

/** 点开 `+`，再点「目标」进入输入态。 */
async function openGoal(ui: ReturnType<typeof mount>) {
  press(ui.buttons()[0]);
  await flush();
  const goalItem = [...document.body.querySelectorAll("[role='menuitem']")].find((el) =>
    (el.textContent ?? "").includes("目标"),
  ) as HTMLElement | undefined;
  expect(goalItem, "菜单里没有「目标」项").toBeTruthy();
  act(() => goalItem!.click());
  await flush();
}

describe("GoalMenu（Codex 式两级交互）", () => {
  it("`+` 点开显示分组菜单，每项带标题和描述", async () => {
    const ui = mount({ onSubmit: vi.fn() });
    press(ui.buttons()[0]);
    await flush();

    const text = ui.view();
    expect(text).toContain("添加");
    expect(text).toContain("目标");
    expect(text).toContain("设置要持续追求的目标");
  });

  it("给了 onPickFiles 才显示「文件和文件夹」", async () => {
    const ui1 = mount({ onSubmit: vi.fn() });
    press(ui1.buttons()[0]);
    await flush();
    expect(ui1.view()).not.toContain("文件和文件夹");

    act(() => {
      for (const r of roots) r.unmount();
    });
    roots = [];
    document.body.innerHTML = "";
    const ui2 = mount({ onSubmit: vi.fn(), onPickFiles: vi.fn() });
    press(ui2.buttons()[0]);
    await flush();
    expect(ui2.view()).toContain("文件和文件夹");
  });

  it("点「目标」从菜单原位换成输入态（不新开窗口）", async () => {
    const ui = mount({ onSubmit: vi.fn() });
    await openGoal(ui);
    expect(ui.textInputs().length).toBe(1);
    expect(ui.textInputs()[0].placeholder).toContain("pytest");
    expect(ui.view()).toContain("要持续追求的目标");
  });

  it("空判据不能提交（避免跑一个没有判据的循环）", async () => {
    const onSubmit = vi.fn();
    const ui = mount({ onSubmit });
    await openGoal(ui);

    const start = ui.buttons().find((b) => (b.textContent ?? "").trim() === "开始");
    expect(start?.disabled, "空判据时「开始」应当禁用").toBe(true);
    act(() => start?.click());
    await flush();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("填了判据后提交，把命令与轮次一起交给调用方", async () => {
    const onSubmit = vi.fn();
    const ui = mount({ onSubmit, defaultRounds: 3 });
    await openGoal(ui);

    setInput(ui.textInputs()[0], "pytest -q");
    await flush();
    const start = ui.buttons().find((b) => (b.textContent ?? "").trim() === "开始")!;
    act(() => start.click());
    await flush();

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit.mock.calls[0][0]).toMatchObject({ command: "pytest -q", maxRounds: 3 });
  });

  it("提交后关掉菜单并清空（不把上次的判据留下）", async () => {
    const onSubmit = vi.fn();
    const ui = mount({ onSubmit });
    await openGoal(ui);
    setInput(ui.textInputs()[0], "make test");
    await flush();
    act(() => ui.buttons().find((b) => (b.textContent ?? "").trim() === "开始")!.click());
    await flush();

    expect(onSubmit).toHaveBeenCalled();
    expect(ui.textInputs().length).toBe(0);
  });

  it("轮次被限制在 1..20", async () => {
    const onSubmit = vi.fn();
    const ui = mount({ onSubmit });
    await openGoal(ui);
    setInput(ui.textInputs()[0], "pytest");
    await flush();

    const num = [...document.body.querySelectorAll("input")].find(
      (i) => i.type === "number",
    ) as HTMLInputElement;
    setInput(num, "9999");
    await flush();
    act(() => ui.buttons().find((b) => (b.textContent ?? "").trim() === "开始")!.click());
    await flush();

    expect(onSubmit.mock.calls[0][0].maxRounds).toBe(20);
  });
});
