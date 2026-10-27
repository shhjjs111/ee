"""git worktree 管理（D5-1）：在独立工作目录 + 独立分支里让 ee 干活，不碰主目录。

- 位置：~/.ee/worktrees/<仓库名>-<仓库路径哈希8>/<名字>，分支 ee/<名字>，基于当前 HEAD（或指定 base）。
- .worktreeinclude（仓库根，gitignore 语法）：新建时把主目录里“被 git 忽略但需要的”文件复制过去（.env、本地配置…）。
- 元数据：~/.ee/worktrees/index.json（路径、仓库、分支、基准提交、名字、创建时间）。
- 删除：有未提交改动时先在它自己的分支上自动提交一次（“删除前自动保存”），默认保留分支，可随时再检出；
  分支有未合并的提交时，要求 delete_branch 时显式 force。
- 合并回主目录：merge（--no-ff 保留历史）或 squash（改动放进主目录暂存区，用户自己提交）；主目录必须干净。
所有 git 调用走 ee.proc.run_capture（stdin=DEVNULL、超时杀进程树），Windows 路径用 / 传给 git。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

from ee.config import ee_home
from ee.core import gitinfo
from ee.proc import run_capture
from ee.tools.textdecode import smart_decode

_LOCK = threading.Lock()
BRANCH_PREFIX = "ee/"
INCLUDE_FILE = ".worktreeinclude"
MAX_INCLUDE_BYTES = 200 * 1024 * 1024


class WorktreeError(Exception):
    pass


def _git(args: list[str], cwd: Path | str, timeout: float = 60, check: bool = True) -> str:
    exe = gitinfo.git_exe()
    if not exe:
        raise WorktreeError("没有找到 git，worktree 需要安装 Git")
    code, out, err = run_capture([exe, "-c", "core.quotepath=false", "-c", "core.longpaths=true", *args], cwd=cwd, timeout=timeout)
    if check and code != 0:
        msg = smart_decode(err or out).strip() or f"git {' '.join(args[:2])} 失败（{code}）"
        raise WorktreeError(msg[:2000])
    return out.decode("utf-8", "replace")


def root_dir(home: Path | None = None) -> Path:
    return (home or ee_home()) / "worktrees"


def _index_file(home: Path | None = None) -> Path:
    return root_dir(home) / "index.json"


def _load(home: Path | None = None) -> list[dict]:
    try:
        data = json.loads(_index_file(home).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (FileNotFoundError, ValueError):
        return []


def _save(items: list[dict], home: Path | None = None) -> None:
    f = _index_file(home)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, f)


def _norm(p: Path | str) -> str:
    s = os.path.normpath(str(Path(p).resolve())).replace("\\", "/")
    return s.lower() if os.name == "nt" else s


def repo_root(cwd: Path | str) -> Path:
    out = _git(["rev-parse", "--show-toplevel"], cwd, timeout=10, check=False).strip()
    if not out:
        raise WorktreeError("这个目录不是 git 仓库，不能建 worktree（先 git init 并提交一次）")
    # 如果 cwd 本身是某个 worktree，找到主仓库
    common = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd, timeout=10, check=False).strip()
    if common and Path(common).name == ".git":
        return Path(common).parent.resolve()
    return Path(out).resolve()


_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def _slug(name: str) -> str:
    s = re.sub(r"[^\w\-.]+", "-", name.strip(), flags=re.UNICODE).strip("-.")
    s = re.sub(r"-{2,}", "-", s)
    s = s[:48].rstrip("-.")
    if s.split(".")[0].upper() in _WIN_RESERVED:     # Windows 下 CON、NUL.txt 这类名字建不了目录
        s = "wt-" + s
    return s


def _auto_name() -> str:
    return time.strftime("%m%d-%H%M%S") + "-" + os.urandom(2).hex()


def _copy_included(src_root: Path, dst_root: Path) -> list[str]:
    """按 .worktreeinclude 把被 git 忽略的文件复制进新 worktree。"""
    inc = src_root / INCLUDE_FILE
    if not inc.is_file():
        return []
    from ee.core.eeignore import EeIgnore, _Rule, _translate  # 复用 gitignore 语法解析
    rules = []
    for raw in inc.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        neg = line.startswith("!")
        line = line[1:] if neg else line
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        anchored = "/" in line
        line = line.lstrip("/")
        tail = "/.*$" if dir_only else "(?:/.*)?$"
        rx = re.compile("^" + ("" if anchored else "(?:.*/)?") + _translate(line) + tail,
                        re.I if os.name == "nt" else 0)
        rules.append(_Rule(rx, neg, dir_only, _norm(src_root)))
    matcher = EeIgnore(rules, [inc])
    out = _git(["ls-files", "--others", "--ignored", "--exclude-standard", "-z"], src_root, timeout=60, check=False)
    copied, total = [], 0
    for rel in [x for x in out.split("\0") if x]:
        src = src_root / rel
        if not src.is_file() or not matcher.ignored(src, False):
            continue
        size = src.stat().st_size
        if total + size > MAX_INCLUDE_BYTES:
            break
        dst = dst_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        total += size
        copied.append(rel.replace("\\", "/"))
    return copied


def create(cwd: Path | str, name: str | None = None, base: str | None = None, home: Path | None = None) -> dict:
    root = repo_root(cwd)
    if not _git(["rev-parse", "--verify", "-q", "HEAD"], root, timeout=10, check=False).strip():
        raise WorktreeError("仓库还没有任何提交，先提交一次再建 worktree")
    slug = _slug(name or "") or _auto_name()
    branch = BRANCH_PREFIX + slug
    if _git(["rev-parse", "--verify", "-q", f"refs/heads/{branch}"], root, timeout=10, check=False).strip():
        raise WorktreeError(f"分支 {branch} 已存在，换个名字")
    base_ref = (base or "HEAD").strip()
    if base_ref.startswith("-"):
        raise WorktreeError("base 不合法")
    base_sha = _git(["rev-parse", "--verify", f"{base_ref}^{{commit}}"], root, timeout=10).strip()
    h = hashlib.sha1(_norm(root).encode()).hexdigest()[:8]
    path = root_dir(home) / f"{_slug(root.name) or 'repo'}-{h}" / slug
    if path.exists():
        raise WorktreeError(f"目录已存在：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(["worktree", "add", "-b", branch, str(path).replace("\\", "/"), base_sha], root, timeout=180)
    try:
        copied = _copy_included(root, path)
    except Exception as e:  # noqa: BLE001  复制失败不影响 worktree 本身
        copied = [f"（复制 .worktreeinclude 文件失败：{e}）"]
    info = {"name": slug, "path": str(path), "repo": str(root), "branch": branch, "base": base_sha,
            "base_ref": base_ref, "created": time.time(), "included": copied[:200]}
    with _LOCK:
        items = [x for x in _load(home) if _norm(x["path"]) != _norm(path)]
        items.append(info)
        _save(items, home)
    return info


def _find(path: Path | str, home: Path | None = None) -> dict:
    key = _norm(path)
    for x in _load(home):
        if _norm(x["path"]) == key:
            return x
    raise WorktreeError(f"不是 ee 管理的 worktree：{path}")


def status(info: dict) -> dict:
    p = Path(info["path"])
    out = dict(info)
    out["exists"] = p.is_dir()
    if not out["exists"]:
        return out
    st = gitinfo.status(p)
    out["changed"] = st.changed
    out["untracked"] = st.untracked
    ahead = _git(["rev-list", "--count", f"{info['base']}..HEAD"], p, timeout=10, check=False).strip()
    out["commits"] = int(ahead) if ahead.isdigit() else 0
    return out


def list_for(cwd: Path | str | None = None, home: Path | None = None, with_status: bool = True) -> list[dict]:
    items = _load(home)
    if cwd:
        try:
            r = _norm(repo_root(cwd))
        except WorktreeError:
            return []
        items = [x for x in items if _norm(x["repo"]) == r]
    return [status(x) if with_status else x for x in items]


def info_for_dir(cwd: Path | str, home: Path | None = None) -> dict | None:
    """cwd 在某个 ee worktree 里时返回它的元数据。"""
    key = _norm(cwd)
    for x in _load(home):
        k = _norm(x["path"])
        if key == k or key.startswith(k + "/"):
            return x
    return None


def diff(path: Path | str, home: Path | None = None) -> dict:
    """相对基准的全部改动（已提交 + 未提交 + 未跟踪），按文件拆好给审阅面板。"""
    from ee.server.gitops import split_diff
    info = _find(path, home)
    p = Path(info["path"])
    _git(["add", "-A", "--intent-to-add"], p, timeout=60, check=False)    # 让未跟踪文件出现在 diff 里
    text = _git(["diff", "--no-color", "--no-ext-diff", info["base"]], p, timeout=60, check=False)
    files = split_diff(text)
    return {"worktree": status(info), "files": files,
            "additions": sum(f["additions"] for f in files), "deletions": sum(f["deletions"] for f in files)}


def _ident(cwd: Path) -> list[str]:
    """没配置 git 身份时用 ee 兜底（有就用用户自己的）。"""
    name = _git(["config", "user.name"], cwd, timeout=10, check=False).strip()
    email = _git(["config", "user.email"], cwd, timeout=10, check=False).strip()
    return [] if (name and email) else ["-c", "user.name=ee", "-c", "user.email=ee@localhost"]


def _dirty(p: Path) -> bool:
    """严格版的“有没有未提交改动”：git 出错/超时直接报错，不当成干净。"""
    return bool(_git(["status", "--porcelain", "-z", "--untracked-files=normal"], p, timeout=120).strip("\0 \n"))


def _autosave(p: Path, msg: str) -> str | None:
    """有未提交改动就在 worktree 自己的分支上提交一次，返回提交 sha。"""
    # 不用 gitinfo.status：它在超时/出错时返回“干净”，接下来的 --force 删除就会丢掉未提交的改动
    top = _git(["rev-parse", "--show-toplevel"], p, timeout=30, check=False).strip()
    if not top or _norm(top) != _norm(p):
        if any(p.iterdir()):
            raise WorktreeError(f"{p} 已不是有效的 git worktree，里面的文件没法自动保存；为免丢失没有删除，请手动检查后处理")
        return None
    if not _dirty(p):
        return None
    _git(["add", "-A"], p, timeout=120)
    _git([*_ident(p), "commit", "--no-verify", "-q", "-m", msg], p, timeout=120)
    return _git(["rev-parse", "HEAD"], p, timeout=10).strip()


def remove(path: Path | str, delete_branch: bool = False, force: bool = False, home: Path | None = None) -> dict:
    info = _find(path, home)
    p, root = Path(info["path"]), Path(info["repo"])
    saved = _autosave(p, "ee：删除 worktree 前自动保存") if p.is_dir() else None
    if delete_branch and not force:      # 先检查再删目录：拒绝时什么都不动（改动已安全提交在分支上）
        unmerged = _git(["rev-list", "--count", f"{info['base']}..refs/heads/{info['branch']}"], root, timeout=10,
                        check=False).strip()
        code, _, _ = run_capture([gitinfo.git_exe() or "git", "merge-base", "--is-ancestor",
                                  f"refs/heads/{info['branch']}", "HEAD"], cwd=root, timeout=10)
        if unmerged not in ("", "0") and code != 0:
            raise WorktreeError(f"分支 {info['branch']} 上有 {unmerged} 个未合并的提交；确定不要了请勾选强制删除")
    if p.is_dir():
        _git(["worktree", "remove", "--force", str(p).replace("\\", "/")], root, timeout=120, check=False)
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
        if p.exists():                   # 有文件被占用：保留登记，别留下列表里看不到的孤儿目录
            raise WorktreeError(f"目录没能删干净（可能有文件被其他程序占用）：{p}\n改动已保存在分支 {info['branch']} 上；关掉占用的程序后再删一次")
    _git(["worktree", "prune"], root, timeout=60, check=False)
    branch_deleted = False
    if delete_branch:
        _git(["branch", "-D", info["branch"]], root, timeout=30, check=False)
        branch_deleted = not _git(["rev-parse", "--verify", "-q", f"refs/heads/{info['branch']}"], root,
                                  timeout=10, check=False).strip()
    with _LOCK:
        _save([x for x in _load(home) if _norm(x["path"]) != _norm(p)], home)
    return {"removed": True, "autosaved": saved, "branch": info["branch"], "branch_deleted": branch_deleted}


def merge(path: Path | str, squash: bool = False, home: Path | None = None) -> dict:
    """把 worktree 的分支合并回主目录当前分支。主目录必须没有未提交改动。"""
    info = _find(path, home)
    p, root = Path(info["path"]), Path(info["repo"])
    if _dirty(root):
        raise WorktreeError("主目录有未提交的改动，先提交或暂存后再合并")
    saved = _autosave(p, "ee：合并前保存 worktree 改动") if p.is_dir() else None
    try:
        if squash:
            _git(["merge", "--squash", info["branch"]], root, timeout=180)
            result = "squash"
        else:
            _git([*_ident(root), "merge", "--no-ff", "--no-edit", info["branch"]], root, timeout=180)
            result = "merge"
    except WorktreeError as e:
        # 有冲突：回到合并前的状态，不把主目录留在半合并里
        _git(["merge", "--abort"], root, timeout=60, check=False)
        if squash:
            _git(["reset", "--merge"], root, timeout=60, check=False)
        raise WorktreeError(f"合并有冲突，已取消合并、主目录保持原样。可以在 worktree 里先合并主分支解决冲突再试。\n{e}")
    return {"merged": result, "autosaved": saved, "branch": info["branch"], "repo": str(root)}


# ------------------------------------------------------------------ 写型子代理用（D5-2）

AGENT_LOCK = threading.Lock()     # 同一进程里并行子代理建/收 worktree 时串行跑 git（避免 ref/config 锁冲突）


def agent_eligible(cwd: Path | str) -> tuple[bool, str]:
    """能不能给写型子代理建独立 worktree。返回 (能否, 不能的原因)。"""
    if not gitinfo.git_exe():
        return False, "没有安装 Git"
    top = _git(["rev-parse", "--show-toplevel"], cwd, timeout=10, check=False).strip()
    if not top:
        return False, "不是 git 仓库"
    if not _git(["rev-parse", "--verify", "-q", "HEAD"], cwd, timeout=10, check=False).strip():
        return False, "仓库还没有任何提交"
    return True, ""


def _sync_dirty(src_top: Path, dst: Path) -> str | None:
    """把主目录相对 HEAD 的未提交改动（含未跟踪、不含被忽略的文件）带进新 worktree 并提交成一个基线提交。

    补丁只应用到索引再检出（--cached + checkout），绕开 Windows 换行转换导致的“补丁对不上工作区”。
    返回基线提交 sha；没有未提交改动返回 None。
    """
    import tempfile
    exe = gitinfo.git_exe() or "git"
    code, patch, err = run_capture([exe, "-c", "core.quotepath=false", "diff", "HEAD", "--binary", "--no-color",
                                    "--no-ext-diff", "--no-renames", "--ignore-submodules"], cwd=src_top, timeout=120)
    if code != 0:
        raise WorktreeError("读取主目录未提交改动失败：" + smart_decode(err or b"")[:500])
    untracked = [x for x in _git(["ls-files", "--others", "--exclude-standard", "-z"], src_top, timeout=60,
                                 check=False).split("\0") if x]
    if not patch.strip() and not untracked:
        return None
    if patch.strip():
        fd, tmp = tempfile.mkstemp(suffix=".patch")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(patch)
            _git(["apply", "--cached", "--binary", "--whitespace=nowarn", tmp.replace("\\", "/")], dst, timeout=120)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        _git(["checkout-index", "-a", "-f"], dst, timeout=120)
        # 补丁删掉的文件：索引里已没有、工作区还在（新 worktree 里此时唯一的未跟踪文件就是它们）→ 删掉
        for rel in [x for x in _git(["ls-files", "--others", "--exclude-standard", "-z"], dst, timeout=60,
                                    check=False).split("\0") if x]:
            try:
                (dst / rel).unlink()
            except OSError:
                pass
    total = 0
    for rel in untracked:
        src = src_top / rel
        if not src.is_file():
            continue
        total += src.stat().st_size
        if total > MAX_INCLUDE_BYTES:
            break
        d = dst / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, d)
    _git(["add", "-A"], dst, timeout=120)
    if not _dirty(dst):
        return None
    _git([*_ident(dst), "commit", "--no-verify", "-q", "-m", "ee：同步主目录未提交的改动（子代理基线）"], dst, timeout=120)
    return _git(["rev-parse", "HEAD"], dst, timeout=10).strip()


def create_for_agent(cwd: Path | str, label: str = "", home: Path | None = None) -> dict:
    """给写型子代理建 worktree：基于 cwd 当前 HEAD，并带上主目录的未提交改动（作为基线提交）。

    返回的 info 额外带 `agent_base`（子代理改动的起点：有基线提交就是它，否则同 base）和 `cwd`（对应的子目录）。
    """
    cwd = Path(cwd).resolve()
    top = Path(_git(["rev-parse", "--show-toplevel"], cwd, timeout=10).strip()).resolve()
    head = _git(["rev-parse", "HEAD"], cwd, timeout=10).strip()
    slug = _slug(label)[:20].strip("-.")
    name = "agent-" + (slug + "-" if slug and slug.isascii() else "") + _auto_name()
    with AGENT_LOCK:
        info = create(cwd, name=name, base=head, home=home)
        p = Path(info["path"])
        try:
            baseline = _sync_dirty(top, p)
        except Exception as e:  # noqa: BLE001  同步失败：删掉这个 worktree，调用方退回串行
            try:
                remove(p, delete_branch=True, force=True, home=home)
            except Exception:  # noqa: BLE001
                pass
            raise WorktreeError(f"把主目录未提交的改动带进 worktree 失败：{e}") from None
    try:
        rel = cwd.relative_to(top)
    except ValueError:
        rel = Path()
    info["agent_base"] = baseline or info["base"]
    info["synced_dirty"] = bool(baseline)
    info["cwd"] = str(p / rel)
    with _LOCK:
        items = _load(home)
        for x in items:
            if _norm(x["path"]) == _norm(p):
                x["agent_base"] = info["agent_base"]
                x["agent"] = True
        _save(items, home)
    return info


def finish_agent(info: dict, message: str, home: Path | None = None) -> dict:
    """子代理结束：把它的改动提交在自己的分支上，统计相对起点的改动；一点改动都没有就删掉 worktree 和分支。"""
    p = Path(info["path"])
    base = info.get("agent_base") or info["base"]
    with AGENT_LOCK:
        if p.is_dir():
            _autosave(p, message)
        files = []
        out = _git(["diff", "--numstat", "-z", "--no-renames", f"{base}..HEAD"], p, timeout=60, check=False) \
            if p.is_dir() else ""
        parts = [x for x in out.split("\0") if x]
        for rec in parts:
            bits = rec.split("\t", 2)
            if len(bits) == 3:
                a, d, path = bits
                files.append({"path": path.strip(), "additions": int(a) if a.isdigit() else 0,
                              "deletions": int(d) if d.isdigit() else 0, "binary": a == "-"})
        log = _git(["log", "--format=%h %s", f"{base}..HEAD"], p, timeout=30, check=False).strip() if p.is_dir() else ""
        commits = [x for x in log.splitlines() if x.strip()]
        removed = False
        if not files:
            try:
                remove(p, delete_branch=True, force=True, home=home)
                removed = True
            except Exception:  # noqa: BLE001
                removed = False
    return {"path": str(p), "branch": info["branch"], "base": base, "files": files[:200],
            "file_count": len(files), "additions": sum(f["additions"] for f in files),
            "deletions": sum(f["deletions"] for f in files), "commits": commits[:20], "removed": removed,
            "synced_dirty": bool(info.get("synced_dirty"))}
