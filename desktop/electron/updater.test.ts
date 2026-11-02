// @vitest-environment node
import { describe, expect, it, vi } from "vitest";
import { EventEmitter } from "node:events";
import fs from "node:fs";
import path from "node:path";
import { FEED, UpdateController, friendlyError, releaseUrl, type UpdateState } from "./updater";

/** 假 autoUpdater：只管发出事件、记录调用，不碰网络。 */
class FakeUpdater extends EventEmitter {
  autoDownload = false;
  autoInstallOnAppQuit = true;
  checks = 0;
  installs: Array<[boolean, boolean]> = [];
  failWith: Error | null = null;
  /** setFeedURL 收到的参数（真实现里决定去哪里拉包）。 */
  feeds: unknown[] = [];

  setFeedURL(opts: unknown) {
    this.feeds.push(opts);
  }

  async checkForUpdates() {
    this.checks++;
    this.emit("checking-for-update");
    if (this.failWith) {
      this.emit("error", this.failWith);
      throw this.failWith;
    }
    return { updateInfo: { version: "9.9.9" } };
  }

  quitAndInstall(silent: boolean, forceRun: boolean) {
    this.installs.push([silent, forceRun]);
  }
}

function make(opts: { packaged?: boolean; failWith?: Error | null } = {}) {
  const updater = new FakeUpdater();
  if (opts.failWith !== undefined) updater.failWith = opts.failWith;
  const states: UpdateState[] = [];
  const logs: string[] = [];
  const ctl = new UpdateController({
    currentVersion: "0.1.0",
    packaged: opts.packaged ?? true,
    updater: updater as never,
    emit: (s) => states.push(s),
    log: (l) => logs.push(l),
  });
  return { ctl, updater, states, logs };
}

