// 自动更新（electron-updater + GitHub Releases）。
//
// 设计取舍：
//
// 1. **绝不自动重启**。`autoUpdater.quitAndInstall()` 会在用户正干活时掐掉会话、
//    杀掉后台命令和终端。更新是锦上添花，不能拿用户正在跑的东西换。所以这里只
//    下载，下载完把「重启安装」交给用户点。
// 2. **只在打包版启动检查**。开发态没有 latest.yml，检查必然失败，日志里全是
//    噪音；`app.isPackaged` 为 false 时直接不启用。
// 3. **失败不打扰用户**。网络不通、代理没配、GitHub 被墙都太常见。检查失败只在
//    日志里留一行，界面不弹任何东西；只有「发现新版本」和「下载完成」才通知。
// 4. **UI 通过 IPC 拉状态，不靠事件堆积**。渲染进程一进来就 `getState()` 拿当前
//    快照，之后靠 `onState` 收增量。这样即使窗口是后打开的，也不会有状态错位。
//
// 为什么状态要自己维护一份：electron-updater 把进度/错误都塞在 events 和
// `autoUpdater` 的实例字段里（`updateInfo`、`downloadProgress`），版本之间变过好几次。
// 自己维护一个只含「界面要用的字段」的纯数据对象，渲染层就不用跟着它的内部结构跑。
import type { AppUpdater, UpdateInfo } from "electron-updater";

/** 给界面的更新状态（全字段可 JSON 序列化，走 IPC）。 */
export type UpdateState = {
  /** 当前阶段。idle=还没检查 / checking / available / downloading / downloaded / up-to-date / error */
  phase: "idle" | "checking" | "available" | "downloading" | "downloaded" | "up-to-date" | "error";
  /** 新版本号（有更新时）。 */
  version?: string;
  /** 下载进度 0-100。 */
  percent?: number;
  /** 一句话说明（错误原因 / 已是最新等），直接给用户看。 */
  message?: string;
  /** 当前安装的版本。 */
  current: string;
};

export type UpdaterDeps = {
  /** 当前版本，通常是 app.getVersion()。 */
  currentVersion: string;
  /** 是否打包版。false 时整个模块不启用（开发态没有 latest.yml）。 */
  packaged: boolean;
  /** 注入 electron-updater 的 autoUpdater（测试用假实现）。 */
  updater: AppUpdater;
  /** 把状态推给界面。 */
  emit: (s: UpdateState) => void;
  /** 日志。默认不输出，调用方接 backend.appendLog。 */
  log?: (line: string) => void;
};

/**
 * 更新源：必须与 `desktop/package.json` 的 `build.publish[0]` 一致。
 * 写死在代码里，而不是读磁盘上的 app-update.yml —— 见 pinFeed() 的说明。
 */
export const FEED = { provider: "github", owner: "shhjjs111", repo: "ee" } as const;

/** Release 页地址（界面上「打开发布页」用，和更新源同源）。 */
export function releaseUrl(): string {
  return `https://github.com/${FEED.owner}/${FEED.repo}/releases`;
}

/** 日志里单个字段的上限：完整 XML 不截会把 backend.log 灌满。 */
function cap(s: string, max: number): string {
  return s.length > max ? `${s.slice(0, max)}…` : s;
}

/**
 * 把 electron-updater 的原始报错洗成一句人话。
 *
 * 它的错误里会原样带上整份 Atom feed / 完整 release notes（实测几 KB 的 HTML），
 * 直接丢给界面会把「关于」页那个 text-danger 撑爆。这里只留**第一行**并截断，
 * 同时把最常见的两种失败翻成人话：
 * - 仓库里只有 Pre-release → GitHub 的 `/releases/latest` 是 404，它却报成 406/解析失败
 * - 网络不通 → 保留原始错误码，方便用户自己判断是不是代理问题
 */
export function friendlyError(e: unknown): string {
  const raw = (e as Error)?.message ?? String(e);
  // 先把 XML/HTML 开标签剥掉：报错第一行里就塞着整份 feed（实测 "Cannot parse releases feed: <feed ..."）
  const noMarkup = raw.replace(/<[^>]*>/g, " ");
  const head = noMarkup.split(/\r?\n/)[0].replace(/\s+/g, " ").trim();
  // /releases/latest 找不到：GitHub 上还没有正式 Release（只有 Pre-release / draft 也会这样）
  if (/latest version on GitHub/i.test(head) || /\b40[46]\b/.test(head) || /parse releases feed/i.test(head)) {
    return "还没有正式发布版本（GitHub 上的 Release 可能仍是预发布）";
  }
  if (/ENOTFOUND|EAI_AGAIN|ETIMEDOUT|ECONNREFUSED|socket hang up|net::ERR/i.test(head)) {
    return "连不上 GitHub，请检查网络或代理";
  }
  if (!head) return "检查更新失败（原因不明）";
  // 其余保留原意，但限长：界面只有一行位置
  return head.length > 200 ? `${head.slice(0, 200)}…` : head;
}

