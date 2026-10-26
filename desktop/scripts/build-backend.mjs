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
