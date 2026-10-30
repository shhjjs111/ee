import { test, expect, ElectronApplication, Page } from "@playwright/test";
import { _electron as electron } from "playwright";
import path from "node:path";

let app: ElectronApplication;
let win: Page;

test.beforeAll(async () => {
  // EE_TEST_USER_DATA：独立 userData，避免与正在运行的 ee 抢 disk cache（主进程支持）
  process.env.EE_TEST_USER_DATA = path.resolve(__dirname, "../test-results/electron-user-data");
  app = await electron.launch({ args: ["."], cwd: path.resolve(__dirname, "..") });
  win = await app.firstWindow();
  await win.waitForLoadState("domcontentloaded");
  await win.locator("[data-testid=app-shell]").waitFor({ state: "visible", timeout: 60_000 });
  await win.waitForTimeout(800);
});

test.afterAll(async () => {
  await app?.close().catch(() => undefined);
});

/**
 * 真机缺陷（用户实测 2026-10-30）：长会话下对话区无法上下滚动，输入框（Composer）
 * 被推出视口，用户「看不见我打字的那个框」。
 *
 * 结构上滚动链是：#root(100%) → AppShell(h-full overflow-hidden) → main 列(min-h-0)
 *   → Conversation(flex-1 flex-col) → 滚动区(flex-1 overflow-y-auto) → Composer 在滚动区外。
 * 这条链任何一层高度失守（比如某层变成 auto 由内容撑开），页面就会整体溢出、
 * 内部滚动失效——正是用户看到的样子。
 *
 * 本测试不依赖真实模型：直接往真实 DOM 的对话容器里注入大量条目，
 * 量每个关键层的 computed style，断言滚动链完好。
 */
test("长会话下对话区可滚动、Composer 始终在视口内（ACC-52 / 真机回归）", async () => {
  // 往对话滚动区里塞 120 个高条目，模拟长会话
  await win.evaluate(() => {
    const scroller = document.querySelector<HTMLElement>(".overflow-y-auto");
    if (!scroller) throw new Error("找不到对话滚动容器 .overflow-y-auto");
    const inner = scroller.firstElementChild as HTMLElement;
    for (let i = 0; i < 120; i++) {
      const d = document.createElement("div");
      d.dataset.probe = "1";
      d.style.height = "60px";
      d.textContent = `探针条目 ${i}：这是一段足够长的内容，用来模拟真实的长 Markdown 回复。`;
      inner.appendChild(d);
    }
  });
  await win.waitForTimeout(300);

  // 量滚动链每一层
  const info = await win.evaluate(() => {
    const read = (el: Element | null) => {
      if (!el) return null;
      const cs = getComputedStyle(el);
      return {
        clientH: el.clientHeight,
        scrollH: el.scrollHeight,
        overflowY: cs.overflowY,
        minHeight: cs.minHeight,
        height: cs.height,
        canScroll: el.scrollHeight > el.clientHeight,
      };
    };
    const shell = document.querySelector<HTMLElement>('[data-testid="app-shell"]')!;
    const main = shell.querySelector(".flex.min-h-0") as HTMLElement | null;
    const scroller = document.querySelector<HTMLElement>(".overflow-y-auto");
    const composer = document.querySelector("textarea");
    const rect = composer?.getBoundingClientRect();
    return {
      shell: read(shell),
      main: read(main),
      scroller: read(scroller),
      composerTop: rect ? Math.round(rect.top) : null,
      composerBottom: rect ? Math.round(rect.bottom) : null,
      innerH: window.innerHeight,
    };
  });

  // 1. 滚动链每层高度必须被约束住（clientH 不随内容增长）
  expect(info.shell?.clientH, "AppShell 高度失守").toBeLessThanOrEqual(info.innerH);
  expect(info.scroller?.overflowY, "对话滚动容器的 overflow-y 不是 auto/scroll").toMatch(/auto|scroll/);

  // 2. 长内容下滚动容器必须真的可滚
  expect(info.scroller?.canScroll, "长内容下滚动容器不可滚（被内容撑开了）").toBe(true);

  // 3. 滚动到底部：Composer 必须仍在视口内
  await win.evaluate(() => {
    const scroller = document.querySelector<HTMLElement>(".overflow-y-auto")!;
    scroller.scrollTop = scroller.scrollHeight;
  });
  await win.waitForTimeout(200);
  const after = await win.evaluate(() => {
    const rect = document.querySelector("textarea")?.getBoundingClientRect();
    return { top: rect ? Math.round(rect.top) : null, bottom: rect ? Math.round(rect.bottom) : null, innerH: window.innerHeight };
  });
  expect(after.top, "Composer 顶部超出视口（用户看不到输入框）").toBeLessThan(after.innerH);
  expect(after.bottom, "Composer 底部超出视口").toBeLessThanOrEqual(after.innerH);
});

