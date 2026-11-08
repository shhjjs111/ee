// @vitest-environment jsdom
/**
 * 点开对话里的图片看原图。
 *
 * 为什么需要这条：`item.attachments[].thumb` 只有 160px（发送前 canvas 压过），
 * 用户点开想看细节时，界面必须去后台要 `attachment/read` 拿原图——
 * 这段接线断了，界面上什么都不会报错，只是图一直是糊的。
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

const THUMB = "data:image/jpeg;base64,THUMB";
const FULL = "data:image/png;base64,FULL";

const item = {
  id: "u1",
  type: "user",
  text: "看图",
  attachments: [{ id: "abc123def456", name: "截图.png", mime: "image/png", size: 100, thumb: THUMB }],
} as UserItem;

function mount(call: ReturnType<typeof vi.fn>) {
  act(() =>
    useApp.setState({
      activeThreadId: "t1",
      rpc: { call },
      details: { t1: { status: "idle" } },
    } as never),
  );
  return render(<UserMessage item={item} />);
}

describe("对话里的图片：点开看原图", () => {
  it("点缩略图后向后台要原图，拿到了就换成原图", async () => {
    const call = vi.fn().mockResolvedValue({ id: "abc123def456", mime: "image/png", data_url: FULL });
    const el = mount(call);

    const thumbBtn = el.querySelector("button[title*='看大图']") as HTMLButtonElement;
    expect(thumbBtn, "缩略图没有做成可点击的按钮").toBeTruthy();

    await act(async () => {
      thumbBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    // 参数必须对得上后端签名：thread_id + attachment_id
    expect(call).toHaveBeenCalledWith("attachment/read", {
      thread_id: "t1",
      attachment_id: "abc123def456",
    });

    const box = document.body.querySelector("[data-testid='image-lightbox']");
    expect(box, "点了之后没有灯箱").toBeTruthy();
    const img = box!.querySelector("img") as HTMLImageElement;
    expect(img.getAttribute("data-full"), "原图没替换掉缩略图").toBe("1");
    expect(img.src).toBe(FULL);
  });

  it("取不到原图时仍显示缩略图，并说明原因", async () => {
    const call = vi.fn().mockRejectedValue(new Error("图片已不在磁盘上（可能被清理了）"));
    const el = mount(call);

    await act(async () => {
      (el.querySelector("button[title*='看大图']") as HTMLButtonElement).dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
    });

    const box = document.body.querySelector("[data-testid='image-lightbox']");
    expect(box, "原图取不到也不该连灯箱都不开").toBeTruthy();
    const img = box!.querySelector("img") as HTMLImageElement;
    expect(img.getAttribute("data-full")).toBe("0");
    expect(img.src).toBe(THUMB);
    expect(box!.textContent).toContain("不在磁盘上");
  });
});
