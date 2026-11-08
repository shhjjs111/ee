/** 图片「另存为」的数据处理（与 IPC 解耦，好在 node 环境里直接测）。 */

const MIME_EXT: Record<string, string> = {
  "image/png": "png",
  "image/jpeg": "jpg",
  "image/gif": "gif",
  "image/webp": "webp",
};

/** 认得的图片 MIME；不在表里的一律不写盘。 */
export function imageExt(mime: string): string | null {
  return MIME_EXT[mime] ?? null;
}

/**
 * data URL → 可写盘的字节。**不是图片就返回 null**。
 *
 * 只认 `data:image/<已知类型>;base64,` 这一种形态：
 * 界面上要写盘的是从 `attachment/read` 拿回来的原图，任何别的字符串（http 链接、
 * text/plain、缺 base64 标记）都不该被当成图片落地——写进去就是一坨坏文件，
 * 而用户以为自己存了一张图。
 */
export function decodeImageDataUrl(dataUrl: unknown): { bytes: Buffer; ext: string } | null {
  if (typeof dataUrl !== "string") return null;
  const m = /^data:([a-z]+\/[a-z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)$/i.exec(dataUrl);
  if (!m) return null;
  const ext = imageExt(m[1].toLowerCase());
  if (!ext) return null;
  const bytes = Buffer.from(m[2].replace(/\s+/g, ""), "base64");
  return bytes.length ? { bytes, ext } : null;
}

/**
 * 保存对话框里的建议文件名。
 *
 * 用户在会话里发的图可能是任意名字（含 Windows 不允许的 `\ / : * ? " < > |`、
 * 甚至整串空白），直接塞进 defaultPath 会得到一个非法路径或覆盖 `CON` 这类保留名。
 * 扩展名以实际内容为准（`ext`），不信用户文件名里的后缀——一张叫 `.jpg` 的 PNG
 * 存出去还是该叫 `.png`。
 */
export function suggestedFileName(name: unknown, ext: string): string {
  const raw = typeof name === "string" ? name : "";
  const base = raw
    .replace(/\.([a-z0-9]{1,5})$/i, "") // 去掉原后缀，下面按真实格式补
    .replace(/[\\/:*?"<>|\u0000-\u001f]/g, "_") // Windows 非法字符
    .replace(/^\.+|\.+$/g, "") // 前后点会得到隐藏文件或空名
    .trim();
  // CON/PRN/AUX/NUL/COM1-9/LPT1-9 在 Windows 上是保留设备名，加了扩展名也打不开
  const reserved = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])$/i.test(base);
  const safe = (reserved ? `_${base}` : base).slice(0, 80) || "image";
  return `${safe}.${ext}`;
}