/** 用户真机场景：流式 Markdown 长回复（标题/列表/代码块混排）下滚动 + 输入框可见。 */
test("流式 Markdown 长回复下滚动链完好（真机缺陷回归）", async () => {
  // 清掉上一个用例注入的探针条目
  await win.evaluate(() => {
    document.querySelectorAll("[data-probe]").forEach((el) => el.remove());
    const scroller = document.querySelector<HTMLElement>(".overflow-y-auto")!;
    const inner = scroller.firstElementChild as HTMLElement;
    // 注入真实 Markdown 渲染产物：h2/h3/ul/pre/code 混排，与用户的 3000 字文章形态一致
    for (let i = 0; i < 40; i++) {
      const d = document.createElement("div");
      d.dataset.probe = "1";
      d.innerHTML = `
        <h2>章节 ${i}：架构与集成</h2>
        <p>这是一段较长的正文，用来模拟真实回复的密度与换行行为，包含<strong>加粗</strong>、<em>斜体</em>与<code>行内代码</code>。</p>
        <ul><li>要点一：性能与稳定性</li><li>要点二：工程化实践</li><li>要点三：用户体验细节</li></ul>
        <pre><code>const example = "code block ${i}";\nif (example) { console.log(example); }</code></pre>
        <h3>小节 ${i}.1 延伸讨论</h3>
        <p>收尾段落，保证每块高度足够，40 块足以撑出数千像素的滚动距离。</p>`;
      inner.appendChild(d);
    }
  });
  await win.waitForTimeout(400);

  const md = await win.evaluate(() => {
    const scroller = document.querySelector<HTMLElement>(".overflow-y-auto")!;
    const composer = document.querySelector("textarea");
    const rect = composer?.getBoundingClientRect();
    return {
      canScroll: scroller.scrollHeight > scroller.clientHeight,
      overflowY: getComputedStyle(scroller).overflowY,
      composerTop: rect ? Math.round(rect.top) : null,
      composerBottom: rect ? Math.round(rect.bottom) : null,
      innerH: window.innerHeight,
      // 关键：滚动容器是否被内部某块（如 pre）撑破——父容器 clientH 应小于视口且稳定
      scrollerH: scroller.clientHeight,
    };
  });

  expect(md.overflowY).toMatch(/auto|scroll/);
  expect(md.canScroll, "Markdown 长内容下不可滚").toBe(true);
  // 未滚动时 Composer 也必须在视口内（flex 布局下它贴底，不该被内容推走）
  expect(md.composerTop, "未滚动时 Composer 已被内容推出视口顶部以下").toBeLessThan(md.innerH);
  expect(md.composerBottom, "未滚动时 Composer 在视口外").toBeLessThanOrEqual(md.innerH);

  // 滚到底也要在
  await win.evaluate(() => {
    const scroller = document.querySelector<HTMLElement>(".overflow-y-auto")!;
    scroller.scrollTop = scroller.scrollHeight;
  });
  await win.waitForTimeout(200);
  const bottom = await win.evaluate(() => {
    const rect = document.querySelector("textarea")?.getBoundingClientRect();
    return { top: rect ? Math.round(rect.top) : null, innerH: window.innerHeight };
  });
  expect(bottom.top, "滚到底后 Composer 不可见").toBeLessThan(bottom.innerH);
});

/**
 * 真机缺陷（用户截图 2026-10-30 #3）：侧栏被挤出屏幕左缘（残影可见）、
 * 页面横向溢出、纵向无法滚动、Composer 不可见。
 * 根因假设：超长无空格内容（长 URL / 长代码行 / 宽表格）撑破宽度约束链，
 * 使某个 flex 祖先被内容撑开 → 整页横向溢出 → 内部纵向滚动失效。
 */
test("超长无空格内容不得撑破布局（横向溢出回归）", async () => {
  await win.evaluate(() => {
    document.querySelectorAll("[data-probe]").forEach((el) => el.remove());
    const scroller = document.querySelector<HTMLElement>(".overflow-y-auto")!;
    const inner = scroller.firstElementChild as HTMLElement;
    // 三类「不可断行」内容：超长 URL、超长代码 token、宽 Markdown 表格
    const longUrl = "https://example.com/" + "a".repeat(3000);
    const longToken = "x".repeat(3000);
    let table = "<table><tr>";
    for (let i = 0; i < 30; i++) table += `<th>列头_${i}_比较长的内容</th>`;
    table += "</tr>";
    for (let r = 0; r < 5; r++) {
      table += "<tr>";
      for (let i = 0; i < 30; i++) table += `<td>单元格 ${r}-${i} 一些内容让表格足够宽</td>`;
      table += "</tr>";
    }
    table += "</table>";
    for (let i = 0; i < 6; i++) {
      const d = document.createElement("div");
      d.dataset.probe = "1";
      d.innerHTML = `
        <p>下面是一个超长链接：<a href="#">${longUrl}</a></p>
        <pre><code>const t = "${longToken}";</code></pre>
        ${table}
        <p>表格之后的正常段落，用于检查后续内容仍然可见。</p>`;
      inner.appendChild(d);
    }
  });
  await win.waitForTimeout(400);

  const m = await win.evaluate(() => {
    const shell = document.querySelector<HTMLElement>('[data-testid="app-shell"]')!;
    const sidebar = shell.firstElementChild as HTMLElement;
    const sidebarRect = sidebar?.getBoundingClientRect();
    const scroller = document.querySelector<HTMLElement>(".overflow-y-auto")!;
    const composer = document.querySelector("textarea");
    const rect = composer?.getBoundingClientRect();
    return {
      bodyScrollW: document.body.scrollWidth,
      innerW: window.innerWidth,
      horizontalOverflow: document.body.scrollWidth > window.innerWidth,
      sidebarLeft: sidebarRect ? Math.round(sidebarRect.left) : null,
      sidebarVisible: sidebarRect ? sidebarRect.left >= 0 && sidebarRect.width > 0 : null,
      scrollerW: scroller.clientWidth,
      composerLeft: rect ? Math.round(rect.left) : null,
      composerVisible: rect ? rect.left >= 0 && rect.right <= window.innerWidth : null,
    };
  });

  // 核心断言：页面不得横向溢出（溢出 = 侧栏被挤出、布局被撑破）
  expect(m.horizontalOverflow, `页面横向溢出：body.scrollWidth=${m.bodyScrollW} > 视口 ${m.innerW}，侧栏被挤出屏幕`).toBe(false);
  expect(m.sidebarVisible, "侧栏被挤出视口外").toBe(true);
  expect(m.composerVisible, "Composer 被推出视口").toBe(true);
});

