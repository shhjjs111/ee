"""CI 的离线门槛与付费评测隔离（ACC-32 / ACC-33）。

为什么需要这层守卫：

- **ACC-32**：CI 每次 push main 都真跑全部测试，而 GitHub Actions 上没有模型 API key。
  这本身已经证明「不退化判定不需要 key」——但它只是**当下的事实**，不是**约束**。
  有人哪天为了让某个测试通过而在 workflow 里注入真实 key（或改成只跑 PR），
  这一层就悄悄没了，而且不会有人发现：CI 依旧全绿，只是开始花钱/不再守主干。
- **ACC-33**：`ee/scripts/eval_profiles.py` 会真实调用付费模型
  （默认 `--key-env DEEPSEEK_API_KEY`，见该文件 `main()` 的检查）。它只能手动跑。
  一旦接进 CI，就是每次 push 烧钱，且失败原因是「没 key」而非「代码坏了」。

所以本文件把两件事变成**可执行断言**：
- CI 跑到真实测试，且不依赖任何模型凭据；
- CI 不调用付费评测脚本。

真实运行记录（2026-10-30）：#124 / #125 / #126 三轮 verify 共 12 步全绿，全程未注入 key。
那些记录是证据，本文件是保证——证据会过期，断言不会。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

# 模型凭据：出现即说明 CI 依赖真实付费调用，离线门槛就名存实亡
MODEL_KEY_ENVS = (
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "MOONSHOT_API_KEY",
    "DASHSCOPE_API_KEY",
    "GEMINI_API_KEY",
)

# secrets 里允许引用的：CI 自身凭据，不产生任何模型调用
# 确有新需求时加进这里，并在提交信息里说明「为什么不会产生付费调用」
ALLOWED_SECRETS = {"GITHUB_TOKEN"}

# 会真实调用付费模型的脚本（按名字前缀匹配，避免漏掉同名变体）
PAID_EVAL_SCRIPTS = ("eval_profiles", "eval_gate")


def _workflows() -> list[Path]:
    files = sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))
    assert files, f"没找到任何 CI 配置：{WORKFLOW_DIR}（门槛无从谈起）"
    return files


def _strip_comment(line: str) -> str:
    """去掉 YAML 注释，避免「注释里提了一句 key 名」被判成违规。

    只认 ` #`（空格 + #）：shell 里的 `${VAR#prefix}` 没有空格前缀，不会被误切。
    """
    head, sep, _ = line.partition(" #")
    return head if sep else line


def _code_lines(path: Path):
    """产出 (行号, 去注释后的代码行)，跳过空行与整行注释。"""
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        yield n, _strip_comment(raw)


def test_ci_does_not_require_model_api_key():
    """ACC-32：CI 必须在没有任何模型凭据的前提下完成不退化判定。

    这是「离线门槛可跑」的**否定式**表述，比「跑起来了」更耐时间：
    今天绿可能是运气，不注入 key 是特性。
    """
    offenders: list[str] = []
    for f in _workflows():
        for n, code in _code_lines(f):
            for env in MODEL_KEY_ENVS:
                if env in code:
                    offenders.append(f"{f.name}:{n} 出现模型凭据环境变量 {env}")
            for m in re.finditer(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)", code):
                name = m.group(1)
                if name not in ALLOWED_SECRETS:
                    offenders.append(f"{f.name}:{n} 引用 secrets.{name}")

    assert not offenders, (
        "CI 不该依赖模型凭据：ACC-32 要求「无需 key 即可判定不退化」。\n"
        "如确需注入（例如上传产物的 NPM_TOKEN），把它加进本文件的 ALLOWED_SECRETS，\n"
        "并在提交信息里说明为什么不会产生付费模型调用。\n命中：\n  "
        + "\n  ".join(offenders)
    )


def test_ci_does_not_run_paid_eval_scripts():
    """ACC-33：付费评测（真实模型）只能手动跑，不能挂在 CI 上。"""
    offenders: list[str] = []
    for f in _workflows():
        for n, code in _code_lines(f):
            for script in PAID_EVAL_SCRIPTS:
                if script in code:
                    offenders.append(f"{f.name}:{n} 调用了付费评测 {script}")

    assert not offenders, (
        "付费评测不得进 CI：它会真实调用模型（eval_profiles 默认检查 DEEPSEEK_API_KEY），\n"
        "挂在 push 上等于每次提交都烧钱，且失败原因是「没 key」而不是「代码坏了」。\n"
        "手动入口：./.venv/Scripts/python.exe ee/scripts/eval_profiles.py --model <名>\n命中：\n  "
        + "\n  ".join(offenders)
    )


def test_ci_verify_job_runs_real_suites():
    """ACC-32 的另一半：「不花钱」之外还必须「真判定」。

    一条只装依赖、不跑测试的 CI 同样满足上一条断言，却毫无门槛作用。
    """
    text = (WORKFLOW_DIR / "desktop-windows.yml").read_text(encoding="utf-8")
    required = (
        ("-m pytest", "后端测试"),
        ("npm test", "前端单测"),
        ("npm run typecheck", "类型检查"),
    )
    missing = [f"{why}（{needle}）" for needle, why in required if needle not in text]
    assert not missing, (
        "CI 缺少判定步骤：" + "、".join(missing)
        + "。缺了它们，「离线门槛」就没有可判定的对象。"
    )


def _indented_block(lines: list[str], i: int) -> list[str]:
    """返回 lines[i] 连同其后所有缩进更深的行（YAML 的块就是缩进）。

    只按缩进深度切块，不看键名——否则 `push:` 的块会一路吃掉后面的
    `pull_request:`（它同样缩进），从而把别的触发器的 paths 当成 push 的。
    这条曾让本文件的 paths 断言假绿：删掉 push 下的 `ee/**` 后，
    被误吞的 `pull_request` 里的 `ee/**` 顶了上去，测试照样通过。
    """
    base = len(lines[i]) - len(lines[i].lstrip())
    out = [lines[i]]
    for line in lines[i + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= base:
            break
        out.append(line)
    return out


def _trigger(text: str, name: str) -> list[str] | None:
    """取出顶层 `on:` 下某个触发器的完整块（含自身那一行）；没有则 None。"""
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if re.match(r"^on:\s*$", l)), None)
    assert start is not None, (
        "workflow 缺少块形式的顶层 `on:`（本守卫按缩进切块解析）。"
        "若改成行内写法 `on: [push, pull_request]`，需同步更新本测试。"
    )
    body = _indented_block(lines, start)[1:]
    for i, l in enumerate(body):
        if re.match(rf"^\s*{name}:\s*$", l):
            return _indented_block(body, i)
    return None


def test_ci_gate_covers_main_and_ee_sources():
    """门槛必须挂在 main 的 push 上，且覆盖 ee/ 与 desktop/ 的改动。

    两个真实的失效方式：
    - 只配 `pull_request`：本仓库是直推 main，主干就彻底没有门槛；
    - paths 收窄到不含 `ee/**`：改了后端却不触发测试，门槛对最贵的部分失明。
    """
    text = (WORKFLOW_DIR / "desktop-windows.yml").read_text(encoding="utf-8")

    push = _trigger(text, "push")
    assert push is not None, "CI 没有配置 push 触发：直推 main 时没有任何门槛"
    push_text = "\n".join(push)
    assert "main" in push_text, f"push 触发未包含 main：\n{push_text}"
    for path in ("ee/**", "desktop/**"):
        assert path in push_text, (
            f"push 的 paths 未包含 {path}：改这部分代码不会触发 CI，门槛对最贵的改动失明。\n"
            f"当前：\n{push_text}"
        )

    # PR 触发是可选的，但如果配了就同样不能对 ee/desktop 失明
    pr = _trigger(text, "pull_request")
    if pr is not None and any(p.strip().startswith("paths:") for p in pr):
        pr_text = "\n".join(pr)
        for path in ("ee/**", "desktop/**"):
            assert path in pr_text, (
                f"pull_request 的 paths 未包含 {path}：改这部分代码的 PR 不会触发 CI。\n"
                f"当前：\n{pr_text}"
            )
