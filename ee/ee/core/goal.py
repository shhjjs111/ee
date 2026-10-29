"""Goal 判据判定（T10-b / U1）：回答「这个条件现在成立吗，凭什么」。

**为什么这一层值得单独存在**：让 agent「跑到通过为止」最容易出的事故不是跑不完，
而是**假成功**——命令退出了、看起来没报错，就宣布达标。本项目 CI 上真出过一次
相近的形态（`0 passed` 也能是 exit 0）。

所以判据只有一个方向：**只有证据充分才算成立**。
- exit 必须是 0；
- 必须真的收集到测试，数量 ≥ `min_tests`（默认 1）；
- 超时、被杀、拿不到退出码，一律不成立；
- 读不出测试计数时按 0 处理——宁可多跑几轮，不可假成功。

判据由用户给，模型不能改（见任务书不变量 8）。

**边界**：本模块只判定，不跑 agent 回合。续跑控制器在 U2，`turn_runner` 由调用方注入，
避免出现第二个 agent 循环。
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

# 停止原因的取值（U2 用，这里先定义好避免两边字面量漂移）
MET = "met"
EXHAUSTED = "exhausted"
BUDGET = "budget"
INTERRUPTED = "interrupted"
ERROR = "error"

_TAIL_CHARS = 4000        # 报告里保留的输出尾部长度


@dataclass
class GoalSpec:
    """一条判据。MVP 只有 command 一种——自然语言判据无法可靠判定。"""
    command: str
    cwd: str | None = None
    min_tests: int = 1
    timeout: float = 300.0
    shell: str = "auto"


@dataclass
class Verdict:
    """一次判定的结果。`met` 是唯一结论，其余都是证据。"""
    exit: int | None = None
    tests: int | None = None
    timed_out: bool = False
    output: str = ""
    min_tests: int = 1

    @property
    def met(self) -> bool:
        if self.timed_out:
            return False
        if self.exit is None or self.exit != 0:
            return False
        # tests 为 None（读不出计数）在这里统一按 0 处理：保守方向
        n = self.tests if isinstance(self.tests, int) else 0
        return n >= max(1, int(self.min_tests))

    @property
    def reason(self) -> str:
        """给人和模型看的一句话，说明为什么（不）成立。"""
        if self.timed_out:
            return "timeout：判据命令超时，未跑完不算成功"
        if self.exit is None:
            return "no_exit：没拿到退出码（进程被杀或启动失败）"
        if self.exit != 0:
            return f"exit={self.exit}：命令以非零退出"
        n = self.tests if isinstance(self.tests, int) else 0
        need = max(1, int(self.min_tests))
        if n < need:
            if n == 0:
                return ("zero_tests：命令成功但没收集到任何测试"
                        f"（需要 ≥{need} 个）。退出 0 不等于验证通过")
            return f"only_{n}_tests：只跑了 {n} 个测试，需要 ≥{need} 个"
        return f"ok：{n} 个测试通过"

    def tail(self) -> str:
        out = self.output or ""
        return out[-_TAIL_CHARS:]


@dataclass
class GoalResult:
    """整轮 Goal 执行的结果（U2 填充 rounds）。"""
    stop_reason: str = ERROR
    rounds: list[Verdict] = field(default_factory=list)
    detail: str = ""

    @property
    def met(self) -> bool:
        return self.stop_reason == MET

    def report(self) -> str:
        lines = [f"goal: {self.stop_reason}（{len(self.rounds)} 轮）"]
        if self.detail:
            lines.append(self.detail)
        last = self.rounds[-1] if self.rounds else None
        if last is not None:
            lines.append(f"最后一次判定：{last.reason}")
            t = last.tail().strip()
            if t:
                lines.append("最后输出（尾部）：")
                lines.append(t)
        return "\n".join(lines)


# ---------------------------------------------------------------- 计数解析

# pytest 的几种常见形态。顺序重要：先认「实际跑过」的计数，再退回 collected。
_PASSED = re.compile(r"(\d+)\s+passed", re.I)
_FAILED = re.compile(r"(\d+)\s+failed", re.I)
_COLLECTED = re.compile(r"collected\s+(\d+)\s+items?", re.I)
_RAN = re.compile(r"(\d+)\s+(?:tests?|用例)\s*(?:passed|通过)", re.I)


def parse_tests(output: str) -> int:
    """从判据输出里读出「有多少个测试真正跑过了」。

    规则（按可靠性排序）：
    - 有 `N passed` 与 `M failed` 时**相加**：两个都是"真跑过"的测试，
      `2 failed, 3 passed` 表示跑了 5 个。只取 passed 会把失败那部分漏掉，
      在"修好一半"的场景里低估进度。
    - 只有 `N passed` 就用它。
    - 只有 `N failed` 就用它（跑过但都失败）。
    - 否则 `collected N items`。
    - 都没有 → 0（读不出就当没跑，不假成功）。

    为什么 `collected` 排在后面：collected 包含 skipped，
    10 collected / 8 passed / 2 skipped 的真实通过数不是 10。
    """
    if not output:
        return 0
    p = _PASSED.search(output)
    f = _FAILED.search(output)
    if p and f:
        return int(p.group(1)) + int(f.group(1))
    if p:
        return int(p.group(1))
    if f:
        return int(f.group(1))
    c = _COLLECTED.search(output)
    if c:
        return int(c.group(1))
    r = _RAN.search(output)
    if r:
        return int(r.group(1))
    return 0


# ---------------------------------------------------------------- 执行判定

async def judge(spec: GoalSpec, *, runner=None) -> Verdict:
    """跑一次判据命令并给出 Verdict。

    `runner` 可注入（测试用），签名与 `ee.tools.shell.run_command` 一致：
    `(command, shell, cwd, timeout, emit, sandbox) -> (exit, output, timed_out)`。
    复用 else 分支的那条路径，不新开一套子进程语义。
    """
    from pathlib import Path

    cwd = Path(spec.cwd) if spec.cwd else Path.cwd()

    if runner is None:
        from ee.tools.shell import run_command
        from ee.tools.shellenv import resolve_shell

        shell = resolve_shell(spec.shell)
        run = run_command
    else:
        shell = None
        run = runner

    try:
        code, output, timed_out = await run(
            spec.command, shell, cwd, float(spec.timeout), lambda _s: None, None)
    except Exception as e:  # noqa: BLE001  启动失败也是「没拿到退出码」，不是崩溃
        return Verdict(exit=None, tests=0, timed_out=False,
                       output=f"启动判据命令失败：{type(e).__name__}: {e}",
                       min_tests=spec.min_tests)

    return Verdict(exit=code, tests=parse_tests(output or ""),
                   timed_out=bool(timed_out), output=output or "",
                   min_tests=spec.min_tests)


# ---------------------------------------------------------------- 续跑控制器（U2）

DEFAULT_MAX_ROUNDS = 3


async def run_goal(spec: GoalSpec, *, turn_runner, judge_fn=None, max_rounds: int = DEFAULT_MAX_ROUNDS,
                   budget_exhausted=None, emit=None) -> GoalResult:
    """跑到判据成立为止，或到上限为止。

    参数都是注入的，**这里不自己造 agent 回合**：
    - `turn_runner`：跑一个 agent 回合的协程函数，由调用方给（headless 传它自己的）。
    - `judge_fn`：判定函数，默认 `judge`（测试可替换，避免真跑子进程）。
    - `budget_exhausted`：可选的无参函数，返回 True 表示预算用完。

    顺序是「先判后跑」：条件本来就成立时一个回合都不跑，避免为了"确认一下"
    去改本来就对的代码。
    """
    jf = judge_fn or judge
    result = GoalResult(stop_reason=ERROR)
    rounds = max(1, int(max_rounds))

    for i in range(rounds):
        v = await jf(spec)
        result.rounds.append(v)
        if emit is not None:
            try:
                r = emit(v)
                # emit 允许是协程：忘了 await 会让事件静默丢失（踩过）
                if asyncio.iscoroutine(r):
                    await r
            except Exception:  # noqa: BLE001  观感回调出错不影响判定
                pass
        if v.met:
            result.stop_reason = MET
            result.detail = f"第 {i + 1} 次判定即成立"
            return result

        # 还没达标：这一轮要不要再跑回合？
        if i == rounds - 1:
            break                       # 最后一轮判完就停，不再跑无用的回合
        if budget_exhausted is not None and budget_exhausted():
            result.stop_reason = BUDGET
            result.detail = "花费上限已到，停止续跑"
            return result

        try:
            await turn_runner()
        except KeyboardInterrupt:
            result.stop_reason = INTERRUPTED
            result.detail = "用户中断"
            return result
        except asyncio.CancelledError:
            result.stop_reason = INTERRUPTED
            result.detail = "任务被取消"
            return result
        except Exception as e:  # noqa: BLE001  回合失败要报出来，不抛给调用方
            result.stop_reason = ERROR
            result.detail = f"回合执行失败：{type(e).__name__}: {e}"
            return result

    result.stop_reason = EXHAUSTED
    result.detail = f"跑满 {rounds} 轮仍未成立"
    return result

