import { describe, expect, it } from "vitest";
import { formatArgs, formatKv, parseArgs, parseKv, splitCommandLine } from "./mcp";

describe("mcp helpers", () => {
  it("parses args with quotes and windows paths", () => {
    expect(parseArgs('-y @modelcontextprotocol/server-filesystem "C:\\My Docs" D:\\code')).toEqual([
      "-y",
      "@modelcontextprotocol/server-filesystem",
      "C:\\My Docs",
      "D:\\code",
    ]);
    expect(parseArgs("  a   b ")).toEqual(["a", "b"]);
    expect(parseArgs('x ""')).toEqual(["x", ""]);
    expect(parseArgs("")).toEqual([]);
  });
  it("round-trips args", () => {
    const a = ["-y", "C:\\My Docs", "", 'say "hi"'];
    expect(parseArgs(formatArgs(a))).toEqual(a);
  });
  it("parses kv", () => {
    expect(parseKv("A=1\n# c\n\nB = x=y\nbad", "=")).toEqual({ A: "1", B: "x=y" });
    expect(parseKv("Authorization: Bearer t\nX-A:b", ":")).toEqual({ Authorization: "Bearer t", "X-A": "b" });
    expect(formatKv({ A: "1" }, "=")).toBe("A=1");
    expect(formatKv({ A: "1" }, ":")).toBe("A: 1");
  });
  it("splits a full command line", () => {
    expect(splitCommandLine("npx -y server")).toEqual({ command: "npx", args: ["-y", "server"] });
    expect(splitCommandLine('"C:\\Program Files\\x.exe" --a')).toEqual({ command: "C:\\Program Files\\x.exe", args: ["--a"] });
  });
});