describe("更新源钉死在代码里", () => {
  it("start 时把源设成本仓库，且只设一次", () => {
    const { ctl, updater } = make();
    ctl.start();
    expect(updater.feeds).toEqual([{ provider: "github", owner: "shhjjs111", repo: "ee" }]);
    // 再检查一次也不能重置内部 provider
    void ctl.check();
    expect(updater.feeds).toHaveLength(1);
  });

  it("不读 getFeedURL（6.8.11 里它固定返回废弃提示字符串）", () => {
    // 守住这次踩过的坑：真实现 getFeedURL() 恒返回 "Deprecated. Do not use it."，
    // 拿它做来源校验会让每次检查都被拒。源码里不许再出现这个调用。
    const src = fs.readFileSync(path.join(__dirname, "updater.ts"), "utf-8");
    expect(src).not.toMatch(/\.getFeedURL\s*\(/); // 只拦真实调用，注释里可以提
  });

  it("FEED 与 package.json 的 build.publish 一致", () => {
    // 两处分别写着 owner/repo，改一处忘另一处就会去拉别人的包。
    const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf-8"));
    const publish = pkg.build.publish[0];
    expect({ provider: FEED.provider, owner: FEED.owner, repo: FEED.repo }).toEqual({
      provider: publish.provider,
      owner: publish.owner,
      repo: publish.repo,
    });
  });

  it("发布页地址与更新源同源", () => {
    expect(releaseUrl()).toBe(`https://github.com/${FEED.owner}/${FEED.repo}/releases`);
  });

  it("setFeedURL 抛错时给出可读错误，不把异常漏出去", () => {
    const { ctl, updater } = make();
    updater.setFeedURL = () => {
      throw new Error("ERR_UPDATER_INVALID_PROVIDER_CONFIGURATION");
    };
    expect(() => ctl.start()).not.toThrow();
    expect(ctl.getState().phase).toBe("error");
    expect(ctl.getState().message).toContain("INVALID_PROVIDER");
    expect(updater.checks).toBe(0); // 源没配好就别发请求
  });
});

describe("报错洗成人话", () => {
  it("几 KB 的 Atom XML 只留第一行，不把界面撑爆", () => {
    // 实测现场：GitHub 只有 Pre-release 时，报错里带着整份 feed + release notes
    const xml = `<feed xmlns="http://www.w3.org/2005/Atom">${'<entry><title>x</title></entry>'.repeat(200)}</feed>`;
    const raw = new Error(`Cannot parse releases feed: ${xml}`);
    const msg = friendlyError(raw);
    expect(msg).not.toContain("<feed");
    expect(msg).not.toContain("<entry>");
    expect(msg.length).toBeLessThanOrEqual(201); // 200 字 + 省略号
  });

  it("只有预发布时给出人话，而不是 GitHub 原始报错", () => {
    // 实际报错：Unable to find latest version on GitHub (...), please ensure a production release exists: HttpError: 406
    const e = new Error(
      "Unable to find latest version on GitHub (https://github.com/shhjjs111/ee/releases/latest), please ensure a production release exists: HttpError: 406",
    );
    expect(friendlyError(e)).toContain("还没有正式发布版本");
    expect(friendlyError(new Error("HttpError: 404"))).toContain("还没有正式发布版本");
  });

  it("网络不通给出可操作的建议", () => {
    expect(friendlyError(new Error("getaddrinfo ENOTFOUND github.com"))).toContain("网络或代理");
    expect(friendlyError(new Error("connect ETIMEDOUT 140.82.121.3:443"))).toContain("网络或代理");
  });

  it("其余错误保留原意（不限成一句空话）", () => {
    expect(friendlyError(new Error("校验失败：sha512 不匹配"))).toBe("校验失败：sha512 不匹配");
    expect(friendlyError("字符串错误")).toBe("字符串错误");
  });

  it("检查失败时状态里出现的是人话，不是原始长文", async () => {
    const xml = "Cannot parse releases feed: " + "<entry>y</entry>".repeat(200);
    const { ctl, updater } = make({ failWith: new Error(xml) });
    ctl.start();
    await vi.waitFor(() => expect(ctl.getState().phase).toBe("error"));
    const msg = ctl.getState().message ?? "";
    expect(msg).not.toContain("<entry>");
    expect(msg.length).toBeLessThanOrEqual(201);
  });
});

describe("开发态不启用", () => {
  it("packaged=false 时 start 什么都不注册、也不检查", () => {
    const { ctl, updater, logs } = make({ packaged: false });
    ctl.start();
    expect(updater.checks).toBe(0);
    expect(updater.eventNames()).toEqual([]);
    expect(logs.some((l) => l.includes("开发态"))).toBe(true);
    expect(ctl.getState().phase).toBe("idle");
  });

  it("开发态手动检查给出可读原因，而不是静默失败", async () => {
    const { ctl } = make({ packaged: false });
    const s = await ctl.check();
    expect(s.phase).toBe("error");
    expect(s.message).toContain("开发版");
  });
});

describe("状态机", () => {
  it("start 设置成只下载不自动装，并触发首次检查", async () => {
    const { ctl, updater } = make();
    ctl.start();
    expect(updater.autoDownload).toBe(true);
    expect(updater.autoInstallOnAppQuit).toBe(false); // 绝不替用户决定重启
    await vi.waitFor(() => expect(updater.checks).toBe(1));
  });

  it("发现新版本 → 下载中 → 下载完成，版本与进度都带上", async () => {
    const { ctl, updater, states } = make();
    ctl.start();
    await vi.waitFor(() => expect(ctl.getState().phase).toBe("checking"));

    updater.emit("update-available", { version: "0.2.0" });
    expect(ctl.getState()).toMatchObject({ phase: "available", version: "0.2.0", percent: 0 });

    updater.emit("download-progress", { percent: 42.345 });
    // 限一位小数：每帧原样转发会白占渲染线程
    expect(ctl.getState()).toMatchObject({ phase: "downloading", percent: 42.3 });

    updater.emit("update-downloaded", { version: "0.2.0" });
    expect(ctl.getState()).toMatchObject({ phase: "downloaded", version: "0.2.0", percent: 100 });
    expect(ctl.getState().message).toContain("重启后生效");
    expect(states.length).toBeGreaterThanOrEqual(4);
  });

  it("进度越界会被夹回 0-100", () => {
    const { ctl, updater } = make();
    ctl.start();
    updater.emit("download-progress", { percent: 130 });
    expect(ctl.getState().percent).toBe(100);
    updater.emit("download-progress", { percent: -5 });
    expect(ctl.getState().percent).toBe(0);
  });

  it("已是最新", () => {
    const { ctl, updater } = make();
    ctl.start();
    updater.emit("update-not-available", {});
    expect(ctl.getState()).toMatchObject({ phase: "up-to-date", message: "已是最新版本" });
    expect(ctl.getState().version).toBeUndefined();
  });
});

describe("失败不打扰用户", () => {
  it("检查失败只记日志 + 状态置 error，不抛到外面", async () => {
    const { ctl, updater, logs } = make({ failWith: new Error("getaddrinfo ENOTFOUND github.com") });
    ctl.start();
    await vi.waitFor(() => expect(ctl.getState().phase).toBe("error"));
    expect(ctl.getState().message).toContain("网络或代理"); // 界面给人话
    // 原始错误码留在日志里，排障要看
    expect(logs.some((l) => l.includes("ENOTFOUND") && l.includes("检查失败"))).toBe(true);
    expect(updater.checks).toBe(1);
  });

  it("只有源设好了才去检查（设失败就不发请求）", () => {
    const { ctl, updater } = make();
    updater.setFeedURL = () => {
      throw new Error("boom");
    };
    ctl.start();
    expect(ctl.getState().phase).toBe("error");
    expect(updater.checks).toBe(0);
  });
});

describe("重启安装", () => {
  it("只有下载完才允许，且只能触发一次", () => {
    const { ctl, updater } = make();
    ctl.start();
    expect(ctl.quitAndInstall()).toBe(false); // 还没下载完

    updater.emit("update-downloaded", { version: "0.2.0" });
    expect(ctl.quitAndInstall()).toBe(true);
    expect(updater.installs).toEqual([[false, true]]); // 显示安装器 + 装完自动启动

    expect(ctl.quitAndInstall()).toBe(false); // 连点无效
    expect(updater.installs).toHaveLength(1);
  });
});
