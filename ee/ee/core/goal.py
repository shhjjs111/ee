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
import inspect
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
_SKIPPED = re.compile(r"(\d+)\s+skipped", re.I)
_COLLECTED = re.compile(r"collected\s+(\d+)\s+items?", re.I)
_RAN = re.compile(r"(\d+)\s+(?:tests?|用例)\s*(?:passed|通过)", re.I)

# 其它 runner 的汇总行（allout 阶段 0）。每一条都用行首锚定：
# vitest 的 `Test Files 3 passed` 会命中 `(\d+) passed`，不锚定就会把文件数当测试数。
#
# 返回值的口径统一为「真跑过的测试数」= 通过 + 失败（跳过不算）：
# 只取通过数会让「修好一半」的进度被低估，也偏离 pytest 分支的既有口径。
_GO_OK = re.compile(r"^ok\s+\S+", re.M)                       # 成功行：ok  pkg  0.12s
_GO_PASS = re.compile(r"^\s*--- PASS:\s+\S+", re.M)           # -v 形态
_GO_FAIL = re.compile(r"^\s*--- FAIL:\s+\S+", re.M)           # -v 形态（未通过也算跑过）
_GO_FAIL_SUMMARY = re.compile(r"^(?:FAIL|\s*FAIL)\s+\S+", re.M)  # 非 -v 的失败汇总行
_VITEST_TESTS = re.compile(r"^\s*Tests\s+(.+)$", re.M)        # Tests  1 failed | 11 passed (12)
_JEST_TESTS = re.compile(r"^\s*Tests:\s+(.+)$", re.M)         # Tests:  1 failed, 11 passed, 12 total
_DOTNET = re.compile(r"Failed:\s*(\d+),\s*Passed:\s*(\d+)", re.I)
_MOCHA_PASS = re.compile(r"(\d+)\s+passing", re.I)
_RSPEC = re.compile(r"(\d+)\s+examples?,\s*(\d+)\s+failures?", re.I)
_PHPUNIT_OK = re.compile(r"OK\s*\((\d+)\s+tests?", re.I)
_PHPUNIT_SUM = re.compile(r"Tests:\s*(\d+),\s*Assertions:", re.I)
_BUN_PASS = re.compile(r"^\s*(\d+)\s+pass\s*$", re.M)
_BUN_FAIL = re.compile(r"^\s*(\d+)\s+fail\s*$", re.M)
_MAVEN = re.compile(r"Tests run:\s*(\d+)", re.I)
_GRADLE_SUMMARY = re.compile(r"(\d+)\s+tests?\s+completed", re.I)   # 失败汇总：3 tests completed, 1 failed
_GRADLE_LINE = re.compile(r"^.+?\s>\s.+?\s(?:PASSED|FAILED)\s*$", re.M)  # testLogging 逐条 PASSED/FAILED


# 「Tests  1 failed | 11 passed (12)」/「Tests: 1 failed, 11 passed, 12 total」里的数字提取
_NUM_PASSED = re.compile(r"(\d+)\s+passed", re.I)
_NUM_FAILED = re.compile(r"(\d+)\s+failed", re.I)
_NUM_TOTAL = re.compile(r"(\d+)\s+total", re.I)


def _from_vitest_jest(line: str) -> int:
    """vitest / jest 的 Tests 行：优先 通过+失败，再退回 total。"""
    p = _NUM_PASSED.search(line)
    f = _NUM_FAILED.search(line)
    if p and f:
        return int(p.group(1)) + int(f.group(1))
    if p:
        return int(p.group(1))
    if f:
        return int(f.group(1))
    t = _NUM_TOTAL.search(line)
    return int(t.group(1)) if t else 0


