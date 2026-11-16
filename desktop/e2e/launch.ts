// e2e 统一的隔离启动：后端 home（EE_HOME）与 Electron userData 都指向临时目录。
//
// 为什么必须隔离（两条都是实测踩出来的）：
// 1. 不隔离 userData：ee 桌面端正开着时，第二个实例的 disk cache 被锁（"拒绝访问"），
//    再加上 requestSingleInstanceLock，electron.launch 直接报 "Target page ... closed"。
//    日常开发里 ee 基本一直在跑，所以这类失败是常态而不是偶发。
// 2. 不隔离 EE_HOME：spec 里的 settings/set、workspace/open|remove 会写进用户真实的
//    ~/.ee/config.toml 和 desktop.json。实测把真实配置的 default_model 改成了假模型
//    （fake-gpt → 127.0.0.1），workspace/remove 还会隐藏用户真实项目。
//
// 每个 spec 用自己的临时目录，跑完删掉；断言只针对自己造的数据。
//
// `opts.configToml` / `opts.extraEnv` 只给"真模型"spec 用：临时 home 里写一份**不含密钥**
// 的配置（provider/model/base_url/budget），真实 key 由后端在运行时从系统凭据库解析
// （`config.key_source()` 先查 store，与 EE_HOME 无关）。默认调用行为与以前完全一致。

import { _electron as electron, type ElectronApplication } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export interface IsolatedApp {
  app: ElectronApplication;
  home: string;
  /** 本次实例的 Electron userData（临时目录；logs 也在它下面）。 */
  userData: string;
  /** 关窗口 + 删临时目录。afterAll 里调用。 */
  dispose: () => Promise<void>;
}

export interface LaunchOptions {
  /** 写进临时 home 的 config.toml。**不得**包含 api_key 等敏感字段。 */
  configToml?: string;
  /** 额外环境变量（同样不要放密钥：真实 key 走系统凭据库）。 */
  extraEnv?: Record<string, string>;
}

export async function launchIsolated(opts: LaunchOptions = {}): Promise<IsolatedApp> {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "ee-e2e-home-"));
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), "ee-e2e-userdata-"));
  if (opts.configToml) {
    fs.writeFileSync(path.join(home, "config.toml"), opts.configToml, "utf8");
  }
  // extraEnv 不能覆盖隔离键：否则一个手滑的调用就能把临时实例指回真实 home/userData。
  const extra: Record<string, string> = { ...(opts.extraEnv || {}) };
  for (const k of ["EE_HOME", "EE_TEST_USER_DATA", "EE_LOG_DIR"]) delete extra[k];
  const app = await electron.launch({
    args: ["."],
    cwd: path.resolve(__dirname, ".."),
    env: { ...process.env, ...extra, EE_HOME: home, EE_TEST_USER_DATA: userData },
  });
  return {
    app,
    home,
    userData,
    dispose: async () => {
      await app.close().catch(() => undefined);
      for (const d of [home, userData]) {
        try {
          fs.rmSync(d, { recursive: true, force: true });
        } catch {
          /* Windows 上偶发占用，删不掉也不影响断言 */
        }
      }
    },
  };
}
