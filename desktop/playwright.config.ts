// Playwright 配置（Wave 0 Task 1）：真实 Electron 窗口的渲染基线。
//
// 为什么单独一份配置：Vitest 用 jsdom 跑组件（vite.config.mts），证明不了
// 「窗口打开后长什么样、多宽、有没有溢出」。这两类验证不能混在一个 runner 里。
//
// 运行前提：必须先 `npm run build` —— Electron 加载的是 dist/，不重建就看不到
// 源码改动（这一点在改版过程中踩过：第一张截图用的是 27 号的旧构建）。

import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  // 视觉基线只在本机跑：不同 Windows 版本/DPI 的像素结果不可比。
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  use: {
    // 失败时留证据，但不自动更新基线（基线变更必须人工确认）。
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
});
