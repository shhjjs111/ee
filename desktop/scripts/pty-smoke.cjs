// CI 冒烟：node-pty 能加载（N-API 预编译，Electron 里同一个 .node 可用）并真的跑起一个伪终端。
const pty = require("node-pty");
const win = process.platform === "win32";
const t = pty.spawn(win ? "cmd.exe" : "sh", win ? ["/c", "echo ee-pty-ok"] : ["-c", "echo ee-pty-ok"], {
  cols: 80,
  rows: 24,
  cwd: process.cwd(),
  env: process.env,
});
let out = "";
t.onData((d) => {
  out += d;
  if (out.includes("ee-pty-ok")) {
    console.log("node-pty OK");
    setTimeout(() => process.exit(0), 200);
  }
});
setTimeout(() => {
  console.error("node-pty smoke timeout; output:", JSON.stringify(out));
  process.exit(1);
}, 20000);
