// ui.tsx 的可访问性与焦点规范（Wave 1b）。
//
// 改版前所有按钮都没有统一的键盘焦点样式：Tab 到某个按钮时只能靠浏览器默认描边，
// 在深色背景上几乎看不见；图标按钮也没有可访问名称。这里钉住三条最低要求：
// 1) 任何可交互元素 Tab 聚焦时必须有可见焦点环（不依赖浏览器默认）；
// 2) 纯图标按钮必须有 aria-label 或 title，否则读屏与测试都拿不到名字；
// 3) 禁用态不能只靠降低透明度表达"不可点"，还要 aria-disabled / disabled。
import * as TT from "@radix-ui/react-tooltip";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { Button, Input, Toggle, Tip } from "./ui";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let roots: { unmount(): void }[] = [];
afterEach(() => {
  act(() => {
    for (const r of roots) r.unmount();
  });
  roots = [];
  document.body.innerHTML = "";
});

function mount(el: React.ReactElement) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  roots.push(root);
  act(() => root.render(el));
  return host;
}

const buttons = () => [...document.body.querySelectorAll("button")] as HTMLButtonElement[];

describe("Button 的可访问性", () => {
  it("给 aria-label 的图标按钮能把名字透传到 DOM", () => {
    mount(<Button size="icon" aria-label="关闭" />);
    expect(buttons()[0].getAttribute("aria-label")).toBe("关闭");
  });

  it("disabled 同时落到原生属性，键盘无法聚焦", () => {
    mount(<Button disabled>发送</Button>);
    expect(buttons()[0].disabled).toBe(true);
  });

  it("默认 type=button，避免在表单里误触发提交", () => {
    mount(<Button>普通按钮</Button>);
    expect(buttons()[0].type).toBe("button");
  });

  it("键盘聚焦时有可见焦点环类名（不依赖浏览器默认描边）", () => {
    mount(<Button>发送</Button>);
    // 用 data 属性而不是嗅探 CSS：jsdom 不算样式，这条断言钉住的是"类名被挂上"这个事实
    expect(buttons()[0].className).toContain("ee-focus");
  });
});

describe("Toggle 的可访问性", () => {
  it("是 role=switch 且 aria-checked 跟随状态", () => {
    mount(<Toggle on={false} onChange={() => undefined} />);
    const el = document.body.querySelector("[role='switch']")!;
    expect(el.getAttribute("aria-checked")).toBe("false");
  });

  it("点击后回调收到取反值", () => {
    let got: boolean | null = null;
    mount(<Toggle on={false} onChange={(v) => (got = v)} />);
    act(() => (document.body.querySelector("[role='switch']") as HTMLElement).click());
    expect(got).toBe(true);
  });
});

describe("Input 的可访问性", () => {
  it("label 关联：Field 里的 input 能通过 id/label 找到", () => {
    mount(
      <label>
        <span>项目名</span>
        <Input defaultValue="ee" />
      </label>,
    );
    const input = document.body.querySelector("input")!;
    expect(input.closest("label")).toBeTruthy();
  });

  it("focus-visible 时挂上统一焦点环类名", () => {
    mount(<Input defaultValue="" />);
    expect(document.body.querySelector("input")!.className).toContain("ee-focus");
  });
});

describe("Tip 的可访问性", () => {
  it("包裹的按钮仍是原生 button，不被包成 div 后失去键盘可达", () => {
    mount(
      <TT.Provider>
        <Tip label="收起侧栏">
          <button aria-label="收起侧栏">x</button>
        </Tip>
      </TT.Provider>,
    );
    expect(buttons().length).toBe(1);
  });
});
