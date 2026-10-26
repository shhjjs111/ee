// 图片附件（D3-D）：浏览器里用 canvas 压缩，不依赖后端图像库。
export type Attachment = {
  key: string;
  name: string;
  data: string; // base64（不带 data: 前缀）
  thumb: string; // 小缩略图 data URL（≤ ~30KB），存进会话条目用于显示
  size: number;
};

export const MAX_ATTACHMENTS = 8;
const MAX_SIDE = 2048;
const THUMB_SIDE = 160;
const ACCEPT = ["image/png", "image/jpeg", "image/gif", "image/webp"];

export function isImageFile(f: File) {
  return ACCEPT.includes(f.type) || /\.(png|jpe?g|gif|webp)$/i.test(f.name);
}

function loadImage(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("图片无法解码"));
    img.src = url;
  });
}

function draw(img: HTMLImageElement, maxSide: number, mime: string, quality: number): string {
  const scale = Math.min(1, maxSide / Math.max(img.naturalWidth, img.naturalHeight));
  const w = Math.max(1, Math.round(img.naturalWidth * scale));
  const h = Math.max(1, Math.round(img.naturalHeight * scale));
  const c = document.createElement("canvas");
  c.width = w;
  c.height = h;
  const ctx = c.getContext("2d")!;
  if (mime === "image/jpeg") {
    ctx.fillStyle = "#fff"; // JPEG 没有透明通道
    ctx.fillRect(0, 0, w, h);
  }
  ctx.drawImage(img, 0, 0, w, h);
  return c.toDataURL(mime, quality);
}

function readAsDataURL(f: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result));
    r.onerror = () => reject(new Error("读取文件失败"));
    r.readAsDataURL(f);
  });
}

/** 压缩：长边 ≤ 2048；GIF 保留原图（动图）；PNG 截图保持 PNG，大于 1.5MB 才转 JPEG。 */
export async function prepareImage(f: File): Promise<Attachment> {
  const orig = await readAsDataURL(f);
  const img = await loadImage(orig);
  const big = Math.max(img.naturalWidth, img.naturalHeight) > MAX_SIDE;
  let url = orig;
  if (f.type !== "image/gif" && (big || f.size > 1.5 * 2 ** 20)) {
    url = draw(img, MAX_SIDE, "image/png", 1);
    if (url.length * 0.75 > 1.5 * 2 ** 20) url = draw(img, MAX_SIDE, "image/jpeg", 0.88);
  }
  const thumb = draw(img, THUMB_SIDE, "image/jpeg", 0.75);
  const data = url.split(",", 2)[1] ?? "";
  return { key: `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`, name: f.name || "image.png", data, thumb, size: Math.round(data.length * 0.75) };
}
