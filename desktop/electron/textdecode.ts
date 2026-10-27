// 子进程（内核）输出解码：先按 UTF-8 流式解（多字节字符被拆在两块之间也能拼对）；
// 一旦出现非法 UTF-8（中文 Windows 上启动器/系统报错是 GBK），本条流后面都按 GBK 解，不再乱码。
import { TextDecoder } from "node:util";
export class OutputDecoder {
  private utf8: TextDecoder | null = new TextDecoder("utf-8", { fatal: true });
  private fallback: TextDecoder | null = null;
  private pending: Buffer = Buffer.alloc(0); // UTF-8 阶段尚未确认的字节（切换编码时要重解）

  constructor(private readonly fallbackEncoding = "gbk") {}

  write(chunk: Buffer): string {
    if (this.fallback) return this.fallback.decode(chunk, { stream: true });
    const data = Buffer.concat([this.pending, chunk]);
    try {
      const s = this.utf8!.decode(chunk, { stream: true });
      // 末尾可能有半个字符被 TextDecoder 扣着：记住这些字节，切换编码时一起交给 GBK
      const used = Buffer.byteLength(s, "utf8");
      this.pending = data.subarray(Math.min(used, data.length));
      return s;
    } catch {
      return this.switchToFallback(data);
    }
  }

  end(): string {
    if (this.fallback) return this.fallback.decode();
    try {
      return this.utf8!.decode();
    } catch {
      return this.switchToFallback(this.pending, true);
    }
  }

  private switchToFallback(data: Buffer, final = false): string {
    this.utf8 = null;
    this.pending = Buffer.alloc(0);
    try {
      this.fallback = new TextDecoder(this.fallbackEncoding);
    } catch {
      this.fallback = new TextDecoder("utf-8");
    }
    return this.fallback.decode(data, { stream: !final });
  }
}
