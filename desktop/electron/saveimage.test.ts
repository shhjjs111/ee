// @vitest-environment node
import { describe, expect, it } from "vitest";
import { decodeImageDataUrl, imageExt, suggestedFileName } from "./saveimage";

// latin1 构造：PNG 头里的 `\x89` 是单字节，用默认 utf8 会编成两字节（194 137）
const PNG = "data:image/png;base64," + Buffer.from("\x89PNG\r\n\x1a\n" + "x".repeat(16), "latin1").toString("base64");

describe("另存为：只写真正的图片", () => {
  it("认得的图片格式转成字节 + 扩展名", () => {
    const r = decodeImageDataUrl(PNG)!;
    expect(r.ext).toBe("png");
    expect(r.bytes.subarray(0, 8)).toEqual(Buffer.from("\x89PNG\r\n\x1a\n", "binary"));
  });

  it.each([
    ["http 链接", "https://example.com/a.png"],
    ["非图片 data URL", "data:text/plain;base64,aGVsbG8="],
    ["pdf", "data:application/pdf;base64,JVBERi0="],
    ["缺 base64 标记", "data:image/png,abc"],
    ["空内容", "data:image/png;base64,"],
    ["不是字符串", 123],
  ])("%s 一律拒绝", (_label, bad) => {
    expect(decodeImageDataUrl(bad)).toBeNull();
  });

  it("jpeg 存成 .jpg（不是 .jpeg）", () => {
    expect(imageExt("image/jpeg")).toBe("jpg");
    expect(imageExt("image/svg+xml")).toBeNull();
  });
});

describe("另存为：建议文件名", () => {
  it("去掉原后缀，按真实格式补", () => {
    expect(suggestedFileName("截图.png", "jpg")).toBe("截图.jpg");
    expect(suggestedFileName("report.final.JPEG", "png")).toBe("report.final.png");
  });

  it("Windows 非法字符换成下划线", () => {
    expect(suggestedFileName('a<b>c:d"e|f?g*h', "png")).toBe("a_b_c_d_e_f_g_h.png");
  });

  it("保留设备名不能撞（CON / LPT1）", () => {
    expect(suggestedFileName("CON.png", "png")).toBe("_CON.png");
    expect(suggestedFileName("lpt1", "jpg")).toBe("_lpt1.jpg");
  });

  it("空名字/纯点 给兜底名，不产出隐藏文件", () => {
    expect(suggestedFileName("", "png")).toBe("image.png");
    expect(suggestedFileName("...", "png")).toBe("image.png");
    expect(suggestedFileName(undefined, "webp")).toBe("image.webp");
  });

  it("超长名字截断到 80 字符", () => {
    expect(suggestedFileName("x".repeat(200), "png").length).toBe(84); // 80 + ".png"
  });
});
