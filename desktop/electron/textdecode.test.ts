// @vitest-environment node
import { describe, expect, it } from "vitest";
import { OutputDecoder } from "./textdecode";

const gbk = Buffer.from([0xcf, 0xb5, 0xcd, 0xb3, 0xd5, 0xd2, 0xb2, 0xbb, 0xb5, 0xbd, 0xd6, 0xb8, 0xb6, 0xa8, 0xb5, 0xc4, 0xce, 0xc4, 0xbc, 0xfe]); // “系统找不到指定的文件”

describe("后端输出解码", () => {
  it("UTF-8 的中文被拆在两块之间也不乱码", () => {
    const b = Buffer.from("内核启动失败：找不到模块", "utf8");
    const d = new OutputDecoder();
    let s = "";
    for (let i = 0; i < b.length; i += 1) s += d.write(b.subarray(i, i + 1));
    s += d.end();
    expect(s).toBe("内核启动失败：找不到模块");
  });

  it("中文 Windows 的 GBK 输出（如启动器报错）按 GBK 解", () => {
    const d = new OutputDecoder();
    const s = d.write(Buffer.concat([Buffer.from("[PYI-1:ERROR] "), gbk.subarray(0, 7)])) + d.write(gbk.subarray(7)) + d.end();
    expect(s).toBe("[PYI-1:ERROR] 系统找不到指定的文件");
  });
});
