// @vitest-environment jsdom
/**
 * 点开对话里的图片看原图，以及多张时的连续翻看。
 *
 * 为什么需要这一层：`item.attachments[].thumb` 只有 160px（发送前 canvas 压过），
 * 用户点开想看细节时界面必须去后台要 `attachment/read` 拿原图。这段接线断了，
 * 界面不会报任何错，只是图一直是糊的——所以每次加载都要断言真的换了 src。
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { UserMessage } from "./Items";
import { useApp } from "../lib/store";
import type { UserItem } from "../types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let mounted: Root | null = null;

afterEach(() => {
  if (mounted) act(() => mounted!.unmount());
  mounted = null;
  document.body.innerHTML = "";
});

function render(node: React.ReactNode) {
  const el = document.createElement("div");
  document.body.appendChild(el);
  const root = createRoot(el);
  mounted = root;
  act(() => root.render(node));
  return el;
}

const FULL: Record<string, string> = {
  aaa111aaa111: "data:image/png;base64,FULL1",
  bbb222bbb222: "data:image/png;base64,FULL2",
  ccc333ccc333: "data:image/png;base64,FULL3",
};
const thumb = (id: string) => `data:image/jpeg;base64,T${id.slice(0, 3)}`;

const item = {
  id: "u1",
  type: "user",
  text: "这三张图",
  attachments: ["aaa111aaa111", "bbb222bbb222", "ccc333ccc333"].map((id, i) => ({
    id,
    name: `截图${i + 1}.png`,
    mime: "image/png",
    size: 100,
    thumb: thumb(id),
  })),
} as UserItem;

/** 每个 id 返回它自己的原图；记录调用次数用于断言缓存。 */
function rpcMock() {
  return vi.fn((_m: string, p: { attachment_id: string }) =>
    Promise.resolve({ id: p.attachment_id, mime: "image/png", data_url: FULL[p.attachment_id] }),
  );
}

function mount(call: ReturnType<typeof vi.fn>, one = item) {
  act(() =>
    useApp.setState({
      activeThreadId: "t1",
      rpc: { call },
      details: { t1: { status: "idle" } },
    } as never),
  );
  return render(<UserMessage item={one} />);
}

const thumbs = (el: HTMLElement) => Array.from(el.querySelectorAll("button[title*='看大图']")) as HTMLButtonElement[];
const lightbox = () => document.body.querySelector("[data-testid='image-lightbox']") as HTMLElement | null;
const shown = () => lightbox()!.querySelector("img") as HTMLImageElement;

async function click(el: Element) {
  await act(async () => {
    el.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
}

async function press(key: string) {
  await act(async () => {
    window.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
  });
}

describe("对话里的图片：点开看原图", () => {
  it("点缩略图后向后台要原图，拿到了就换成原图", async () => {
    const call = rpcMock();
    const el = mount(call);

    const btn = thumbs(el)[0];
    expect(btn, "缩略图没有做成可点击的按钮").toBeTruthy();
    await click(btn);

    expect(call).toHaveBeenCalledWith("attachment/read", { thread_id: "t1", attachment_id: "aaa111aaa111" });
    expect(lightbox(), "点了之后没有灯箱").toBeTruthy();
    expect(shown().getAttribute("data-full")).toBe("1");
    expect(shown().src).toBe(FULL.aaa111aaa111);
  });

  it("取不到原图时仍显示缩略图，并说明原因", async () => {
    const call = vi.fn().mockRejectedValue(new Error("图片已不在磁盘上（可能被清理了）"));
    const el = mount(call);

    await click(thumbs(el)[0]);

    expect(lightbox(), "原图取不到也不该连灯箱都不开").toBeTruthy();
    expect(shown().getAttribute("data-full")).toBe("0");
    expect(shown().src).toBe(thumb("aaa111aaa111"));
    expect(lightbox()!.textContent).toContain("不在磁盘上");
  });
});

describe("多张图片：连续翻看", () => {
  it("点哪张开哪张，并且显示「第几张 / 共几张」", async () => {
    const call = rpcMock();
    const el = mount(call);

    await click(thumbs(el)[1]);
    expect(shown().src).toBe(FULL.bbb222bbb222);
    expect(lightbox()!.textContent).toContain("2 / 3");
    // 灯箱里的图是第 2 张的名字
    expect(lightbox()!.textContent).toContain("截图2.png");
  });

  it("方向键左右切换，到头不绕回", async () => {
    const call = rpcMock();
    const el = mount(call);
    await click(thumbs(el)[0]);

    await press("ArrowRight");
    expect(shown().src).toBe(FULL.bbb222bbb222);
    await press("ArrowRight");
    expect(shown().src).toBe(FULL.ccc333ccc333);
    // 已经是最后一张：再按不动，不能绕回第一张（会让人以为翻乱了）
    await press("ArrowRight");
    expect(shown().src).toBe(FULL.ccc333ccc333);
    expect(lightbox()!.textContent).toContain("3 / 3");

    await press("ArrowLeft");
    expect(shown().src).toBe(FULL.bbb222bbb222);
  });

  it("翻回看过的图不重复请求（原图有缓存）", async () => {
    const call = rpcMock();
    const el = mount(call);
    await click(thumbs(el)[0]);
    await press("ArrowRight");
    await press("ArrowLeft");

    const ids = call.mock.calls.map((c) => (c[1] as { attachment_id: string }).attachment_id);
    expect(ids).toEqual(["aaa111aaa111", "bbb222bbb222"]);
  });

  it("只有一张时不出翻页控件", async () => {
    const call = rpcMock();
    const one = { ...item, attachments: [item.attachments![0]] } as UserItem;
    const el = mount(call, one);
    await click(thumbs(el)[0]);
    expect(lightbox()!.textContent).not.toContain("1 / 1");
  });
});

describe("缩放", () => {
  it("滚轮放大、缩小，并显示倍率", async () => {
    const call = rpcMock();
    const el = mount(call);
    await click(thumbs(el)[0]);

    const box = lightbox()!;
    const img = shown();
    await act(async () => {
      box.dispatchEvent(new WheelEvent("wheel", { deltaY: -120, bubbles: true, cancelable: true }));
    });
    expect(Number(img.dataset.scale)).toBeGreaterThan(1);
    expect(box.textContent).toContain("×");

    const bigger = Number(img.dataset.scale);
    await act(async () => {
      box.dispatchEvent(new WheelEvent("wheel", { deltaY: 120, bubbles: true, cancelable: true }));
    });
    expect(Number(img.dataset.scale)).toBeLessThan(bigger);
  });

  it("切换图片后缩放复位，不会带着上一个倍率看下一张", async () => {
    const call = rpcMock();
    const el = mount(call);
    await click(thumbs(el)[0]);
    const box = lightbox()!;
    await act(async () => {
      box.dispatchEvent(new WheelEvent("wheel", { deltaY: -240, bubbles: true, cancelable: true }));
    });
    expect(Number(shown().dataset.scale)).toBeGreaterThan(1);

    await press("ArrowRight");
    expect(Number(shown().dataset.scale)).toBe(1);
  });
});
