import { beforeEach, describe, expect, it, vi } from "vitest";
import { openPath } from "./openPath";
import { useApp } from "./store";

describe("openPath（渲染进程）", () => {
  const showToast = vi.fn();
  beforeEach(() => {
    showToast.mockReset();
    useApp.setState({ showToast } as never);
  });
  it("成功不提示；被拦截/失败时 toast", async () => {
    (window as unknown as { ee: unknown }).ee = { openPath: vi.fn(async (p: string) => (p.endsWith(".bat") ? "为安全起见不直接运行 .bat 文件，已在资源管理器中定位" : p === "bad" ? "只能打开当前工作区或 ee 目录里的文件" : "")) };
    await openPath("/w/a.md");
    expect(showToast).not.toHaveBeenCalled();
    await openPath("/w/a.bat");
    expect(showToast).toHaveBeenLastCalledWith(expect.stringContaining("资源管理器"), "info");
    await openPath("bad");
    expect(showToast).toHaveBeenLastCalledWith(expect.stringContaining("工作区"), "error");
  });
  it("IPC 抛错也提示", async () => {
    (window as unknown as { ee: unknown }).ee = { openPath: vi.fn(async () => Promise.reject(new Error("boom"))) };
    await openPath("/w/a.md");
    expect(showToast).toHaveBeenCalledWith("打开失败：boom", "error");
  });
});
