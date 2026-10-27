// 工具结果 details（后端 ee/tools/details.py）→ 界面文字。纯函数，便于测试。
import type { FileChange, ToolDetails, ToolItem } from "../types";

export function details(it: ToolItem): ToolDetails {
  return it.details ?? {};
}

/** 改文件类工具的文件列表（edit/write/apply_patch）。 */
export function changedFiles(it: ToolItem): FileChange[] {
  const d = details(it);
  const fs = d.files as FileChange[] | undefined;
  if (Array.isArray(fs) && fs.length && typeof fs[0] === "object") return fs;
  if (d.path) return [{ verb: d.verb ?? "Edited", path: d.path, added: d.added ?? 0, removed: d.removed ?? 0 }];
  return [];
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/** 浏览类工具（read/grep/glob）一行后面的补充说明。 */
export function exploreHint(it: ToolItem): string {
  const d = details(it);
  if (it.status !== "ok") return "";
  if (d.kind === "read") {
    if (d.ref) return "未改变";
    if (d.total_lines === 0) return "空文件";
    if (d.start && d.end && d.total_lines) {
      return d.start === 1 && d.end >= d.total_lines ? `${d.total_lines} 行` : `第 ${d.start}–${d.end} 行 / 共 ${d.total_lines} 行`;
    }
    return "";
  }
  if (d.kind === "search") {
    const n = (d.files as string[] | undefined)?.length ?? 0;
    return d.matches ? `${d.matches} 处 · ${n} 个文件` : "没有匹配";
  }
  if (d.kind === "list") {
    const total = d.total ?? (d.files as string[] | undefined)?.length;
    return total !== undefined ? `${total} 项` : "";
  }
  return "";
}

/** 给模型的输出被截断时的提示，如「输出过长：共 5,000 行 / 48.8 KB，模型只看到开头和结尾」。 */
export function truncationText(d: ToolDetails): string | null {
  const t = d.truncated;
  if (!t || typeof t !== "object") return null;
  return `输出过长：共 ${t.lines.toLocaleString()} 行 / ${formatBytes(t.bytes)}，模型只看到开头和结尾`;
}
