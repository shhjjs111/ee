import * as Dialog from "@radix-ui/react-dialog";
import { ChevronLeft, ChevronRight, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useApp } from "../lib/store";
import type { AttachmentMeta } from "../types";

const MIN_SCALE = 1;
const MAX_SCALE = 5;
const STEP = 1.15;

/**
 * 点开对话里的图片看**原图**，多张时可连续翻看。
 *
 * 为什么必须向后台要原图：`item.attachments[].thumb` 是发送前 canvas 压到 160px 的
 * data URL（`lib/attachments.ts` 的 THUMB_SIDE），放大就是一坨糊块。原图躺在
 * `~/.ee/sessions/<会话id>/attachments/<id>.<ext>`，走 `attachment/read` 取回。
 *
 * 交互上的几个决定：
 * - 先用缩略图立刻撑开（点击有即时反馈），原图到了原地替换；取不到也不算失败。
 * - 原图按 id 缓存：来回翻看不会反复请求，也不会因为换图闪一下占位。
 * - 切换图片时缩放**复位**——带着上一张的 3 倍去开下一张，人会以为图坏了。
 * - 翻到头不绕回：从最后一张按右跳到第一张会让人以为顺序乱了。
 * - 滚轮监听用原生 addEventListener：React 的 onWheel 是 passive，
 *   preventDefault 无效，缩放时底层消息列表会跟着滚。
 */
export function ImageLightbox({
  threadId,
  atts,
  startIndex,
  onClose,
}: {
  threadId: string;
  atts: AttachmentMeta[];
  startIndex: number;
  onClose: () => void;
}) {
  const rpc = useApp((s) => s.rpc);
  const [i, setI] = useState(startIndex);
  const [fulls, setFulls] = useState<Record<string, string>>({});
  const [errs, setErrs] = useState<Record<string, string>>({});
  const [scale, setScale] = useState(1);
  const [pos, setPos] = useState({ x: 0, y: 0 });
  const boxRef = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x: number; y: number; ox: number; oy: number } | null>(null);

  const att = atts[i];
  const n = atts.length;

  const go = useCallback(
    (d: number) => {
      setI((cur) => {
        const next = cur + d;
        return next < 0 || next >= n ? cur : next;
      });
      setScale(1);
      setPos({ x: 0, y: 0 });
    },
    [n],
  );

  // 拉原图：按 id 缓存，翻回来不重复请求
  useEffect(() => {
    if (!rpc || !att || fulls[att.id] || errs[att.id]) return;
    let alive = true;
    rpc
      .call("attachment/read", { thread_id: threadId, attachment_id: att.id })
      .then((r) => {
        if (alive && typeof r?.data_url === "string") setFulls((m) => ({ ...m, [att.id]: r.data_url }));
      })
      .catch((e: Error) => {
        if (alive) setErrs((m) => ({ ...m, [att.id]: e?.message || "读不到原图" }));
      });
    return () => {
      alive = false;
    };
  }, [rpc, threadId, att, fulls, errs]);

  // 左右方向键切换（Escape 交给 Radix 关闭）
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key === "ArrowLeft") {
        e.preventDefault();
        go(-1);
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        go(1);
      }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [go]);

  // 滚轮缩放（passive:false，见文件头说明）。
  // 挂 window 而不是 Content 的 ref：Radix Portal 里 ref 的挂载时机比 effect 晚，
  // 实测绑不上（jsdom 里滚轮事件全丢）。灯箱是模态、整屏都是它，全局监听等价。
  useEffect(() => {
    const h = (e: WheelEvent) => {
      e.preventDefault();
      const k = e.deltaY < 0 ? STEP : 1 / STEP;
      setScale((s) => Math.min(MAX_SCALE, Math.max(MIN_SCALE, Math.round(s * k * 1000) / 1000)));
    };
    window.addEventListener("wheel", h, { passive: false });
    return () => window.removeEventListener("wheel", h);
  }, []);

  // 缩回 1 倍时把平移归零，否则图会停在偏移后的位置
  useEffect(() => {
    if (scale <= 1) setPos({ x: 0, y: 0 });
  }, [scale]);

  if (!att) return null;
  const url = fulls[att.id];
  const err = errs[att.id];
  const pannable = scale > 1;

  return (
    <Dialog.Root open onOpenChange={(o) => !o && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-black/80 fadein" />
        <Dialog.Content
          ref={boxRef}
          data-testid="image-lightbox"
          className="fixed inset-0 z-50 flex flex-col items-center justify-center overflow-hidden outline-none"
          aria-describedby={undefined}
        >
          <Dialog.Title className="sr-only">{att.name}</Dialog.Title>

          <button
            type="button"
            onClick={onClose}
            title="关闭（Esc）"
            className="absolute right-4 top-4 flex h-9 w-9 items-center justify-center rounded-full bg-black/50 text-white/90 transition hover:bg-black/70"
          >
            <X size={18} />
          </button>

          {n > 1 && (
            <>
              <button
                type="button"
                onClick={() => go(-1)}
                disabled={i === 0}
                title="上一张（←）"
                className="absolute left-4 flex h-10 w-10 items-center justify-center rounded-full bg-black/50 text-white/90 transition hover:bg-black/70 disabled:opacity-25"
              >
                <ChevronLeft size={20} />
              </button>
              <button
                type="button"
                onClick={() => go(1)}
                disabled={i === n - 1}
                title="下一张（→）"
                className="absolute right-4 flex h-10 w-10 items-center justify-center rounded-full bg-black/50 text-white/90 transition hover:bg-black/70 disabled:opacity-25"
              >
                <ChevronRight size={20} />
              </button>
            </>
          )}

          <img
            src={url ?? att.thumb}
            alt={att.name}
            data-full={url ? "1" : "0"}
            data-scale={scale}
            onDoubleClick={() => setScale((s) => (s > 1 ? 1 : 2.5))}
            onPointerDown={(e) => {
              if (!pannable) return;
              drag.current = { x: e.clientX, y: e.clientY, ox: pos.x, oy: pos.y };
              e.currentTarget.setPointerCapture(e.pointerId);
            }}
            onPointerMove={(e) => {
              const d = drag.current;
              if (!d) return;
              setPos({ x: d.ox + (e.clientX - d.x), y: d.oy + (e.clientY - d.y) });
            }}
            onPointerUp={() => {
              drag.current = null;
            }}
            draggable={false}
            style={{
              transform: `translate(${pos.x}px, ${pos.y}px) scale(${scale})`,
              cursor: pannable ? (drag.current ? "grabbing" : "grab") : "zoom-in",
            }}
            className="max-h-[86vh] max-w-[92vw] select-none rounded-xl object-contain shadow-2xl transition-transform duration-75"
          />

          <div className="absolute bottom-4 flex max-w-[90vw] items-center gap-2 rounded-full bg-black/50 px-3 py-1.5 text-[12px] text-white/85">
            <span className="max-w-[46vw] truncate">{att.name}</span>
            {n > 1 && <span className="shrink-0 tabular-nums">{i + 1} / {n}</span>}
            {scale > 1 && <span className="shrink-0 tabular-nums">×{scale.toFixed(scale < 2 ? 2 : 1)}</span>}
            {err ? <span className="text-danger">{err}</span> : !url && <span className="text-white/60">正在加载原图…</span>}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