/**
 * min-h-0 结构守卫（2026-10-30）：
 * Conversation 根 div 现在带 min-h-0。真机曾出现一次长会话下布局被撑爆的坏状态
 * （容器 5046px、侧栏与对话区被顶出视口），本层缺 min-h-0 是嫌疑之一但未能确证为
 * 根因（回滚后真实数据遍历 19 个会话均正常）。本用例作为纵深防御：若未来某次
 * 改动让这层失去 min-h-0 且内容可撑爆布局，这里会变红。
 * （注入内容挂在滚动容器内不会触发该失效——已实测；本用例守卫的是「约束仍在」。）
 */
test("Conversation 层有 min-h-0，长内容不撑爆布局（真机缺陷结构性回归）", async () => {
  // 前置：确保在某个已打开的会话里（滚动容器只在会话详情视图存在）。
  // 前面用例可能把会话切走或清了内容，这里点侧栏第一个会话条目兜底。
  await win.evaluate(() => {
    const sb = document.querySelector('[data-testid="shell-sidebar"]');
    const cands = [...(sb?.querySelectorAll("*") ?? [])].filter((el) => {
      const t = (el.textContent || "").trim();
      return (el.children.length === 0 || el.tagName === "SPAN") && t.length > 6 &&
        !/^(新对话|聊天|需处理|更多|全部|搜索|设置|分组)/.test(t);
    });
    cands.find((el) => el.getBoundingClientRect().width > 100)?.click();
  });
  await win.waitForTimeout(1500);

  const ready = await win.evaluate(() => document.querySelectorAll(".overflow-y-auto").length);
  if (ready < 2) test.skip(true, "测试环境没有可打开的历史会话（干净 userData），跳过结构性回归");

  // 注入极长内容到**滚动容器的父级（Conversation 层）**，模拟真实缺陷的传导路径：
  // 缺 min-h-0 时，任何在该层渲染的超高内容都会把它撑爆（内容进滚动容器只会被
  // overflow 截断，触发不了这个 bug——上一版用例因此假绿）。
  await win.evaluate(() => {
    const scroller = document.querySelectorAll<HTMLElement>(".overflow-y-auto")[1] as HTMLElement;
    const conv = scroller.parentElement as HTMLElement;
    for (let i = 0; i < 80; i++) {
      const d = document.createElement("div");
      d.dataset.probe = "1";
      d.style.height = "60px";
      d.textContent = `min-h-0 回归条目 ${i}`;
      conv.appendChild(d);
    }
  });
  await win.waitForTimeout(300);

  const m = await win.evaluate(() => {
    const scroller = document.querySelectorAll<HTMLElement>(".overflow-y-auto")[1] as HTMLElement;
    const conv = scroller.parentElement as HTMLElement;
    const composer = document.querySelector("textarea");
    const cr = composer?.getBoundingClientRect();
    return {
      convH: Math.round(conv.getBoundingClientRect().height),
      convMinH: getComputedStyle(conv).minHeight,
      canScroll: scroller.scrollHeight > scroller.clientHeight,
      composerBottom: cr ? Math.round(cr.bottom) : null,
      innerH: window.innerHeight,
    };
  });

  expect(m.convMinH, "Conversation 层缺 min-h-0（真机缺陷根因：flex 默认 min-height:auto 让长内容撑爆布局）").toBe("0px");
  expect(m.convH, "Conversation 层被内容撑爆（超出视口高）").toBeLessThanOrEqual(m.innerH);
  expect(m.canScroll, "长内容下对话区不可滚").toBe(true);
  expect(m.composerBottom, "Composer 不可见").toBeLessThanOrEqual(m.innerH);
});
