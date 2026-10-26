import { describe, expect, it } from "vitest";
import { commandFromText, commandText, hookCount, newHook, toggleAllow, validateHook } from "./hooks";

describe("hooks helpers", () => {
  it("formats and parses commands", () => {
    expect(commandText(["python", "C:\\My Hooks\\x.py", "a"])).toBe('python "C:\\My Hooks\\x.py" a');
    expect(commandText("echo hi | tee x")).toBe("echo hi | tee x");
    expect(commandFromText('python "C:\\My Hooks\\x.py"', true)).toEqual(["python", "C:\\My Hooks\\x.py"]);
    expect(commandFromText("echo  hi", false)).toBe("echo  hi");
  });

  it("toggles allow ids per file", () => {
    expect(toggleAllow(["aaaa:1111", "bbbb:2222"], "aaaa:3333", true)).toEqual(["bbbb:2222", "aaaa:3333"]);
    expect(toggleAllow(["aaaa:1111", "bbbb:2222"], "aaaa:1111", false)).toEqual(["bbbb:2222"]);
    expect(toggleAllow([], "cccc:1", true)).toEqual(["cccc:1"]);
  });

  it("validates hooks", () => {
    expect(validateHook(newHook())).toBe("命令不能为空");
    expect(validateHook({ ...newHook(), command: "x", timeout: 0 })).toMatch("超时");
    expect(validateHook({ ...newHook(), command: ["py", "a"], matcher: "shell|write" })).toBeNull();
    expect(validateHook({ ...newHook(), command: "x", matcher: "(write" })).toMatch("正则");
    expect(validateHook({ ...newHook(), command: "x", matcher: "^(write|edit)$" })).toBeNull();
    expect(hookCount({ enabled: true, allowed_project: [], events: { Stop: [newHook()], PreToolUse: [newHook(), newHook()] } })).toBe(3);
  });
});
