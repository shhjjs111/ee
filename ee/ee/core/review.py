"""代码审查（/review）：选审查对象 → 收集 diff → 拼成审查提示，交给主会话跑一个只读回合。

审查对象（kind）：
  uncommitted  工作区里尚未提交的改动（含暂存和未跟踪文件）
  branch       相对某个基准分支：merge-base 到当前工作区的全部改动（含未提交）
  commit       某一个提交
  custom       不给 diff，只按用户写的要求审查（模型自己用 git 查）

审查放在主会话里跑（而不是独立子会话），这样审完可以直接说“修掉 P1”。
回合强制只读；结果末尾带一个 ```ee-review 的 JSON 块，界面把它画成问题卡片。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ee.core import gitinfo
from ee.core.context import load_prompt

KINDS = ("uncommitted", "branch", "commit", "custom")
MAX_DIFF_CHARS = 80_000
REF_RE = re.compile(r"^[A-Za-z0-9._/@^~+-]{1,200}$")


class ReviewError(Exception):
    pass


@dataclass
class Target:
    kind: str
    ref: str = ""
    label: str = ""     # 给人看的：未提交的改动 / 相对 main / 提交 abc1234 …
    diff: str = ""


def _check_ref(ref: str) -> str:
    ref = (ref or "").strip()
    if not ref or ref.startswith("-") or not REF_RE.match(ref):
        raise ReviewError(f"不合法的分支或提交：{ref or '（空）'}")
    return ref


def targets(cwd: Path) -> dict:
    """界面选择器用：当前分支、本地分支、推荐基准、最近提交、是否有未提交改动。"""
    st = gitinfo.status(cwd)
    if not st.is_repo:
        return {"is_repo": False, "git_available": bool(gitinfo.git_exe())}
    _, out = gitinfo._run(["for-each-ref", "--format=%(refname:short)", "--sort=-committerdate", "refs/heads"], cwd)
    branches = [b for b in out.splitlines() if b.strip()]
    base = ""
    c, up = gitinfo._run(["rev-parse", "--abbrev-ref", "origin/HEAD"], cwd)
    cand = [up.strip().split("/", 1)[-1]] if c == 0 and up.strip() else []
    for b in cand + ["main", "master", "develop", "trunk"]:
        if b in branches and b != st.branch:
            base = b
            break
    _, log = gitinfo._run(["log", "-20", "--format=%h%x09%s%x09%cr"], cwd)
    commits = []
    for line in log.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            commits.append({"sha": parts[0], "subject": parts[1], "when": parts[2]})
    return {"is_repo": True, "branch": st.branch, "dirty": st.dirty, "branches": branches, "base": base,
            "commits": commits}


def collect(cwd: Path, kind: str, ref: str = "") -> Target:
    if kind not in KINDS:
        raise ReviewError(f"未知的审查对象：{kind}")
    if kind == "custom":
        return Target(kind, label="按你的要求")
    if not gitinfo.status(cwd).is_repo:
        raise ReviewError("这个文件夹不是 git 仓库，只能用“按要求审查”")
    if kind == "uncommitted":
        d = gitinfo.diff(cwd)
        if not d.strip():
            raise ReviewError("没有未提交的改动")
        return Target(kind, label="未提交的改动", diff=d)
    ref = _check_ref(ref)
    if kind == "branch":
        c, mb = gitinfo._run(["merge-base", "HEAD", ref], cwd)
        if c != 0 or not mb.strip():
            raise ReviewError(f"找不到当前分支和 {ref} 的共同祖先")
        mb = mb.strip()
        _, d = gitinfo._run(["diff", "--no-color", "--no-ext-diff", mb], cwd, timeout=30)
        if not d.strip():
            raise ReviewError(f"相对 {ref} 没有改动")
        return Target(kind, ref, label=f"相对 {ref} 的改动（共同祖先 {mb[:8]}）", diff=d)
    c, d = gitinfo._run(["show", "--no-color", "--no-ext-diff", "--format=commit %H%nAuthor: %an%nDate: %ad%n%n%B",
                         ref, "--"], cwd, timeout=30)
    if c != 0 or not d.strip():
        raise ReviewError(f"找不到提交：{ref}")
    sha = d.split("\n", 1)[0].removeprefix("commit ").strip()[:8]
    return Target(kind, ref, label=f"提交 {sha}", diff=d)


def build_prompt(t: Target, instructions: str = "") -> str:
    parts = [load_prompt("review.md"), f"\n## 审查对象：{t.label}"]
    if t.kind == "uncommitted":
        parts.append("（相当于 git diff HEAD 加上未跟踪的新文件）")
    elif t.kind == "branch":
        parts.append(f"（相当于 git diff $(git merge-base HEAD {t.ref})，包含未提交的改动）")
    elif t.kind == "commit":
        parts.append(f"（相当于 git show {t.ref}）")
    if instructions.strip():
        parts.append(f"\n## 额外要求\n{instructions.strip()}")
    if t.diff:
        d = t.diff
        if len(d) > MAX_DIFF_CHARS:
            d = d[:MAX_DIFF_CHARS] + "\n…（diff 太长，后面截掉了；剩下的部分请自己用 git 命令查看）"
        parts.append(f"\n## 改动\n```diff\n{d}\n```")
    elif t.kind == "custom":
        parts.append("\n没有给出 diff：按上面的要求，自己用 git 和读文件确定要看的范围。")
    return "\n".join(parts)


def display_text(t: Target, instructions: str = "") -> str:
    s = f"/review {t.label}"
    return f"{s}：{instructions.strip()}" if instructions.strip() else s


_BLOCK_RE = re.compile(r"```ee-review\s*\n(.*?)```", re.S)


def parse_findings(text: str) -> dict | None:
    """取回答里最后一个 ```ee-review 块；解析失败返回 None（界面照常显示正文）。"""
    blocks = _BLOCK_RE.findall(text or "")
    if not blocks:
        return None
    try:
        data = json.loads(blocks[-1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    out = []
    for f in data.get("findings") or []:
        if not isinstance(f, dict) or not f.get("title"):
            continue
        try:
            pr = int(f.get("priority", 2))
        except (TypeError, ValueError):
            pr = 2
        out.append({"priority": min(3, max(0, pr)), "title": str(f["title"])[:200], "body": str(f.get("body") or ""),
                    "file": str(f.get("file") or ""), "line_start": f.get("line_start"),
                    "line_end": f.get("line_end"), "confidence": f.get("confidence")})
    out.sort(key=lambda x: x["priority"])
    return {"overall": "incorrect" if str(data.get("overall", "")).lower().startswith("in") else "correct",
            "summary": str(data.get("summary") or ""), "findings": out}
