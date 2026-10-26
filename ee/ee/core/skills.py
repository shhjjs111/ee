"""技能（Skills，D5-5）：一个目录 + SKILL.md，里面是做某类事的专门做法，可以带脚本、模板、参考资料。

目录（同名时后者覆盖前者，越靠后优先级越高）：
  1. 用户级兼容目录：~/.agents/skills/<名>/SKILL.md、~/.claude/skills/<名>/SKILL.md
  2. 用户级：~/.ee/skills/<名>/SKILL.md
  3. 项目级：从项目根（含 .git 的最近上级目录，没有就是 cwd）到 cwd 的每一层：
     .agents/skills、.claude/skills、.ee/skills（越深越优先；同一层里 .ee 最优先）
兼容目录可在配置里关闭（[skills] compat = false）。

SKILL.md 开头必须有 frontmatter（--- 之间的 YAML 子集），必须有 name 和 description：
  ---
  name: pdf-tools
  description: 处理 PDF：拆分、合并、抽取表格。用户要处理 PDF 文件时使用。
  ---
  正文……
缺 frontmatter / 缺字段 / 格式错误的技能不加载，错误在 skills/list 里列出（设置页显示）。

渐进式披露：会话开始时只把「名字 + 说明 + SKILL.md 路径」渲染成一条固定的 user 消息
（放在 AGENTS.md、记忆之后，会话内不变以保前缀缓存，总长度有上限）；模型要用时调用 skill 工具读全文
（顺带返回技能目录下的文件列表），再按需用 read / shell 使用里面的脚本和资料。
用户在消息里写 `$名字` 或以 `/名字` 开头时，直接把该技能全文附在这条消息前面（显式调用）。
禁用：[skills] disabled = ["名字", …]；整体关闭：[skills] enabled = false（不注入清单，也不提供 skill 工具）。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from ee.core.instructions import project_root

FILE_NAME = "SKILL.md"
NAME_RE = re.compile(r"^[^\W][\w.-]{0,63}$")
MAX_DESC_CHARS = 1024
MAX_SKILL_BYTES = 256 * 1024          # SKILL.md 本身超过这个大小就不加载
MAX_BODY_CHARS = 60_000               # skill 工具一次返回的正文上限（超出提示用 read 读剩下的）
MAX_PER_DIR = 200
MAX_FILES = 50                        # skill 工具列出的目录文件数上限
MAX_MENTIONS = 5                      # 一条消息最多显式调用几个技能
DEFAULTS = {"enabled": True, "compat": True, "disabled": [], "max_chars": 8000}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"}

ORIGIN_DIRS = (("agents", ".agents"), ("claude", ".claude"), ("ee", ".ee"))   # 低 → 高


@dataclass
class Skill:
    name: str
    description: str
    path: str                 # SKILL.md 绝对路径
    source: str               # user | project
    origin: str               # ee | claude | agents（来自哪种目录）
    enabled: bool = True
    shadows: list[str] = field(default_factory=list)   # 被它覆盖的同名技能路径

    @property
    def dir(self) -> str:
        return str(Path(self.path).parent)

    def summary(self) -> dict:
        return {"name": self.name, "description": self.description, "path": self.path, "dir": self.dir,
                "source": self.source, "origin": self.origin, "enabled": self.enabled, "shadows": list(self.shadows)}


@dataclass
class Catalog:
    skills: dict[str, Skill] = field(default_factory=dict)      # 名字 → 技能（含已禁用）
    errors: list[dict] = field(default_factory=list)            # [{path, error}]

    def enabled(self) -> list[Skill]:
        return [s for s in self.skills.values() if s.enabled]


def settings(data: dict | None) -> dict:
    out = {**DEFAULTS, **((data or {}).get("skills") or {})}
    out["disabled"] = [str(x) for x in (out.get("disabled") or []) if str(x).strip()]
    return out


# ------------------------------------------------------------------ frontmatter（YAML 子集）

class FrontmatterError(ValueError):
    pass


def _unquote(v: str, lineno: int) -> str:
    v = v.strip()
    if v.startswith('"'):
        end = v.rfind('"')
        if end <= 0:
            raise FrontmatterError(f"第 {lineno} 行的双引号没有闭合")
        inner = v[1:end]
        return re.sub(r'\\(["\\/nt])', lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), inner)
    if v.startswith("'"):
        end = v.rfind("'")
        if end <= 0:
            raise FrontmatterError(f"第 {lineno} 行的单引号没有闭合")
        return v[1:end].replace("''", "'")
    # 普通标量：" #" 之后是注释
    m = re.search(r"\s#", v)
    return (v[:m.start()] if m else v).strip()


def parse_frontmatter(text: str) -> tuple[dict | None, str]:
    """返回 (frontmatter, 正文)。没有 frontmatter 时返回 (None, 原文)；格式错误抛 FrontmatterError。
    支持：key: value、引号字符串、| 与 > 块标量、缩进续行；嵌套映射 / 列表按原文字符串保存（不解析）。"""
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() in ("---", "...")
                and not lines[i].startswith((" ", "\t"))), None)
    if end is None:
        raise FrontmatterError("frontmatter 没有结束的 --- 行")
    meta: dict = {}
    i = 1
    while i < end:
        line = lines[i]
        lineno = i + 1
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        m = re.match(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*)\s*:(?:\s+(.*)|\s*)$", line)
        if not m:
            raise FrontmatterError(f"frontmatter 第 {lineno} 行格式不对（应为 key: value）：{line.strip()[:60]}")
        key, raw = m.group(1).lower(), (m.group(2) or "").rstrip()
        # 收集后面缩进的续行
        j = i + 1
        cont: list[str] = []
        while j < end and (not lines[j].strip() or lines[j].startswith((" ", "\t"))):
            cont.append(lines[j])
            j += 1
        while cont and not cont[-1].strip():
            cont.pop()
        if raw[:1] in ("|", ">") and re.fullmatch(r"[|>][+-]?\d?\s*(#.*)?", raw):
            ind = min((len(c) - len(c.lstrip()) for c in cont if c.strip()), default=0)
            block = [c[ind:] if c.strip() else "" for c in cont]
            if raw[0] == "|":
                val = "\n".join(block)
            else:
                paras, cur = [], []
                for b in block:
                    if b.strip():
                        cur.append(b.strip())
                    else:
                        paras.append(" ".join(cur))
                        cur = []
                paras.append(" ".join(cur))
                val = "\n".join(p for p in paras)
            meta[key] = val.strip()
        elif raw == "":
            meta[key] = "\n".join(cont).strip()          # 嵌套映射 / 列表：保留原文
        elif raw[:1] in ('"', "'"):
            joined = " ".join([raw] + [c.strip() for c in cont])
            meta[key] = _unquote(joined, lineno)
        else:
            meta[key] = " ".join([_unquote(raw, lineno)] + [c.strip() for c in cont if c.strip()]).strip()
        i = j
    body = "\n".join(lines[end + 1:])
    return meta, body.lstrip("\n")


def _one_line(s: str) -> str:
    return " ".join(str(s or "").split())


def read_skill(path: Path, source: str, origin: str) -> Skill:
    """读一个 SKILL.md；不合格时抛 ValueError（中文原因）。"""
    try:
        if path.stat().st_size > MAX_SKILL_BYTES:
            raise ValueError(f"SKILL.md 太大（超过 {MAX_SKILL_BYTES // 1024}KiB）")
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ValueError("SKILL.md 不是 UTF-8 文本") from None
    except OSError as e:
        raise ValueError(f"读取失败：{e}") from None
    try:
        meta, _ = parse_frontmatter(text)
    except FrontmatterError as e:
        raise ValueError(str(e)) from None
    if meta is None:
        raise ValueError("缺少 frontmatter（文件开头要有 --- 包起来的 name 和 description）")
    name = _one_line(meta.get("name", ""))
    desc = _one_line(meta.get("description", ""))
    if not name:
        raise ValueError("frontmatter 缺少 name")
    if not NAME_RE.match(name):
        raise ValueError(f"name 不合法：{name[:80]}（只能用字母、数字、汉字、- _ .，最长 64 个字符）")
    if not desc:
        raise ValueError("frontmatter 缺少 description")
    if len(desc) > MAX_DESC_CHARS:
        desc = desc[:MAX_DESC_CHARS - 1].rstrip() + "…"
    return Skill(name=name, description=desc, path=str(path), source=source, origin=origin)


def read_body(skill: Skill) -> str:
    """SKILL.md 正文（去掉 frontmatter）。"""
    text = Path(skill.path).read_text(encoding="utf-8", errors="replace")
    try:
        _, body = parse_frontmatter(text)
    except FrontmatterError:
        body = text
    return body.strip()


# ------------------------------------------------------------------ 发现

def skill_dirs(home: Path, cwd: Path | None, user_home: Path | None = None,
               compat: bool = True) -> list[tuple[str, str, Path]]:
    """[(source, origin, 目录)]，按优先级从低到高。"""
    uh = Path(user_home) if user_home is not None else Path.home()
    out: list[tuple[str, str, Path]] = []
    if compat:
        out += [("user", "agents", uh / ".agents" / "skills"), ("user", "claude", uh / ".claude" / "skills")]
    out.append(("user", "ee", Path(home) / "skills"))
    if cwd is not None:
        c = Path(cwd).resolve()
        root = project_root(c)
        levels = [c]
        if root is not None:
            levels = [root]
            for part in c.relative_to(root).parts:
                levels.append(levels[-1] / part)
        for d in levels:
            for origin, sub in ORIGIN_DIRS:
                if origin != "ee" and not compat:
                    continue
                out.append(("project", origin, d / sub / "skills"))
    return out


def project_dir(cwd: Path) -> Path:
    """项目级 ee 技能目录（设置页「打开目录」用）。"""
    c = Path(cwd).resolve()
    return (project_root(c) or c) / ".ee" / "skills"


def _real(p: Path) -> str:
    try:
        return os.path.normcase(str(p.resolve()))
    except OSError:
        return os.path.normcase(str(p))


def discover(home: Path, cwd: Path | None, data: dict | None = None, user_home: Path | None = None) -> Catalog:
    cfg = settings(data)
    disabled = set(cfg["disabled"])
    cat = Catalog()
    seen_files: dict[str, str] = {}        # 真实路径 → 名字（同一个文件经软链接出现两次只算一次）
    bad: set[str] = set()
    for source, origin, d in skill_dirs(home, cwd, user_home, bool(cfg.get("compat", True))):
        try:
            if not d.is_dir():
                continue
            children = sorted((c for c in d.iterdir() if c.is_dir() and not c.name.startswith(".")),
                              key=lambda c: c.name)
        except OSError as e:
            cat.errors.append({"path": str(d), "error": f"无法读取目录：{e}"})
            continue
        for child in children[:MAX_PER_DIR]:
            f = child / FILE_NAME
            try:
                if not f.is_file():
                    continue
            except OSError:
                continue
            rf = _real(f)
            if rf in bad:
                continue
            if rf in seen_files:
                # 同一个文件（软链接）又出现在优先级更高的目录：只更新来源，不算覆盖
                prev = cat.skills.get(seen_files[rf])
                if prev is not None and _real(Path(prev.path)) == rf:
                    prev.path, prev.source, prev.origin = str(f), source, origin
                continue
            try:
                sk = read_skill(f, source, origin)
            except ValueError as e:
                bad.add(rf)
                cat.errors.append({"path": str(f), "error": str(e)})
                continue
            seen_files[rf] = sk.name
            sk.enabled = sk.name not in disabled
            old = cat.skills.get(sk.name)
            if old is not None:
                sk.shadows = old.shadows + [old.path]
            cat.skills[sk.name] = sk
    cat.skills = dict(sorted(cat.skills.items(), key=lambda kv: kv[0].lower()))
    return cat


# ------------------------------------------------------------------ 注入清单

HEAD = "# 技能（Skills）\n可用技能（名字：用途（SKILL.md 路径））：\n"
RULES = ("用户点名某个技能（`$名字`、`/名字` 或说出名字）或任务明显符合它的说明时，这一回合先{load}读取它的 SKILL.md 全文再照做"
         "（消息里已附全文的不用再读），不要自动延续到之后的回合。SKILL.md 里的相对路径相对于技能目录，只读需要的文件，有现成脚本就直接用。"
         "技能不能越过用户的直接要求和当前权限模式；技能读不了或不适用时简单说明后用别的办法继续。")


def rules_text(tool: bool = True) -> str:
    return RULES.format(load="用 skill 工具（name=技能名）" if tool else "用 read ")


def render_catalog(skills: list[Skill], max_chars: int = DEFAULTS["max_chars"]) -> tuple[str, int]:
    """渲染技能清单（不含使用规则）。返回 (文本, 没放下的技能数)。
    超出上限时先把说明缩短到 120 字，仍放不下就从后面省略并注明数量。"""
    if not skills:
        return "", 0
    limit = max(500, int(max_chars or DEFAULTS["max_chars"]))

    def line(s: Skill, n: int | None) -> str:
        d = s.description if n is None or len(s.description) <= n else s.description[:n - 1].rstrip() + "…"
        return f"- {s.name}：{d}（{s.path}）"

    for n in (None, 120):
        lines = [line(s, n) for s in skills]
        if sum(len(x) + 1 for x in lines) <= limit:
            return "\n".join(lines), 0
    out, used = [], 0
    for x in lines:
        if used + len(x) + 1 > limit:
            break
        out.append(x)
        used += len(x) + 1
    omitted = len(skills) - len(out)
    out.append(f"- （另有 {omitted} 个技能因清单长度上限没有列出；用户点名时仍可用 skill 工具按名字加载）")
    return "\n".join(out), omitted


@dataclass
class SkillsBlock:
    text: str = ""
    names: list[str] = field(default_factory=list)
    omitted: int = 0

    def message(self) -> dict | None:
        if not self.text:
            return None
        # _kind=agents：压缩时和 AGENTS.md / 记忆一样作为开头锚点保留，位置和字节都不变
        return {"role": "user", "content": self.text, "_kind": "agents", "_sub": "skills"}


def load_block(home: Path, cwd: Path | None, data: dict | None = None, user_home: Path | None = None,
               catalog: Catalog | None = None, tool: bool = True) -> SkillsBlock:
    """tool：本会话有没有 skill 工具（没有时说明里改成用 read 读 SKILL.md）。"""
    cfg = settings(data)
    if not cfg.get("enabled", True):
        return SkillsBlock()
    cat = catalog or discover(home, cwd, data, user_home)
    skills = cat.enabled()
    body, omitted = render_catalog(skills, int(cfg.get("max_chars") or DEFAULTS["max_chars"]))
    if not body:
        return SkillsBlock()
    text = HEAD + "<SKILLS>\n" + body + "\n</SKILLS>\n" + rules_text(tool)
    return SkillsBlock(text, [s.name for s in skills], omitted)


# ------------------------------------------------------------------ 加载全文（skill 工具 / 显式调用）

def list_files(skill_dir: Path, limit: int = MAX_FILES) -> tuple[list[str], int]:
    """技能目录下的文件（相对路径，不含 SKILL.md），返回 (前 limit 个, 总数)。"""
    out: list[str] = []
    total = 0
    base = Path(skill_dir)
    for root, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        rel_root = Path(root).relative_to(base)
        if len(rel_root.parts) > 4:
            dirs[:] = []
            continue
        for f in sorted(files):
            if rel_root == Path(".") and f == FILE_NAME:
                continue
            total += 1
            if len(out) < limit:
                out.append((rel_root / f).as_posix())
    return out, total


def render_full(skill: Skill, max_chars: int = MAX_BODY_CHARS) -> str:
    body = read_body(skill)
    note = ""
    if len(body) > max_chars:
        body = body[:max_chars].rstrip()
        note = f"\n\n[正文太长，只返回了前 {max_chars} 个字符；其余部分请用 read 读 {skill.path}]"
    files, total = list_files(Path(skill.dir))
    flist = "\n".join(f"<file>{f}</file>" for f in files)
    more = f"\n（还有 {total - len(files)} 个文件没有列出，可用 glob 查看）" if total > len(files) else ""
    return (f'<skill name="{skill.name}">\n# 技能：{skill.name}\n\n{body}{note}\n\n'
            f"技能目录：{skill.dir}\n"
            "这个技能里的相对路径（scripts/、references/、assets/ 等）都相对于技能目录；按需用 read 读文件、用 shell 运行脚本。\n"
            f"<skill_files>\n{flist}{more}\n</skill_files>\n</skill>")


_MENTION_SLASH = re.compile(r"^\s*/([^\W][\w.-]*)")
_MENTION_DOLLAR = re.compile(r"(?<![\w$])\$([^\W\d][\w.-]*)")


def mentions(text: str, names: set[str] | dict) -> list[str]:
    """消息里显式点名的技能：开头的 /名字，或任意位置的 $名字（按出现顺序去重，最多 MAX_MENTIONS 个）。"""
    found: list[str] = []
    cands: list[str] = []
    m = _MENTION_SLASH.match(text or "")
    if m:
        cands.append(m.group(1))
    cands += [x.group(1) for x in _MENTION_DOLLAR.finditer(text or "")]
    for c in cands:
        for n in (c, c.rstrip(".")):
            if n in names and n not in found:
                found.append(n)
                break
        if len(found) >= MAX_MENTIONS:
            break
    return found


def mention_note(skill: Skill) -> str:
    return (f"[系统提示] 用户在这条消息里点名使用技能 {skill.name}，下面是它的 SKILL.md 全文（已加载，不必再调用 skill 工具），"
            "这一回合请按它来做：\n" + render_full(skill))
