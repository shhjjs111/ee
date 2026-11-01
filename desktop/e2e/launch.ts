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

import { _electron as electron, type ElectronApplication } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export interface IsolatedApp {
  app: ElectronApplication;
  home: string;
  /** 关窗口 + 删临时目录。afterAll 里调用。 */
  dispose: () => Promise<void>;
}

export async function launchIsolated(): Promise<IsolatedApp> {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "ee-e2e-home-"));
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), "ee-e2e-userdata-"));
  const app = await electron.launch({
    args: ["."],
    cwd: path.resolve(__dirname, ".."),
    env: { ...process.env, EE_HOME: home, EE_TEST_USER_DATA: userData },
  });
  return {
    app,
    home,
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
