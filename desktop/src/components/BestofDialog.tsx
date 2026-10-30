// best-of-n 候选比较（T10-d 界面侧）：把「谁通过了验证」摆出来让人自己看。
// 纪律：胜者只由判据决定，模型自述不算；没有候选通过时说「没选」，不硬挑。
import { CheckCircle2, GitCompare, Trophy } from "lucide-react";
import { useEffect, useState } from "react";
import { useApp } from "../lib/store";
import type { BestofCandidate, WorktreeInfo } from "../types";
import { Button, Input, Spinner } from "./ui";
import { DialogFrame } from "./ui/DialogFrame";

interface PickResult {
  winner: string | null;
  candidates: BestofCandidate[];
  report: string;
  cleanup: { keep: string[]; remove: string[] };
}

export function BestofDialog() {
  const path = useApp((s) => s.bestofDialog);
  const rpc = useApp((s) => s.rpc);
  const s = useApp();
  const [wts, setWts] = useState<WorktreeInfo[] | null>(null);
  const [res, setRes] = useState<PickResult | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // 判据命令：候选之间比的就是「谁的判据真的通过」。没有判据就只能看，不能选。
  const [command, setCommand] = useState("");

  useEffect(() => {
    setWts(null);
    setRes(null);
    setErr(null);
    if (!path) return;
    let alive = true;
    rpc!
      .call("worktree/list", { cwd: path }, 60000)
      .then((r) => {
        if (alive) setWts((r as { worktrees: WorktreeInfo[] }).worktrees ?? []);
      })
      .catch((e) => {
        if (alive) setErr((e as Error).message);
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path]);

  if (!path) return null;
  const close = () => s.set({ bestofDialog: null });
  const enough = (wts?.length ?? 0) >= 2;

  /** 重新核对：读各候选当前代码状态，验证后改过的标成「证据已过期」（ACC-16）。 */
  const recheck = async () => {
    if (!res || !rpc) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await rpc.call("bestof/recheck", { candidates: res.candidates }, 120000);
      setRes(r as PickResult);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const compare = async () => {
    setBusy(true);
    setErr(null);
    try {
      // 候选来自 worktree；标题用分支名而不是目录名：用户认候选是看分支（ee/xxx），
      // 目录名常常是自动起的 a/b/c，看不出改了什么。
      const candidates = (wts ?? []).map((w) => ({
        id: w.name,
        title: w.branch || w.name,
        worktree: w.path,
        run_id: "",
        cost: 0,
      }));
      // 有判据就让后端为每个候选跑一次判据再选（bestof/verify）；
      // 没判据就退回「只看不选」——胜者只由判据决定，没判据不能硬挑。
      if (command.trim()) {
        const r = await rpc!.call("bestof/verify", {
          goal: { command: command.trim(), timeout: 300 },
          candidates,
        }, 600000);
        setRes(r as PickResult);
      } else {
        const r = await rpc!.call("bestof/pick", { candidates }, 60000);
        setRes(r as PickResult);
      }
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <DialogFrame title="比较候选" icon={<GitCompare size={15} />} onClose={close} width="md">
          <div className="mb-3 text-[12px] leading-relaxed text-muted">
            每个 worktree 是一个候选。胜者<span className="text-fg">只由判据决定</span>
            ——模型说「改好了」不算证据；没有候选通过验证时不会硬挑一个。
          </div>

          {err && (
            <div className="mb-2 whitespace-pre-wrap rounded-lg bg-del-bg px-3 py-2 text-[12.5px] text-danger">
              {err}
            </div>
          )}

          {wts === null && <Spinner />}

          {wts !== null && !enough && (
            <div className="rounded-lg bg-soft px-3 py-3 text-[12.5px] text-muted">
              至少两个 worktree 才能比较（现在 {wts.length} 个）。先在 Worktree 里新建几个候选。
            </div>
          )}

          {enough && (
            <>
              <div className="mb-2">
                <Input
                  value={command}
                  onChange={(e) => setCommand(e.target.value)}
                  placeholder="判据命令，例如 pytest -q（留空则只看不选）"
                  className="mono text-[12px]"
                />
                <div className="mt-1 text-[11px] leading-relaxed text-faint">
                  每个候选都跑一次这条命令。要求退出码为 0 且确实收集到测试：
                  零测试、超时都不算通过。只跑一次判据，不改候选代码。
                </div>
              </div>
              <div className="mb-2 flex items-center gap-2">
                <Button size="sm" variant="primary" disabled={busy} onClick={compare}>
                  {busy ? <Spinner size={12} /> : <GitCompare size={13} />}
                  {command.trim() ? `验证 ${wts!.length} 个候选` : `比较 ${wts!.length} 个候选`}
                </Button>
              </div>
              <div className="min-h-0 flex-1 overflow-y-auto rounded-lg border border-line">
                {(res?.candidates ?? wts!.map((w) => ({
                  id: w.name, title: w.branch || w.name, worktree: w.path, run_id: "", cost: 0,
                  verified: false, met: false, self_reported_ok: false, reason: "还没判定",
                }))).map((c: BestofCandidate) => {
                  const win = res?.winner === c.id;
                  return (
                    <div
                      key={c.id}
                      data-verified={c.verified ? "true" : "false"}
                      className="flex items-start gap-2 border-b border-line px-3 py-2 text-[12px] last:border-0"
                    >
                      <span className="mt-0.5 w-4 shrink-0">
                        {win ? (
                          <Trophy size={13} className="text-accent" />
                        ) : c.verified ? (
                          <CheckCircle2 size={13} className="text-ok" />
                        ) : (
                          <span className="block h-3 w-3 rounded-full border border-line" />
                        )}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-fg">
                          {c.title || c.id}
                          {win && <span className="ml-1.5 text-[11px] text-accent">胜者</span>}
                          {c.stale && (
                            <span className="ml-1.5 text-[11px] text-warn" title="验证之后代码又改过，之前的判据结果证明的是旧代码">
                              证据已过期
                            </span>
                          )}
                          {!c.verified && !c.stale && (
                            <span className="ml-1.5 text-[11px] text-faint">
                              未验证{c.self_reported_ok ? "（仅自述通过）" : ""}
                            </span>
                          )}
                        </span>
                        <span className="mono mt-0.5 block truncate text-[11px] text-faint" title={c.worktree}>
                          {c.worktree}
                        </span>
                        {res && <span className="mt-0.5 block text-[11px] text-muted">{c.reason}</span>}
                      </span>
                    </div>
                  );
                })}
              </div>
              {res && !res.winner && (
                <div className="mt-2 rounded-lg bg-soft px-3 py-2 text-[12px] text-muted">
                  没有候选通过验证，不选。模型自述不算证据。
                </div>
              )}
              {res && res.candidates?.length > 0 && (
                <div className="mt-2 flex items-center gap-2">
                  <Button variant="outline" size="sm" disabled={busy} onClick={() => void recheck()}>
                    重新核对
                  </Button>
                  <span className="text-[11.5px] text-faint">
                    重新读各候选的代码状态：验证后改过的会标成「证据已过期」
                  </span>
                </div>
              )}
              {res?.winner && (
                <div className="mt-2 text-[11.5px] text-faint">
                  落选者默认保留（worktree 不删），由你决定怎么处理。
                </div>
              )}
            </>
          )}
    </DialogFrame>
  );
}
