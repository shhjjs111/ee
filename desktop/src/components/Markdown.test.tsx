// Markdown 增量渲染（T22）：流式期间纯文本、稳定后一次性 markdown。
// 目的：流式输出每个 delta 都变 text，ReactMarkdown 的全文解析是同步 CPU 开销，
// 长回复下每帧都重解析会卡输入。流式期间用纯文本渲染，终态再一次性走 markdown。
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { Markdown } from "./Markdown";

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

describe("Markdown 增量渲染", () => {
  it("流式期间：markdown 语法按字面量显示，不解析成 <strong>", () => {
    const host = mount(<Markdown text="**粗体** 和 `code`" streaming={true} />);
    // 纯文本：语法字符原样可见，没有 strong/code 元素
    expect(host.textContent).toContain("**粗体**");
    expect(host.querySelector("strong")).toBeNull();
    expect(host.querySelector("code")).toBeNull();
  });

  it("稳定后：走 ReactMarkdown，**粗体** 解析成 <strong>", () => {
    const host = mount(<Markdown text="**粗体**" streaming={false} />);
    expect(host.querySelector("strong")?.textContent).toBe("粗体");
  });

  it("不传 streaming（老调用）按稳定态处理，保持向后兼容", () => {
    const host = mount(<Markdown text="**粗体**" />);
    expect(host.querySelector("strong")?.textContent).toBe("粗体");
  });

  it("流式期间中文文本逐字保留（不丢字、不破坏组合字符）", () => {
    const host = mount(<Markdown text="你好，世界 🌏" streaming={true} />);
    expect(host.textContent).toContain("你好，世界 🌏");
  });
});