def parse_tests(output: str) -> int:
    """从判据输出里读出「有多少个测试真正跑过了」。

    规则（按可靠性排序）：
    - 先认 pytest 的 `N passed` / `M failed`：两者**相加**，都是"真跑过"的测试，
      `2 failed, 3 passed` 表示跑了 5 个。只取 passed 会把失败那部分漏掉，
      在"修好一半"的场景里低估进度。
    - 其它 runner（go / dotnet / vitest / jest / mocha / rspec / phpunit / bun /
      maven / gradle）按各自汇总行解析，口径同样是「通过 + 失败」。
    - 否则 `collected N items`。
    - 都没有 → 0（读不出就当没跑，不假成功）。

    为什么 `collected` 排在后面：collected 包含 skipped，
    10 collected / 8 passed / 2 skipped 的真实通过数不是 10。
    """
    if not output:
        return 0
    # vitest / jest 的 `Tests` 行必须先判：vitest 会把 `Test Files  3 passed`
    # 放在前面，pytest 的通用 `N passed` 会先把文件数当成测试数。
    m = _VITEST_TESTS.search(output) or _JEST_TESTS.search(output)
    if m:
        n = _from_vitest_jest(m.group(1))
        if n:
            return n
    # Gradle 的失败汇总行 `3 tests completed, 1 failed`：`1 failed` 会被通用规则
    # 读成只跑了 1 个，必须先认整行。成功且没开 testLogging 时 Gradle 什么都不打，
    # 读 0 是保守方向（探测提示会要求用户开 testLogging）。
    m = _GRADLE_SUMMARY.search(output)
    if m:
        return int(m.group(1))
    # 没有 Tests 行时，`Test Files  3 passed` 同样不能被通用规则当测试数。
    generic = re.sub(r"^.*Test Files.*$", "", output, flags=re.M)
    p = _PASSED.search(generic)
    f = _FAILED.search(generic)
    if p and f:
        return int(p.group(1)) + int(f.group(1))
    if p:
        return int(p.group(1))
    if f:
        return int(f.group(1))
    c = _COLLECTED.search(output)
    if c:
        # collected 含 skipped。全 skipped 时「收集到 3 个」不等于「跑了 3 个」：
        # 扣掉 skip 数，一个都没真跑就返回 0——宁可多跑几轮，不可假成功。
        total = int(c.group(1))
        s = _SKIPPED.search(output)
        skipped = int(s.group(1)) if s else 0
        return max(0, total - skipped)
    r = _RAN.search(output)
    if r:
        return int(r.group(1))

    # ---- 其它 runner（顺序无关，形态互不重叠；但 vitest/jest 必须在 go 之前判，
    # 否则 `Tests  ... passed` 不会被命中，也不冲突。放在一起按最具体的先查。）
    m = _VITEST_TESTS.search(output) or _JEST_TESTS.search(output)
    if m:
        n = _from_vitest_jest(m.group(1))
        if n:
            return n
    m = _MAVEN.search(output)
    if m:
        return int(m.group(1))          # Tests run 已含 failures/errors，不能相加
    gradle_lines = _GRADLE_LINE.findall(output)
    if gradle_lines:
        return len(gradle_lines)        # testLogging 的逐条 PASSED/FAILED
    m = _DOTNET.search(output)
    if m:
        return int(m.group(1)) + int(m.group(2))
    m = _RSPEC.search(output)
    if m:
        return int(m.group(1))          # examples 已含 failures
    m = _PHPUNIT_OK.search(output)
    if m:
        return int(m.group(1))
    m = _PHPUNIT_SUM.search(output)
    if m:
        return int(m.group(1))
    m = _MOCHA_PASS.search(output)
    if m:
        passes = int(m.group(1))
        fail = re.search(r"(\d+)\s+failing", output, re.I)
        return passes + (int(fail.group(1)) if fail else 0)
    bp, bf = _BUN_PASS.search(output), _BUN_FAIL.search(output)
    if bp:
        return int(bp.group(1)) + (int(bf.group(1)) if bf else 0)
    go_fail = bool(_GO_FAIL_SUMMARY.search(output)) or bool(_GO_FAIL.search(output))
    passes = len(_GO_OK.findall(output)) + len(_GO_PASS.findall(output))
    if passes:
        return passes + len(_GO_FAIL.findall(output))
    if go_fail:
        # go 全失败且没带 -v：没有可数的通过行。0 是保守方向（退出码非零本来就不通过）
        return 0
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


async def _call_runner(runner, verdict):
    """调用注入的 turn_runner，兼容接受 0 个或 1 个参数的签名。

    Goal 的 runner 由调用方提供（headless、server/threads、以及测试替身）。
    新契约会传上一个 Verdict，让调用方够得着判据与失败输出；旧的无参 runner
    也必须继续能跑，否则任何一处漏改就会把整个循环打挂。
    """
    try:
        sig = inspect.signature(runner)
    except (TypeError, ValueError):        # 内置/C 实现拿不到签名：当作无参调用
        return await runner()
    params = [p for p in sig.parameters.values()
              if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
    if params or has_varargs:
        return await runner(verdict)
    return await runner()


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
            # 把上一轮的判定交给 runner：调用方据此把判据与失败输出送进模型输入。
            #
            # 用签名探测而不是 TypeError 回退：旧式 `async def turn()` 的注入方仍然可用，
            # 但**不能靠捕获 TypeError 再重试**——那样 KeyboardInterrupt/CancelledError
            # 在重试时抛出会绕过外层 except（同一 try 的 except 块内异常不回流），
            # 中断信号会直接穿透整个循环。
            await _call_runner(turn_runner, v)
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

