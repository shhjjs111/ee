// Build the Python backend with PyInstaller and stage it for electron-builder.
// Usage: node scripts/build-backend.mjs   (python from EE_PYTHON, else `python` / `python3`)
// Output: desktop/resources/backend/ee-backend/ee-backend[.exe]
import { spawnSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const desktopDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = path.resolve(desktopDir, "..");
const spec = path.join(repoRoot, "ee", "packaging", "ee-backend.spec");
const outDir = path.join(desktopDir, "resources", "backend", "ee-backend");
const exeName = process.platform === "win32" ? "ee-backend.exe" : "ee-backend";

function pickPython() {
  if (process.env.EE_PYTHON) return process.env.EE_PYTHON;
  // 仓库根的 .venv 优先：项目约定就是它（AGENTS.md「必须用仓库根的 .venv」）。
  // PATH 上的 python 往往是另一个环境——本机实测是 Anaconda，没有 pyinstaller，
  // 于是本地 `npm run dist:win:full` 每次都在这一步失败，而 CI 因为用的是系统
  // python（装了 pyinstaller）反而是绿的，本地与 CI 的行为于是对不上。
  const venv = path.join(repoRoot, ".venv",
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  if (existsSync(venv)) {
    const ok = spawnSync(venv, ["-c", "import PyInstaller"], { stdio: "ignore" }).status === 0;
    if (ok) return venv;
    throw new Error(
      `仓库根的 .venv 里没有 PyInstaller：${venv}\n` +
      `  装一下：${venv} -m pip install pyinstaller\n` +
      `  或者用 EE_PYTHON 指定一个装好 pyinstaller 的解释器`,
    );
  }
  for (const c of process.platform === "win32" ? ["python", "py"] : ["python3", "python"]) {
    const r = spawnSync(c, ["--version"], { stdio: "ignore" });
    if (r.status === 0) return c;
  }
  throw new Error("No Python found; set EE_PYTHON to a Python with ./ee and pyinstaller installed");
}

const python = pickPython();
const tmp = mkdtempSync(path.join(tmpdir(), "ee-pyi-"));
const dist = path.join(tmp, "dist");
console.log(`[build-backend] ${python} -m PyInstaller ${spec}`);
const r = spawnSync(
  python,
  ["-m", "PyInstaller", spec, "--noconfirm", "--clean", "--distpath", dist, "--workpath", path.join(tmp, "build")],
  { stdio: "inherit", cwd: tmp },
);
if (r.status !== 0) {
  rmSync(tmp, { recursive: true, force: true });
  console.error("[build-backend] PyInstaller failed");
  process.exit(r.status ?? 1);
}
const built = path.join(dist, "ee-backend");
if (!existsSync(path.join(built, exeName))) {
  console.error(`[build-backend] expected ${path.join(built, exeName)} not found`);
  process.exit(1);
}
rmSync(outDir, { recursive: true, force: true });
mkdirSync(path.dirname(outDir), { recursive: true });
cpSync(built, outDir, { recursive: true });
rmSync(tmp, { recursive: true, force: true });
console.log(`[build-backend] staged -> ${path.relative(desktopDir, outDir)}/${exeName}`);
