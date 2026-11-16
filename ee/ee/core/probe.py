"""判据探测（allout 阶段 1）：只读项目文件，回答「这个项目怎么验证」。

**为什么只读、不跑命令**：打开「全力以赴」这个动作本身不能产生副作用。
探测只在用户点开开关、或首次发消息时读几个文件；真正执行判据永远是
`goal.judge`，而它只认用户在「目标」里填的命令或这里探测出、并经用户确认过的命令。

**为什么判据可以来自探测**：`pyproject.toml` / `package.json` 是**项目作者**
写下的事实，不是模型写的（不变量 8 只禁止模型定判据）。但陌生仓库不能让 ee
自动执行它的脚本，所以探测结果带 `confirmed=False`，界面确认一次后才生效。

优先级：更外层的构建系统优先；同一层同时命中多种时取唯一最外层，并列则返回
`None` 并说明原因，由用户手填，不猜。
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ProbeResult:
    """探测结果。`command` 为 None 表示探测不到（调用方降级）。"""
    command: str | None = None
    source: str = ""                 # 命中的文件名（界面展示「来自 pyproject.toml」）
    confirmed: bool = False          # 探测来的判据需要用户确认一次
    test_files: list[str] = field(default_factory=list)   # 供回归门与「谁动过测试」
    test_files_complete: bool = True  # False = 扫描截断/目录不可读，不能宣称保护完整
    note: str = ""                   # 给用户看的补充说明（如 gradle 需要开 testLogging）

    def as_dict(self) -> dict:
        return {"command": self.command, "source": self.source, "confirmed": self.confirmed,
                "test_files": list(self.test_files), "test_files_complete": self.test_files_complete,
                "note": self.note}


# 测试文件的常见命名（相对路径，正斜杠）。只用于「谁碰过测试」与提示，不执行。
_TEST_PATTERNS = [
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"(^|/)[^/]+_test\.(py|go|rb|php|exs?)$"),
    re.compile(r"(^|/)[^/]*Test\.php$"),          # PHPUnit 惯例：FooTest.php
    re.compile(r"\.(test|spec)\.(js|jsx|ts|tsx|mjs|cjs)$"),
    re.compile(r"(^|/)__tests__/"),
    re.compile(r"(^|/)spec/"),
]


def is_test_file(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    return any(p.search(rel) for p in _TEST_PATTERNS)


def _read(root: Path, name: str) -> str | None:
    p = root / name
    try:
        return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None
    except OSError:
        return None


def _pkg_test(root: Path) -> str | None:
    raw = _read(root, "package.json")
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    scripts = data.get("scripts") or {}
    t = scripts.get("test") if isinstance(scripts, dict) else None
    if not isinstance(t, str) or not t.strip():
        return None
    if "no test specified" in t.lower():      # npm init 的默认占位脚本
        return None
    # 包管理器：有 lockfile 就用对应的
    if (root / "pnpm-lock.yaml").is_file():
        return "pnpm test"
    if (root / "yarn.lock").is_file():
        return "yarn test"
    if (root / "bun.lockb").is_file() or (root / "bun.lock").is_file():
        return "bun test"
    return "npm test"


def _python_for(root: Path) -> str:
    """优先项目自己的虚拟环境解释器，避免 PATH 上的另一个 Python 跑错依赖。"""
    for base in (root, *list(root.parents)[:2]):
        for rel in (".venv/Scripts/python.exe", ".venv/bin/python",
                    "venv/Scripts/python.exe", "venv/bin/python"):
            p = base / rel
            if p.is_file():
                text = str(p)
                return f'"{text}"' if any(ch.isspace() for ch in text) else text
    return "python"


def _pyproject(root: Path) -> str | None:
    raw = _read(root, "pyproject.toml")
    if raw is None:
        return None
    if "[tool.pytest" in raw or re.search(r"^\[tool\.uv\]", raw, re.M):
        return f"{_python_for(root)} -m pytest -q"
    return None


def _cargo(root: Path) -> str | None:
    return "cargo test" if (root / "Cargo.toml").is_file() else None


def _go(root: Path) -> str | None:
    return "go test ./..." if (root / "go.mod").is_file() else None


def _dotnet(root: Path) -> str | None:
    if any(root.glob("*.sln")) or any(root.glob("*.csproj")):
        return "dotnet test"
    return None


def _gradle(root: Path) -> str | None:
    if (root / "build.gradle").is_file() or (root / "build.gradle.kts").is_file():
        return "./gradlew test"
    return None


def _maven(root: Path) -> str | None:
    return "mvn -q test" if (root / "pom.xml").is_file() else None


def _ruby(root: Path) -> str | None:
    if (root / "Gemfile").is_file() and ((root / "spec").is_dir() or (root / "test").is_dir()):
        return "bundle exec rspec" if (root / "spec").is_dir() else "bundle exec rake test"
    return None


def _php(root: Path) -> str | None:
    raw = _read(root, "composer.json")
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    dev = data.get("require-dev") or {}
    if isinstance(dev, dict) and any("phpunit" in k.lower() for k in dev):
        return "vendor/bin/phpunit"
    return None


# 顺序即优先级：越靠前的构建系统越外层。同一层命中多个时按顺序取第一个——
# 这里不做「并列就放弃」：同一目录同时有 pyproject.toml 和 package.json 的常见
# 场景（Python 后端 + 前端）里，先让用户确认一次比直接放弃更有用；用户不认可
# 就直接改填。
_PROBES = [
    ("pyproject.toml", _pyproject),
    ("package.json", _pkg_test),
    ("Cargo.toml", _cargo),
    ("go.mod", _go),
    ("pom.xml", _maven),
    ("build.gradle", _gradle),
    ("*.sln / *.csproj", _dotnet),
    ("Gemfile", _ruby),
    ("composer.json", _php),
]


def collect_test_files_ex(root: Path, limit: int = 4000, *,
                          time_budget_s: float = 2.0,
                          max_entries: int = 200_000) -> tuple[list[str], bool]:
    """列出项目里的测试文件，并报告扫描是否完整。

    安全边界：不 follow 到 root 之外、按真实路径去重（junction/symlink 循环只访问一次）、
    超过时间/条目上限或目录不可读都返回 `complete=False`。调用方不能把不完整清单
    当成完整保护。
    """
    skip = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "target",
            ".ee", ".cursor", ".claude", "vendor", "release", "test-results"}
    root = Path(root)
    try:
        root_real = Path(os.path.realpath(root))
        root_resolved = root.resolve()
    except OSError:
        return [], False
    out: list[str] = []
    stack = [root]
    seen_dirs: set[str] = set()
    seen_entries = 0
    complete = True
    deadline = time.monotonic() + max(0.05, float(time_budget_s))
    while stack and len(out) < limit:
        if time.monotonic() > deadline:
            complete = False
            break
        d = stack.pop()
        try:
            real = Path(os.path.realpath(d))
            key = str(real).lower()
            if key in seen_dirs:
                continue
            if not real.is_relative_to(root_real):
                continue
            seen_dirs.add(key)
        except (OSError, ValueError):
            complete = False
            continue
        try:
            entries = list(d.iterdir())
        except OSError:
            complete = False
            continue
        for e in entries:
            seen_entries += 1
            if seen_entries > max_entries or len(out) >= limit:
                complete = False
                break
            try:
                if e.is_dir():
                    if e.name in skip or e.name.startswith("."):
                        continue
                    # 目录联接/符号链接：真实路径必须仍在 root 内；真实路径去重防循环。
                    real_child = Path(os.path.realpath(e))
                    if not real_child.is_relative_to(root_real):
                        continue
                    stack.append(e)
                    continue
                if not e.is_file():
                    continue
                real_file = Path(os.path.realpath(e))
                if not real_file.is_relative_to(root_real):
                    continue
            except (OSError, ValueError):
                complete = False
                continue
            try:
                rel = e.relative_to(root_resolved).as_posix()
            except ValueError:
                rel = e.relative_to(root).as_posix()
            if is_test_file(rel):
                out.append(rel)
    if stack:
        complete = False
    return sorted(out), complete


def collect_test_files(root: Path, limit: int = 4000) -> list[str]:
    """兼容入口：只要文件清单；需要完整性标记时用 `collect_test_files_ex`。"""
    return collect_test_files_ex(root, limit)[0]


def _detect(root: Path) -> list[tuple[str, str]]:
    detected: list[tuple[str, str]] = []
    for label, fn in _PROBES:
        try:
            cmd = fn(root)
        except Exception:  # noqa: BLE001  单个探测器出错不影响其它（坏 JSON 等）
            cmd = None
        if cmd:
            detected.append((label, cmd))
    return detected


# 根目录没有判据时，最多扫一层常见子项目；这些目录不可能放项目构建文件。
_NESTED_SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
                "target", ".ee", ".cursor", ".claude", "vendor", "release", "test-results"}


def _command_in_dir(root: Path, cwd: Path, cmd: str) -> str:
    """把命令包成在指定目录执行、且 cmd/PowerShell 都能跑的一行。

    目标用**相对 root 的路径**，这样候选在各自 worktree 的同一相对位置执行时
    也落在自己的子目录，而不是主工作区。脚本经 UTF-16LE/base64 传递，绕开
    cmd 与 PowerShell 对嵌套双引号的不同解析；调用形式在两种 shell 里都是普通参数。
    命令里的 Python 可执行路径若带双引号，改写为 PowerShell 的 `& 'path'`。
    """
    try:
        target = Path(cwd).resolve().relative_to(Path(root).resolve()).as_posix() or "."
    except (OSError, ValueError):
        target = str(Path(cwd).resolve())
    target = target.replace("'", "''")
    body = cmd.strip()
    if body.startswith('"'):
        end = body.find('"', 1)
        if end > 0:
            exe = body[1:end].replace("'", "''")
            rest = body[end + 1:].strip()
            body = f"& '{exe}'" + (f" {rest}" if rest else "")
    script = f"Set-Location -LiteralPath '{target}'; {body}"
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return f"powershell -NoProfile -EncodedCommand {encoded}"


def _nested_detect(root: Path) -> list[tuple[str, str, Path]]:
    """根目录无判据时找一层子项目（ee/、desktop/ 这类 monorepo 常见布局）。

    返回 `(界面来源名, 已在子目录执行的命令, 子目录)`；命令必须带执行目录，
    否则 `npm test`/`gradlew` 会在根目录跑（2026-11 复核实测）。
    """
    found: list[tuple[str, str, Path]] = []
    try:
        children = sorted(p for p in root.iterdir() if p.is_dir() and p.name not in _NESTED_SKIP)
    except OSError:
        return []
    for child in children:
        for label, cmd in _detect(child):
            found.append((f"{child.name}/{label}", _command_in_dir(root, child, cmd), child))
        if len(found) >= 20:
            break
    return found


def probe(root: Path | str) -> ProbeResult:
    """探测判据。只读文件，不执行任何命令。"""
    root = Path(root)
    detected = _detect(root)
    files, files_complete = collect_test_files_ex(root)
    if not files_complete:
        # 不把截断清单当完整保护；交付闸门会据 test_files_complete=False 标 needs_review。
        scan_note = "测试文件扫描未完整（超过 4000 个或目录不可读），不能宣称既有测试全部受保护。"
    else:
        scan_note = ""
    nested_note = scan_note
    if not detected:
        nested = _nested_detect(root)
        if len(nested) == 1:
            detected = [(nested[0][0], nested[0][1])]
            nested_note = (nested_note + " " if nested_note else "") + \
                f"根目录没有判据，使用子项目 {nested[0][0]} 的 {nested[0][1]}。"
        elif nested:
            choices = "、".join(f"{name}（{cmd}）" for name, cmd, _child in nested[:8])
            note = "根目录没有判据；发现多个子项目：" + choices + \
                   "。请在「目标」里选择其中一条命令启用完整模式。"
            if scan_note:
                note += " " + scan_note
            return ProbeResult(note=note, test_files=files,
                               test_files_complete=files_complete)
    if not detected:
        note = "没找到测试命令（pyproject.toml / package.json / go.mod / pom.xml 等都没有）。" \
               "可以在「目标」里填一条命令启用完整模式。"
        if scan_note:
            note += " " + scan_note
        return ProbeResult(note=note, test_files=files, test_files_complete=files_complete)
    label, cmd = detected[0]
    note = nested_note
    if len(detected) > 1 and not nested_note:
        rest = "、".join(x[0] for x in detected[1:])
        note = f"同时探测到 {rest}；默认用 {label} 的 {cmd}，不对可以在「目标」里改。"
    if cmd.startswith("./gradlew"):
        note = (note + " " if note else "") + \
            "Gradle 成功时默认不打印测试计数（失败时只有 `N tests completed, M failed` 汇总）：在 build.gradle 的 test 配置里加 testLogging { events 'passed', 'failed' }（build.gradle.kts 用双引号），ee 才能数出跑了多少个测试；否则判据会因读不到测试数而判不通过。"
    return ProbeResult(command=cmd, source=label, test_files=files,
                       test_files_complete=files_complete, note=note)
