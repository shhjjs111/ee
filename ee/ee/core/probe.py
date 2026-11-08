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

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ProbeResult:
    """探测结果。`command` 为 None 表示探测不到（调用方降级）。"""
    command: str | None = None
    source: str = ""                 # 命中的文件名（界面展示「来自 pyproject.toml」）
    confirmed: bool = False          # 探测来的判据需要用户确认一次
    test_files: list[str] = field(default_factory=list)   # 供回归门与「谁动过测试」
    note: str = ""                   # 给用户看的补充说明（如 gradle 需要开 testLogging）

    def as_dict(self) -> dict:
        return {"command": self.command, "source": self.source, "confirmed": self.confirmed,
                "test_files": list(self.test_files), "note": self.note}


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


def _pyproject(root: Path) -> str | None:
    raw = _read(root, "pyproject.toml")
    if raw is None:
        return None
    if "[tool.pytest" in raw or re.search(r"^\[tool\.uv\]", raw, re.M):
        return "python -m pytest -q"
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


def collect_test_files(root: Path, limit: int = 4000) -> list[str]:
    """列出项目里的测试文件（相对路径）。只读目录，不做任何判断。

    限深、限数：大仓库的 `node_modules` 已经在 ignore 里，但这里不依赖
    `.gitignore` 解析，只用 skip 目录 + 上限兜住。拿不全只会让「谁碰过测试」
    的信号变弱，不影响正确性。
    """
    skip = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "target",
            ".ee", ".cursor", ".claude", "vendor"}
    out: list[str] = []
    stack = [root]
    while stack and len(out) < limit:
        d = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for e in entries:
            if len(out) >= limit:
                break
            try:
                if e.is_dir():
                    if e.name not in skip and not e.name.startswith("."):
                        stack.append(e)
                    continue
            except OSError:
                continue
            rel = e.relative_to(root).as_posix()
            if is_test_file(rel):
                out.append(rel)
    return sorted(out)


def probe(root: Path | str) -> ProbeResult:
    """探测判据。只读文件，不执行任何命令。"""
    root = Path(root)
    detected: list[tuple[str, str]] = []
    for label, fn in _PROBES:
        try:
            cmd = fn(root)
        except Exception:  # noqa: BLE001  单个探测器出错不影响其它（坏 JSON 等）
            cmd = None
        if cmd:
            detected.append((label, cmd))
    files = collect_test_files(root)
    if not detected:
        return ProbeResult(note="没找到测试命令（pyproject.toml / package.json / go.mod / pom.xml 等都没有）。"
                                "可以在「目标」里填一条命令启用完整模式。", test_files=files)
    label, cmd = detected[0]
    note = ""
    if len(detected) > 1:
        rest = "、".join(x[0] for x in detected[1:])
        note = f"同时探测到 {rest}；默认用 {label} 的 {cmd}，不对可以在「目标」里改。"
    if cmd.startswith("./gradlew"):
        note = (note + " " if note else "") + \
            "Gradle 默认不打印测试计数：需要在 build.gradle 里加 testLogging 显示 passed/failed，" \
            "否则判据会因读不到测试数而判不通过。"
    return ProbeResult(command=cmd, source=label, test_files=files, note=note)
