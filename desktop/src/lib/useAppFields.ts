// 窄订阅辅助（T22 的延伸）：组件只订阅自己真正读的字段。
//
// 为什么需要：store 是**一个** zustand store，`useApp()` 不带选择器 = 订阅整个 state。
// 一个会话流式输出时每帧都会 `setState` 重建 `details`，于是所有全量订阅者
// （输入框、每个侧栏行、右侧面板……）都跟着重渲染 —— 用户看到的就是"别的会话
// 在跑，我这边打字都卡"。下面的 hook 按字段列表订阅，字段值不变就不重渲。
import { useShallow } from "zustand/react/shallow";
import { useApp } from "./store";

/**
 * 从 store 里挑几个字段返回（浅比较）。
 *
 * 用法：`const s = useAppFields("activeThreadId", "rpc", "send")`
 * —— 只在这几个字段变化时重渲染。
 */
export function useAppFields<K extends keyof AppState>(...keys: K[]): Pick<AppState, K> {
  return useApp(
    useShallow((s: AppState) => {
      const out = {} as Pick<AppState, K>;
      for (const k of keys) out[k] = s[k];
      return out;
    }),
  );
}

type AppState = ReturnType<typeof useApp.getState>;
