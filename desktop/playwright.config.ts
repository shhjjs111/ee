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
  // 单个用例的上限。注意它靠 JS 定时器实现，遇到 Electron 进程挂死、
  // 或者 page.evaluate 卡在不可中断的原生调用时，定时器自己也不会被调度——
  // 用例超时救不了。所以下面还要一条真正的全局底线。
  timeout: 120_000,
  // 全局硬上限（含所有用例与重试）。实测发生过一次 CI 上
  // 「E2E with real Electron」卡了 44 分钟不退，把整个 verify 拖过 45 分钟
  // 的 job 上限而被取消，连带 package / publish 全 skipped。
  // 卡住就是卡住，到点就判失败，不要拖死发布。
  globalTimeout: 12 * 60_000,
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  use: {
    // 失败时留证据，但不自动更新基线（基线变更必须人工确认）。
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
});
