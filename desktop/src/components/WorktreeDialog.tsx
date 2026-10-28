// git worktree 管理（D5-1）：列出项目的 ee worktree，新建、在其中开对话、合并回主目录、删除。
import * as Dialog from "@radix-ui/react-dialog";
import { GitBranch, GitMerge, Plus, SquarePen, Trash2, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { relTime } from "../lib/reduce";
import { useApp } from "../lib/store";
import type { WorktreeInfo } from "../types";
import { Button, Input, Spinner, Tip } from "./ui";

export function WorktreeDialog() {
  const path = useApp((s) => s.worktreeDialog);
  const rpc = useApp((s) => s.rpc);
  const s = useApp();
  const [repo, setRepo] = useState<string | null | undefined>(undefined);
  const [list, setList] = useState<WorktreeInfo[]>([]);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<{ wt: WorktreeInfo; branch: boolean; force: boolean; discard: boolean } | null>(null);

  const current = useRef(path);
  current.current = path;
  const load = async () => {
    if (!path) return;
    const asked = path;
    try {
      const r = await rpc!.call("worktree/list", { cwd: asked }, 60000);
      if (current.current !== asked) return; // 已切到别的项目：丢弃迟到的旧结果
      setRepo(r.repo);
      setList(r.worktrees);
    } catch (e) {
      if (current.current === asked) setErr((e as Error).message);
    }
  };
  useEffect(() => {
    setRepo(undefined);
    setErr(null);
    setConfirm(null);
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path]);
  if (!path) return null;
  const close = () => s.set({ worktreeDialog: null });

  const run = async (key: string, fn: () => Promise<unknown>, ok?: string) => {
    setBusy(key);
    setErr(null);
    try {
      await fn();
      if (ok) s.showToast(ok);
      await load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const openThread = (wt: WorktreeInfo) => {
    close();
    s.newThread(wt.path);
  };

  return (
    <Dialog.Root open onOpenChange={(o) => !o && close()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/30 fadein" />
        <Dialog.Content
          aria-describedby={undefined}
          className="fixed left-1/2 top-[10%] z-50 flex max-h-[78vh] w-[640px] max-w-[94vw] -translate-x-1/2 flex-col rounded-2xl border border-line bg-elev p-4 shadow-[var(--shadow)] fadein"
        >
          <div className="mb-1 flex items-center gap-2">
            <GitBranch size={15} className="text-muted" />
            <Dialog.Title className="flex-1 text-[14px] font-semibold text-fg">Worktree</Dialog.Title>
            <Dialog.Close asChild>
              <Button size="icon-sm" aria-label="关闭">
                <X size={14} />
              </Button>
            </Dialog.Close>
          </div>
          <div className="mb-3 text-[12px] leading-relaxed text-muted">
            每个 worktree 是独立的目录和 <span className="mono">ee/</span> 分支，ee 在里面改代码不影响主目录，适合同时做几件事。仓库根目录的
            <span className="mono"> .worktreeinclude </span>（.gitignore 语法）里列出的被忽略文件（如 .env）会在新建时复制过去。删除前有未提交的改动会先自动提交到它的分支上。
          </div>
          {err && <div className="mb-2 whitespace-pre-wrap rounded-lg bg-del-bg px-3 py-2 text-[12.5px] text-danger">{err}</div>}
          {repo === undefined ? (
            <Spinner />
          ) : repo === null ? (
            <div className="py-6 text-center text-[12.5px] text-faint">这个项目不是 git 仓库，不能使用 worktree。</div>
          ) : (
            <>
              <div className="mb-3 flex items-center gap-2">
                <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="新 worktree 名字（可留空自动起名）" className="flex-1" />
                <Button
                  size="sm"
                  variant="primary"
                  disabled={!!busy}
                  onClick={() =>
                    run("create", async () => {
                      await rpc!.call("worktree/create", { cwd: path, name: name.trim() || undefined }, 180000);
                      setName("");
                    }, "已新建 worktree")
                  }
                >
                  {busy === "create" ? <Spinner size={12} /> : <Plus size={13} />} 新建
                </Button>
              </div>
              <div className="min-h-0 flex-1 space-y-1.5 overflow-y-auto">
                {list.length === 0 && <div className="py-4 text-center text-[12.5px] text-faint">还没有 worktree</div>}
                {list.map((wt) => {
                  const dirty = (wt.changed ?? 0) + (wt.untracked ?? 0);
                  return (
                    <div key={wt.path} className="rounded-xl border border-line px-3 py-2.5">
                      <div className="flex items-center gap-2">
                        <GitBranch size={13} className="shrink-0 text-muted" />
                        <span className="mono min-w-0 flex-1 truncate text-[13px] text-fg" title={wt.path}>
                          {wt.branch}
                        </span>
                        <span className="shrink-0 text-[11.5px] text-faint">{relTime(wt.created)}</span>
                      </div>
                      <div className="mt-1 text-[12px] text-muted">
                        {wt.exists === false
                          ? "目录已不存在"
                          : `${wt.commits ?? 0} 个提交 · ${dirty ? `${dirty} 个未提交改动` : "没有未提交改动"}`}
                        {wt.in_use && <span className="ml-2 text-accent">{wt.in_use}</span>}
                      </div>
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        <Button size="sm" variant="outline" disabled={!!busy || wt.exists === false} onClick={() => openThread(wt)}>
                          <SquarePen size={12} /> 在这里新对话
                        </Button>
                        <Tip label="把这个分支合并进主目录当前分支（保留提交历史；先自动提交未提交的改动）">
                          <Button size="sm" variant="outline" disabled={!!busy || !!wt.in_use} onClick={() => run("m" + wt.path, () => rpc!.call("worktree/merge", { path: wt.path }, 180000), "已合并到主目录")}>
                            <GitMerge size={12} /> 合并
                          </Button>
                        </Tip>
                        <Tip label="把全部改动压成一份放进主目录的暂存区，由你自己检查、提交">
                          <Button size="sm" variant="ghost" disabled={!!busy || !!wt.in_use} onClick={() => run("q" + wt.path, () => rpc!.call("worktree/merge", { path: wt.path, squash: true }, 180000), "改动已放进主目录暂存区")}>
                            压成一份
                          </Button>
                        </Tip>
                        <div className="flex-1" />
                        <Button size="sm" variant="ghost" disabled={!!busy || !!wt.in_use} onClick={() => setConfirm({ wt, branch: false, force: false, discard: false })}>
                          <Trash2 size={12} /> 删除
                        </Button>
                      </div>
                      {confirm?.wt.path === wt.path && (
                        <div className="mt-2 space-y-1.5 rounded-lg bg-soft px-3 py-2 text-[12.5px]">
                          <label className="flex items-center gap-2">
                            <input type="checkbox" checked={confirm.branch} onChange={(e) => setConfirm({ ...confirm, branch: e.target.checked })} />
                            同时删除分支 <span className="mono">{wt.branch}</span>
                          </label>
                          {confirm.branch && (
                            <label className="flex items-center gap-2 text-danger">
                              <input type="checkbox" checked={confirm.force} onChange={(e) => setConfirm({ ...confirm, force: e.target.checked })} />
                              分支有未合并的提交也删除（找不回来）
                            </label>
                          )}
                          <label className="flex items-center gap-2 text-danger">
                            <input type="checkbox" checked={confirm.discard} onChange={(e) => setConfirm({ ...confirm, discard: e.target.checked })} />
                            忽略文件没能备份时也删除（那些文件找不回来）
                          </label>
                          <div className="flex justify-end gap-2">
                            <Button size="sm" variant="ghost" onClick={() => setConfirm(null)}>
                              取消
                            </Button>
                            <Button
                              size="sm"
                              variant="danger"
                              disabled={!!busy}
                              onClick={() =>
                                run("d" + wt.path, async () => {
                                  const r = await rpc!.call("worktree/remove", { path: wt.path, delete_branch: confirm.branch, force: confirm.force, discard_unbacked: confirm.discard }, 180000);
                                  setConfirm(null);
                                  const lost = r?.backup_skipped?.length ?? 0;
                                  s.showToast(
                                    lost
                                      ? `已删除 worktree，但有 ${lost} 个被 git 忽略的文件没备份就被丢弃了`
                                      : r?.ignored_backup
                                        ? `已删除 worktree。里面 ${r.backed_up?.length ?? 0} 个被 git 忽略的文件（.env 等）已备份到 ${r.ignored_backup}`
                                        : "已删除 worktree",
                                    lost ? "error" : "info",
                                  );
                                })
                              }
                            >
                              确认删除
                            </Button>
                          </div>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
