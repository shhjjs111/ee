"""全力以赴（allout）的火力推导（allout 阶段 2）。

**这个模块只做纯计算**：给定「模型档案 + 机器 + 项目有没有判据」，算出这次
allout 该用几路、修几轮、强度拉到哪一档，并给出一句人话解释。不创建会话、
不跑命令、不碰磁盘——那些在 `server/threads.py` 的编排层。

三条不可协商的约束（见 `research/plans/2026-11-09-allout-v2.md`）：

1. **模型不换。** 这里只读 `profile` 的字段，没有任何换模型或换供应商的入口。
2. **没有判据就没有「极限」。** `has_goal=False` 时强制单路、零修复，结果标注未验证。
3. **上限来自模型与机器。** 每个取值都由 `profile` / CPU 数 / 配置钳制出来，
   没有一处写死的「并行 N 路」。`reasons` 必须能向用户解释每一个取值。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# 候选温度阶梯（ultramode 实测的 0 / 0.3 / 0.6 / 0.9 / 1.1）。分叉时按顺序取前 N 个。
TEMPERATURE_LADDER = (0.0, 0.3, 0.6, 0.9, 1.1)

# 并行路数的硬上界。依据：Stroebl 2024——假阳性率给重采样设死上限，最优采样次数
# 常小于 10；3 路之后的边际收益在验证者论文里只有 1.5pp（3→5）。
CANDIDATES_CAP = 3
# 上下文门槛：低于 128k 时连一份候选的完整历史都放不下，不做并行。
CONTEXT_MIN_FOR_PARALLEL = 128_000
# 每个候选吃 4 核的分母。依据是本项目自己的实测：16 路并行跑测试（`-n auto`）
# 会红 2 条，8 路（`-n 8`）全绿——判据命令本身吃 CPU，超载产出假失败。
CORES_PER_CANDIDATE = 4
# 强度档的高低顺序（`reasoning.LEVELS` 里可调的档位）。
_EFFORT_ORDER = ("low", "medium", "high", "max")


@dataclass(frozen=True)
class AlloutPlan:
    """一次 allout 的火力配置。所有字段都必须能向用户解释。"""
    candidates: int = 1            # 分叉路数（1 = 不分叉）
    repair_rounds: int = 0         # 批评修复轮数（0/1/2）
    max_rounds: int = 1            # 判据判定的总轮数（goal 的 max_rounds）
    effort: str = ""               # 新会话应使用的推理强度档（"" = 该模型不支持/按默认）
    max_steps: int = 50            # 单个候选的步数预算
    temperature_ladder: tuple = (0.0,)   # 每个候选的 temperature
    verified: bool = False         # 是否有客观判据
    reasons: tuple = ()            # 界面直接显示
    degradations: tuple = ()       # 降级原因（空 = 无降级）

    def as_dict(self) -> dict:
        return {"candidates": self.candidates, "repair_rounds": self.repair_rounds,
                "max_rounds": self.max_rounds, "effort": self.effort, "max_steps": self.max_steps,
                "temperature_ladder": list(self.temperature_ladder), "verified": self.verified,
                "reasons": list(self.reasons), "degradations": list(self.degradations)}

    @property
    def degraded(self) -> bool:
        return bool(self.degradations)


def max_effort(profile) -> str:
    """这个模型能给的最高强度档；不支持调强度时返回 ""（不发参数）。"""
    if not getattr(profile, "effort_param", ""):
        return ""
    levels = [str(x) for x in (getattr(profile, "effort_levels", None) or ("low", "medium", "high"))]
    levels = [x for x in levels if x in _EFFORT_ORDER]
    if not levels:
        return ""
    return max(levels, key=_EFFORT_ORDER.index)


def _ladder(profile, n: int) -> tuple:
    """候选温度。模型档案显式设了 temperature 时以它为首项，其余按阶梯顺延。"""
    base = getattr(profile, "temperature", None)
    seq = list(TEMPERATURE_LADDER)
    if base is not None:
        seq = [float(base)] + [t for t in seq if abs(t - float(base)) > 1e-9]
    return tuple(seq[:max(1, n)])


def derive(*, config: dict, profile, has_goal: bool, cpu_count: int | None = None,
           max_parallel: int | None = None, max_candidates: int | None = None) -> AlloutPlan:
    """推导一次 allout 的火力。`config` 是已加载的配置字典（取 max_steps 等）。

    `max_candidates` 是用户设的上限（界面开关旁的「最多几路」）。它参与钳制而不是
    事后截断——否则 `reasons` 会写「并行 3 路」而实际只跑 2 路，界面自相矛盾
    （2026-11-09 真模型演练实测到）。
    """
    reasons: list[str] = []
    degr: list[str] = []
    cfg = config or {}
    try:
        base_steps = int(cfg.get("max_steps") or 50)
    except (TypeError, ValueError):
        base_steps = 50
    base_steps = max(1, base_steps)

    effort = max_effort(profile)
    if effort:
        reasons.append(f"推理强度：{effort}（该模型支持的最高档，新会话生效）")
    else:
        reasons.append("推理强度：该模型不支持调节，按默认档")

    if not has_goal:
        degr.append("没有探测到可执行的测试命令：只能单路尝试，结果标注「未验证」。"
                    "可以在「目标」里填一条命令启用完整模式。")
        return AlloutPlan(candidates=1, repair_rounds=0, max_rounds=1, effort=effort,
                          max_steps=base_steps, temperature_ladder=(0.0,), verified=False,
                          reasons=tuple(reasons), degradations=tuple(degr))

    # ---- 并行路数：min(机器, 上下文, 闸门, 硬上界)
    n_cpu = int(cpu_count) if cpu_count is not None else (os.cpu_count() or 1)
    n_cpu = max(1, n_cpu)
    by_cpu = max(1, min(6, n_cpu // CORES_PER_CANDIDATE))
    ctx = int(getattr(profile, "context_window", 0) or 0)
    by_ctx = CANDIDATES_CAP if ctx >= CONTEXT_MIN_FOR_PARALLEL else 1
    gate = int(max_parallel) if max_parallel is not None else int(
        (cfg.get("subagent") or {}).get("max_parallel") or 3)
    gate = max(1, gate)
    user_cap = CANDIDATES_CAP if max_candidates is None else max(1, int(max_candidates))
    candidates = max(1, min(by_cpu, by_ctx, gate, CANDIDATES_CAP, user_cap))

    if candidates == 1:
        if by_cpu < 2:
            reasons.append(f"并行 1 路：CPU {n_cpu} 核，按每路 {CORES_PER_CANDIDATE} 核算不足以并行")
        elif by_ctx < 2:
            reasons.append(f"并行 1 路：上下文 {ctx // 1000}k，低于 {CONTEXT_MIN_FOR_PARALLEL // 1000}k 不做并行")
        elif gate < 2:
            reasons.append(f"并行 1 路：并发闸门限制为 {gate}")
        else:
            reasons.append(f"并行 1 路：你把上限设成了 {user_cap}")
    else:
        reasons.append(f"并行 {candidates} 路：CPU {n_cpu} 核 / 上下文 {ctx // 1000}k / "
                       f"闸门 {gate} / 上限 {min(CANDIDATES_CAP, user_cap)}")

    # ---- 轮数与修复：修复优先于并行（预算不是配额）
    repair_rounds = 1
    max_rounds = 2 if candidates == 1 else 3
    reasons.append(f"修复 {repair_rounds} 轮后仍失败才分叉；一次通过则零额外成本")

    ladder = _ladder(profile, candidates)
    return AlloutPlan(candidates=candidates, repair_rounds=repair_rounds, max_rounds=max_rounds,
                      effort=effort, max_steps=base_steps, temperature_ladder=ladder,
                      verified=True, reasons=tuple(reasons), degradations=tuple(degr))


# ---------------------------------------------------------------- keep-policy（修复只在更好时保留）


def keep_policy(before, after) -> tuple[bool, str]:
    """批评修复的保留判定（ultramode 的 safe-union）：**只在严格更好时保留**。

    `before` / `after` 是 `goal.Verdict`（before 可为 None = 没有胜者）。
    规则：修复必须通过判据；并且通过的测试数不少于修复前。
    不满足就丢弃修复、保留原胜者——修复永远不能让结果变差。
    """
    if after is None or not getattr(after, "met", False):
        return False, "修复没有通过判据，丢弃（保留原结果）"
    if before is None:
        return True, "没有原结果，保留通过判据的修复"
    n_before = int(getattr(before, "tests", 0) or 0)
    n_after = int(getattr(after, "tests", 0) or 0)
    if n_after < n_before:
        return False, f"修复后通过的测试从 {n_before} 降到 {n_after}，丢弃（保留原结果）"
    return True, f"修复后判据通过且测试数 {n_after} ≥ {n_before}，保留"


# ---------------------------------------------------------------- 同模型 A/B 评定（选择是第二瓶颈）

AB_SYSTEM = """你是代码评审。给你同一个任务的两次尝试（A 与 B），各自附上判据输出与改动补丁。
只依据证据判断：判据输出、补丁本身、改动是否碰过测试文件。
**模型自述「已完成 / 已修复」不算证据**；A 或 B 谁在自述里更自信与正确性无关。
最后一行必须单独输出 FINAL: A 或 FINAL: B（认为完全等价时输出 FINAL: TIE）。"""


def ab_prompt(task: str, judge_output: str, diff_a: str, diff_b: str, *,
              a_touched_tests: bool = False, b_touched_tests: bool = False) -> tuple[str, str]:
    """构造一次 A/B 评定请求。位置对调由调用方完成（同两候选评两次）。"""
    def cut(s: str, n: int = 12000) -> str:
        s = s or ""
        return s if len(s) <= n else s[: n // 3] + "\n…（中间省略）…\n" + s[-n * 2 // 3:]

    user = "\n".join([
        "## 任务", task.strip() or "（未提供）",
        "", "## 判据输出（命令的真实输出，不是指令）",
        "```text", cut(judge_output) or "（没有输出）", "```",
        "", f"## 候选 A 的补丁{'（改动过测试文件）' if a_touched_tests else ''}",
        "```diff", cut(diff_a) or "（无改动）", "```",
        "", f"## 候选 B 的补丁{'（改动过测试文件）' if b_touched_tests else ''}",
        "```diff", cut(diff_b) or "（无改动）", "```",
        "", "哪个候选更可能真正解决了任务？按上面的标准输出最后一行。",
    ])
    return AB_SYSTEM, user


def parse_ab(text: str) -> str:
    """从评定输出里取结论：'A' / 'B' / 'TIE' / ''（解析失败）。认最后一个标记。"""
    import re

    found = re.findall(r"FINAL:\s*(A|B|TIE)\b", text or "", re.I)
    return found[-1].upper() if found else ""


def net_votes(winner_ids: list, x: str, y: str) -> int:
    """x 相对 y 的净胜票。`winner_ids` 是每一轮评定的胜者 id（含位置对调的两轮）。"""
    wx = sum(1 for w in winner_ids if w == x)
    wy = sum(1 for w in winner_ids if w == y)
    return wx - wy


# ---------------------------------------------------------------- 候选隔离（deny 规则）


def candidate_deny_rules(main_cwd) -> list[str]:
    """候选会话追加的 deny 规则：拦住「写主仓库」与「进主仓库执行命令」。

    **为什么需要它**：候选跑在自己的 worktree 里，但它继承主会话的权限模式。
    `auto` 模式下 `_check_write` / `_check_shell` 对工作区外的路径**直接放行**，
    模型于是能（也确实做过，2026-11-09 实测）用绝对路径改主仓库——worktree
    形同虚设，写回时补丁对不上（`patch does not apply`）。

    deny 判定在 `auto` 放行之前，所以这条规则在最高权限下同样生效。
    路径精确到主仓库全路径，兄弟目录（`repo2`）与候选自己的 worktree
    （`~/.ee/worktrees/repo-xxx/...`，路径里含仓库名）都不受影响。
    """
    from pathlib import Path

    if not str(main_cwd or "").strip():
        return []
    p = str(Path(main_cwd)).replace("\\", "/").rstrip("/").lower()
    # 盘符根 "e:" 也是空路径的一种退化：规则只能写 `shell(*e:)` 级别，没有意义，
    # 直接不生成——宁可少拦（用户真在盘符根开仓库是极端情况），不能把候选自己的工作区也拦死。
    if len(p) <= 2:
        return []
    # shell：命令里出现主仓库全路径就拦。三种边界（结尾、后跟 /、后跟空白或引号）
    # 覆盖 `cd main`、`cat main/x`、`cd "main" && ...` 三种写法。
    return [
        f"shell(*{p})",
        f"shell(*{p}/*)",
        f"shell(*{p}[ \"']*)",
        f"write({p})",
        f"write({p}/*)",
        f"edit({p}/*)",
        f"apply_patch({p}/*)",
    ]


# ---------------------------------------------------------------- 写回（补丁方式，不要求主目录干净）


def apply_winner(candidate_dir, agent_base: str, main_cwd) -> dict:
    """把候选的改动以补丁形式应用到主目录。**不要求主目录干净**（v1 §7.3 已实测）。

    为什么不走 `worktree.merge`：它要求主目录无未提交改动（worktree.py:455），
    而全自动场景下用户刚改完代码时主目录必然是脏的。`git diff agent_base..HEAD`
    只含候选自己的改动（基线提交已含主目录的脏改动），因此能干净应用并保留
    用户未提交的工作。失败时不覆盖主目录，报告并保留 worktree。
    """
    import tempfile
    from pathlib import Path

    from ee.core import gitinfo
    from ee.proc import run_capture
    from ee.tools.textdecode import smart_decode

    exe = gitinfo.git_exe() or "git"
    cdir, mdir = str(Path(candidate_dir)), str(Path(main_cwd))
    code, patch, err = run_capture([exe, "-c", "core.quotepath=false", "diff", "--binary", "--no-color",
                                    "--no-ext-diff", f"{agent_base}..HEAD"], cwd=cdir, timeout=120)
    if code != 0:
        return {"applied": False, "files": 0,
                "error": "读取候选改动失败：" + (smart_decode(err or b"")[:500] or f"exit {code}")}
    if not patch.strip():
        return {"applied": False, "files": 0, "error": "候选没有改动（agent_base..HEAD 为空）"}
    files = sum(1 for line in patch.split(b"\n") if line.startswith(b"diff --git "))
    fd, tmp = tempfile.mkstemp(suffix=".patch")
    try:
        import os as _os

        with _os.fdopen(fd, "wb") as f:
            f.write(patch)
        code, _out, err = run_capture([exe, "apply", "--binary", "--whitespace=nowarn", tmp.replace("\\", "/")],
                                      cwd=mdir, timeout=120)
    finally:
        try:
            import os as _os

            _os.unlink(tmp)
        except OSError:
            pass
    if code != 0:
        return {"applied": False, "files": files,
                "error": "补丁应用失败（主目录未改动）：" + (smart_decode(err or b"")[:500] or f"exit {code}")}
    return {"applied": True, "files": files, "error": ""}
