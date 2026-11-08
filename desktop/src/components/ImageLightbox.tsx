import * as Dialog from "@radix-ui/react-dialog";
import { useEffect, useState } from "react";
import { useApp } from "../lib/store";
import type { AttachmentMeta } from "../types";

/**
 * 点开对话里的图片看**原图**。
 *
 * 为什么要向后台要：`item.attachments[].thumb` 是发送前 canvas 压到 160px 的
 * data URL（`lib/attachments.ts` 的 THUMB_SIDE），拿它放大就是一坨糊块。原图躺在
 * `~/.ee/sessions/<会话id>/attachments/<id>.<ext>`，走 `attachment/read` 取回。
 *
 * 交互取舍：先用缩略图立刻撑开（不等网络，点击有即时反馈），原图到了原地替换；
 * 取不到原图也不算失败——缩略图仍能看，只是底部说明原因。
 */
export function ImageLightbox({
  threadId,
  att,
  onClose,
}: {
  threadId: string;
  att: AttachmentMeta;
  onClose: () => void;
}) {
  const rpc = useApp((s) => s.rpc);
  const [full, setFull] = useState<string | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    if (!rpc) return; // 没连接（浏览器调试模式）时只显示缩略图
    let alive = true;
    rpc
      .call("attachment/read", { thread_id: threadId, attachment_id: att.id })
      .then((r) => {
        if (alive && typeof r?.data_url === "string") setFull(r.data_url);
      })
      .catch((e: Error) => {
        if (alive) setErr(e?.message || "读不到原图");
      });
    return () => {
      alive = false;
    };
  }, [rpc, threadId, att.id]);

  return (
    <Dialog.Root open onOpenChange={(o) => !o && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-black/75 fadein" />
        <Dialog.Content
          data-testid="image-lightbox"
          className="fixed left-1/2 top-1/2 z-50 flex max-h-[92vh] max-w-[92vw] -translate-x-1/2 -translate-y-1/2 flex-col items-center outline-none"
          onKeyDown={(e) => {
            if (e.key === "Escape") onClose();
          }}
        >
          {/* 标题用 sr-only：Dialog 需要可访问名，但用户看的是图不是字 */}
          <Dialog.Title className="sr-only">{att.name}</Dialog.Title>
          <img
            src={full ?? att.thumb}
            alt={att.name}
            className="max-h-[86vh] max-w-[92vw] rounded-xl object-contain shadow-2xl"
            data-full={full ? "1" : "0"}
          />
          <div className="mt-2 max-w-[80vw] truncate text-center text-[12px] text-muted">
            {att.name}
            {err ? ` · ${err}` : full ? "" : " · 正在加载原图…"}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