export class UpdateController {
  private state: UpdateState;
  private readonly d: UpdaterDeps;
  /** 只让「下载 → 重启安装」这条路走一次，避免用户连点。 */
  private installing = false;
  /** 更新源是否已经钉死（见 pinFeed）。 */
  private pinned = false;

  constructor(deps: UpdaterDeps) {
    this.d = deps;
    this.state = { phase: "idle", current: deps.currentVersion };
  }

  getState(): UpdateState {
    return this.state;
  }

  private set(patch: Partial<UpdateState>) {
    this.state = { ...this.state, ...patch };
    this.d.emit(this.state);
  }

  private log(line: string) {
    this.d.log?.(line);
  }

  /**
   * 把更新源钉死在本仓库。
   *
   * 这里不能用 `getFeedURL()` 校验：electron-updater 6.8.11 把它废弃成了
   * `return "Deprecated. Do not use it."`（见 out/AppUpdater.js），拿它判断来源
   * 会让每一次检查都被拒掉——线上就是「检查更新永远失败」。
   * 改成主动 `setFeedURL`：地址由代码决定，安装目录里被替换的 app-update.yml
   * 只剩 updaterCacheDirName 还能生效，改不了去哪里下载。
   * 只在第一次调用时设，避免每次检查都重置内部 provider。
   */
  private pinFeed(): boolean {
    if (this.pinned) return true;
    try {
      this.d.updater.setFeedURL({ provider: FEED.provider, owner: FEED.owner, repo: FEED.repo });
      this.pinned = true;
      return true;
    } catch (e) {
      const msg = cap((e as Error)?.message ?? String(e), 800);
      this.log(`[更新] 更新源配置失败：${msg}`);
      this.set({ phase: "error", message: friendlyError(e) });
      return false;
    }
  }

  /** 接好事件并做首次检查。开发态直接返回（不注册任何东西）。 */
  start(): void {
    if (!this.d.packaged) {
      this.log("[更新] 开发态，不启用自动更新");
      return;
    }
    if (!this.pinFeed()) return;
    const u = this.d.updater;
    // 只下载、不自动装：见文件头第 1 条。
    u.autoDownload = true;
    u.autoInstallOnAppQuit = false;

    u.on("checking-for-update", () => {
      this.set({ phase: "checking", message: undefined });
    });
    u.on("update-available", (info: UpdateInfo) => {
      this.log(`[更新] 发现新版本 ${info.version}（当前 ${this.state.current}）`);
      // electron-updater 会紧接着自动下载，这里先标 available，进度事件会覆盖
      this.set({ phase: "available", version: info.version, percent: 0, message: `发现新版本 ${info.version}` });
    });
    u.on("update-not-available", () => {
      this.set({ phase: "up-to-date", version: undefined, message: "已是最新版本" });
    });
    u.on("download-progress", (p: { percent: number }) => {
      // 限到一位小数：每帧都发 IPC 会白占渲染线程
      const percent = Math.max(0, Math.min(100, Math.round(p.percent * 10) / 10));
      this.set({ phase: "downloading", percent });
    });
    u.on("update-downloaded", (info: UpdateInfo) => {
      this.log(`[更新] ${info.version} 下载完成，等用户重启`);
      this.set({ phase: "downloaded", version: info.version, percent: 100, message: `新版本 ${info.version} 已下载，重启后生效` });
    });
    u.on("error", (e: Error) => {
      // 不打扰用户：网络问题太常见。日志留全量（排障要），但限长避免把 backend.log 灌满
      this.log(`[更新] 检查失败：${cap((e as Error)?.message ?? String(e), 800)}`);
      this.set({ phase: "error", message: friendlyError(e) });
    });

    void this.check();
  }

  /** 手动检查（界面上的「检查更新」按钮）。 */
  async check(): Promise<UpdateState> {
    if (!this.d.packaged) {
      this.set({ phase: "error", message: "开发版不支持自动更新（只有安装版才有 latest.yml）" });
      return this.state;
    }
    if (!this.pinFeed()) return this.state;
    try {
      await this.d.updater.checkForUpdates();
    } catch (e) {
      this.log(`[更新] checkForUpdates 抛错：${cap((e as Error)?.message ?? String(e), 800)}`);
      this.set({ phase: "error", message: friendlyError(e) });
    }
    return this.state;
  }

  /** 用户点「重启并安装」。没下载完时什么都不做（返回 false）。 */
  quitAndInstall(): boolean {
    if (this.state.phase !== "downloaded" || this.installing) return false;
    this.installing = true;
    // isSilent=false、isForceRunAfter=true：正常显示安装器，装完自动把 ee 拉起来
    this.d.updater.quitAndInstall(false, true);
    return true;
  }
}
