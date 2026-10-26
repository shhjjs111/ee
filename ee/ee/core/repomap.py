"""仓库地图（repo map）：用 tree-sitter 抽出各文件的定义和标识符引用，按“谁引用谁”建图做 PageRank 排名，
在 token 预算内输出“文件 → 关键定义签名行”，让模型在陌生项目里先看全局结构再决定读哪些文件。

做法：
1. 列文件：git 仓库用 `git ls-files --cached --others --exclude-standard`（尊重 .gitignore），否则 os.walk；
   都再跳过 SKIP_DIRS、隐藏目录、.eeignore 排除的、不认识的扩展名、过大/压缩过的文件；文件数和总耗时有上限。
2. 解析：每种语言一份“定义”查询（类/函数/方法/接口/类型…），签名取定义起始那一行；
   引用不靠查询，而是统计文件里所有标识符叶子节点（各语言通用，能覆盖调用、类型标注、继承、import）。
3. 排名：文件为节点，A 文件用到 B 文件定义的名字就连 A→B（权重 = 名字系数 × √次数；长而具体的名字、
   用户关注的名字加权，下划线开头、到处都有定义的名字降权），PageRank 后把每个文件的分数按出边分给
   被引用的定义；关注的文件作为个性化向量。
4. 裁剪：按定义得分排序，二分找出预算内能放下的最多条数，按文件分组输出（文件内按行号）。
5. 缓存：按文件 mtime/大小增量缓存到 ~/.ee/cache/repomap/<项目路径哈希>.json，进程内也保留一份。

依赖（tree-sitter + tree-sitter-language-pack）缺失或某种语言加载失败时不报错：没有地图或少这种语言。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import stat
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("ee.repomap")

CACHE_VERSION = 1
DEFAULTS = {"enabled": True, "auto_inject": False, "max_tokens": 1536, "inject_tokens": 1024,
            "max_files": 3000, "time_limit": 20.0}
MAX_FILE_BYTES = 1024 * 1024
SIG_MAX_CHARS = 160
MIN_IDENT_LEN = 2

# 扩展名 → 语言（tree-sitter-language-pack 里的名字）。打包时 ee-backend.spec 只收这些语言的动态库。
EXT_LANG: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".kt": "kotlin", ".kts": "kotlin",
    ".lua": "lua",
}
LANGUAGES = sorted(set(EXT_LANG.values()))

# ------------------------------------------------------------------ 定义查询（只抓定义；引用另行统计）
# 捕获名约定：@name.<kind> 是名字节点，@def.<kind> 是整个定义节点（签名取它的起始行）。

_JS_COMMON = """
(function_declaration name: (identifier) @name.function) @def.function
(generator_function_declaration name: (identifier) @name.function) @def.function
(class_declaration name: (_) @name.class) @def.class
(method_definition name: (property_identifier) @name.method) @def.method
(lexical_declaration (variable_declarator name: (identifier) @name.function
  value: [(arrow_function) (function_expression)]) @def.function)
(variable_declaration (variable_declarator name: (identifier) @name.function
  value: [(arrow_function) (function_expression)]) @def.function)
"""
_TS_EXTRA = """
(function_signature name: (identifier) @name.function) @def.function
(abstract_class_declaration name: (type_identifier) @name.class) @def.class
(interface_declaration name: (type_identifier) @name.interface) @def.interface
(type_alias_declaration name: (type_identifier) @name.type) @def.type
(enum_declaration name: (identifier) @name.enum) @def.enum
(abstract_method_signature name: (property_identifier) @name.method) @def.method
(internal_module name: (identifier) @name.module) @def.module
"""

QUERIES: dict[str, str] = {
    "python": """
(class_definition name: (identifier) @name.class) @def.class
(function_definition name: (identifier) @name.function) @def.function
(module (assignment left: (identifier) @name.constant) @def.constant)
(module (expression_statement (assignment left: (identifier) @name.constant) @def.constant))
""",
    "javascript": _JS_COMMON,
    "typescript": _JS_COMMON + _TS_EXTRA,
    "tsx": _JS_COMMON + _TS_EXTRA,
    "go": """
(function_declaration name: (identifier) @name.function) @def.function
(method_declaration name: (field_identifier) @name.method) @def.method
(type_spec name: (type_identifier) @name.type) @def.type
""",
    "rust": """
(struct_item name: (type_identifier) @name.struct) @def.struct
(enum_item name: (type_identifier) @name.enum) @def.enum
(union_item name: (type_identifier) @name.struct) @def.struct
(type_item name: (type_identifier) @name.type) @def.type
(trait_item name: (type_identifier) @name.trait) @def.trait
(function_item name: (identifier) @name.function) @def.function
(function_signature_item name: (identifier) @name.function) @def.function
(mod_item name: (identifier) @name.module) @def.module
(macro_definition name: (identifier) @name.macro) @def.macro
""",
    "java": """
(class_declaration name: (identifier) @name.class) @def.class
(interface_declaration name: (identifier) @name.interface) @def.interface
(enum_declaration name: (identifier) @name.enum) @def.enum
(record_declaration name: (identifier) @name.class) @def.class
(method_declaration name: (identifier) @name.method) @def.method
(constructor_declaration name: (identifier) @name.method) @def.method
""",
    "c": """
(struct_specifier name: (type_identifier) @name.struct body: (_)) @def.struct
(union_specifier name: (type_identifier) @name.struct body: (_)) @def.struct
(enum_specifier name: (type_identifier) @name.enum body: (_)) @def.enum
(type_definition declarator: (type_identifier) @name.type) @def.type
(function_definition declarator: (function_declarator declarator: (identifier) @name.function)) @def.function
(function_definition declarator: (pointer_declarator declarator: (function_declarator declarator: (identifier) @name.function))) @def.function
(declaration declarator: (function_declarator declarator: (identifier) @name.function)) @def.function
(preproc_function_def name: (identifier) @name.macro) @def.macro
""",
    "cpp": """
(struct_specifier name: (type_identifier) @name.struct body: (_)) @def.struct
(class_specifier name: (type_identifier) @name.class body: (_)) @def.class
(union_specifier name: (type_identifier) @name.struct body: (_)) @def.struct
(enum_specifier name: (type_identifier) @name.enum body: (_)) @def.enum
(type_definition declarator: (type_identifier) @name.type) @def.type
(namespace_definition name: (namespace_identifier) @name.module) @def.module
(function_definition declarator: (function_declarator declarator: (identifier) @name.function)) @def.function
(function_definition declarator: (function_declarator declarator: (field_identifier) @name.method)) @def.method
(function_definition declarator: (function_declarator declarator: (qualified_identifier name: (identifier) @name.method))) @def.method
(function_definition declarator: (pointer_declarator declarator: (function_declarator declarator: (identifier) @name.function))) @def.function
(field_declaration declarator: (function_declarator declarator: (field_identifier) @name.method)) @def.method
(declaration declarator: (function_declarator declarator: (identifier) @name.function)) @def.function
""",
    "csharp": """
(class_declaration name: (identifier) @name.class) @def.class
(struct_declaration name: (identifier) @name.struct) @def.struct
(interface_declaration name: (identifier) @name.interface) @def.interface
(enum_declaration name: (identifier) @name.enum) @def.enum
(record_declaration name: (identifier) @name.class) @def.class
(method_declaration name: (identifier) @name.method) @def.method
(constructor_declaration name: (identifier) @name.method) @def.method
(namespace_declaration name: (_) @name.module) @def.module
""",
    "ruby": """
(class name: (constant) @name.class) @def.class
(module name: (constant) @name.module) @def.module
(method name: (_) @name.method) @def.method
(singleton_method name: (_) @name.method) @def.method
""",
    "php": """
(class_declaration name: (name) @name.class) @def.class
(interface_declaration name: (name) @name.interface) @def.interface
(trait_declaration name: (name) @name.trait) @def.trait
(enum_declaration name: (name) @name.enum) @def.enum
(function_definition name: (name) @name.function) @def.function
(method_declaration name: (name) @name.method) @def.method
""",
    "kotlin": """
(class_declaration (type_identifier) @name.class) @def.class
(object_declaration (type_identifier) @name.class) @def.class
(function_declaration (simple_identifier) @name.function) @def.function
""",
    "lua": """
(function_declaration name: (identifier) @name.function) @def.function
(function_declaration name: (dot_index_expression field: (identifier) @name.function)) @def.function
(function_declaration name: (method_index_expression method: (identifier) @name.method)) @def.method
""",
}

# 引用统计时跳过的常见关键字/内置名（只是降噪，不必完整）
_STOP = {"self", "this", "cls", "super", "None", "True", "False", "null", "nil", "true", "false", "undefined",
         "int", "str", "bool", "float", "list", "dict", "set", "tuple", "len", "print", "range", "string",
         "void", "var", "let", "const", "new", "return", "err", "ctx", "i", "j", "k", "x", "y", "_"}


# ------------------------------------------------------------------ tree-sitter 加载（失败即降级）

class _TS:
    """懒加载 tree-sitter 与语言包；每种语言的 Parser/Query 缓存。任何一步失败都只是“这种语言不可用”。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.loaded = False
        self.ts = None
        self.pack = None
        self.error = ""
        self.langs: dict[str, tuple] = {}
        self.missing: set[str] = set()

    def load(self) -> bool:
        if self.loaded:
            return self.ts is not None
        with self.lock:
            if not self.loaded:
                try:
                    import tree_sitter as ts
                    import tree_sitter_language_pack as pack
                    self.ts, self.pack = ts, pack
                except Exception as e:  # noqa: BLE001
                    self.error = f"{type(e).__name__}: {e}"
                    log.info("仓库地图不可用（tree-sitter 未安装或加载失败）：%s", self.error)
                self.loaded = True
        return self.ts is not None

    def get(self, lang: str):
        """返回 (parser, query)；不可用返回 None。"""
        if not self.load() or lang in self.missing:
            return None
        hit = self.langs.get(lang)
        if hit is not None:
            return hit
        with self.lock:
            hit = self.langs.get(lang)
            if hit is not None:
                return hit
            try:
                language = self.pack.get_language(lang)
                parser = self.ts.Parser(language)
                query = self.ts.Query(language, QUERIES[lang])
                hit = (parser, query)
                self.langs[lang] = hit
                return hit
            except Exception as e:  # noqa: BLE001
                log.info("仓库地图：语言 %s 不可用：%s", lang, e)
                self.missing.add(lang)
                return None

    def captures(self, query, node) -> dict:
        try:
            return self.ts.QueryCursor(query).captures(node)
        except AttributeError:      # 旧版 py-tree-sitter：Query.captures 返回 [(node, name)]
            out: dict = defaultdict(list)
            for n, name in query.captures(node):
                out[name].append(n)
            return out


_ts = _TS()


def available() -> bool:
    """tree-sitter 和语言包能否导入（不保证每种语言都在）。"""
    return _ts.load()


def unavailable_reason() -> str:
    _ts.load()
    return _ts.error


# ------------------------------------------------------------------ 单文件抽取

def _is_ident(node_type: str) -> bool:
    return node_type.endswith("identifier") or node_type in ("constant", "name")


def extract(lang: str, src: bytes) -> tuple[list[list], dict[str, int]] | None:
    """解析一个文件：返回 (defs, refs)。defs = [[名字, 种类, 行号(1 起), 签名行], ...]，refs = {名字: 次数}。
    语言不可用返回 None。"""
    pq = _ts.get(lang)
    if pq is None:
        return None
    parser, query = pq
    tree = parser.parse(src)
    caps = _ts.captures(query, tree.root_node)
    lines = src.split(b"\n")
    defs: list[list] = []
    seen: set[tuple[str, int]] = set()
    def_name_pos: set[tuple[int, int]] = set()
    for cap, nodes in caps.items():
        if not cap.startswith("name."):
            continue
        kind = cap[5:]
        for n in nodes:
            name = n.text.decode("utf-8", "replace") if n.text is not None else ""
            if not name:
                continue
            def_name_pos.add((n.start_byte, n.end_byte))
            row = _def_row(n, cap, caps)
            key = (name, row)
            if key in seen:
                continue
            seen.add(key)
            sig = lines[row].decode("utf-8", "replace").rstrip() if row < len(lines) else name
            if len(sig) > SIG_MAX_CHARS:
                sig = sig[:SIG_MAX_CHARS - 1] + "…"
            defs.append([name, kind, row + 1, sig])
    defs.sort(key=lambda d: (d[2], d[0]))
    refs: Counter = Counter()
    cur = tree.walk()
    visited_children = False
    while True:
        if not visited_children:
            node = cur.node
            if node.child_count == 0 or node.type in ("constant",):
                t = node.type
                if _is_ident(t) and (node.start_byte, node.end_byte) not in def_name_pos:
                    name = node.text.decode("utf-8", "replace") if node.text is not None else ""
                    if len(name) >= MIN_IDENT_LEN and name not in _STOP:
                        refs[name] += 1
            if cur.goto_first_child():
                continue
            visited_children = True
        if cur.goto_next_sibling():
            visited_children = False
            continue
        if not cur.goto_parent():
            break
    return defs, dict(refs)


def _def_row(name_node, cap: str, caps: dict) -> int:
    """签名行：包住名字节点的那个 @def 节点的起始行（如 Python 装饰器后的 def 行、C 的返回类型行）。"""
    kind = cap[5:]
    best = None
    for d in caps.get("def." + kind, ()):
        if d.start_byte <= name_node.start_byte and name_node.end_byte <= d.end_byte:
            if best is None or d.start_byte > best.start_byte:
                best = d
    node = best or name_node
    row = node.start_point[0]
    # 跳过定义节点开头的装饰器/注解行：签名取名字所在行与定义起始行中更具体的那行
    if best is not None and name_node.start_point[0] - row > 0:
        first = best.children[0].type if best.child_count else ""
        if first in ("decorator", "attribute_list", "modifiers", "annotation", "marker_annotation"):
            row = name_node.start_point[0]
    return row


# ------------------------------------------------------------------ 列文件

def _skip_dirs() -> set[str]:
    from ee.tools.search import SKIP_DIRS
    return set(SKIP_DIRS)


def _is_git_repo(root: Path) -> bool:
    return (root / ".git").exists()


def list_files(root: Path, max_files: int) -> tuple[list[str], int]:
    """返回 (相对路径列表（/ 分隔）, 候选总数)。超出上限时浅层目录优先。"""
    skip = _skip_dirs()
    rels: list[str] = []
    got = False
    if _is_git_repo(root):
        try:
            from ee.proc import run_capture
            code, out, _ = run_capture(["git", "-c", "core.quotepath=false", "ls-files", "-z", "--cached",
                                        "--others", "--exclude-standard"], cwd=root, timeout=15)
            if code == 0:
                rels = [p for p in out.decode("utf-8", "replace").split("\0") if p]
                got = True
        except Exception:  # noqa: BLE001  没装 git 等：回退 os.walk
            got = False
    if not got:
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if d not in skip and not d.startswith(".")]
            base = os.path.relpath(dp, root)
            for f in fns:
                if os.path.splitext(f)[1].lower() in EXT_LANG:
                    rels.append(f if base == "." else os.path.join(base, f).replace("\\", "/"))
            if len(rels) > max_files * 4:
                break
    out_list = []
    for r in rels:
        r = r.replace("\\", "/")
        if os.path.splitext(r)[1].lower() not in EXT_LANG:
            continue
        parts = r.split("/")
        if any(p in skip or (p.startswith(".") and p not in (".", "..")) for p in parts[:-1]):
            continue
        out_list.append(r)
    ig = None
    try:
        from ee.core.eeignore import for_dir
        ig = for_dir(root)
        if ig.empty:
            ig = None
    except Exception:  # noqa: BLE001
        ig = None
    if ig is not None:
        out_list = [r for r in out_list if not ig.ignored(root / r, False)]
    total = len(out_list)
    out_list.sort(key=lambda r: (r.count("/"), r))
    return out_list[:max_files], total


# ------------------------------------------------------------------ 缓存

def cache_path(home: Path, root: Path) -> Path:
    h = hashlib.sha1(str(root.resolve()).lower().encode("utf-8") if os.name == "nt"
                     else str(root.resolve()).encode("utf-8")).hexdigest()[:16]
    return Path(home) / "cache" / "repomap" / f"{h}.json"


def _pack_version() -> str:
    try:
        from importlib.metadata import version
        return version("tree-sitter-language-pack")
    except Exception:  # noqa: BLE001
        return "?"


@dataclass
class FileTags:
    mtime_ns: int
    size: int
    defs: list
    refs: dict


@dataclass
class BuildStats:
    total: int = 0          # 候选文件数（截断前）
    listed: int = 0         # 参与的文件数
    parsed: int = 0         # 本次新解析的
    cached: int = 0         # 命中缓存的
    timed_out: bool = False
    missing_langs: list = field(default_factory=list)


class RepoMap:
    """一个项目根目录的地图生成器（同一根目录在进程内共用，见 for_root）。"""

    def __init__(self, root: Path, home: Path):
        self.root = Path(root).resolve()
        self.home = Path(home)
        self.cache_file = cache_path(self.home, self.root)
        self.files: dict[str, FileTags] = {}
        self.lock = threading.Lock()
        self._loaded = False
        self._dirty = False

    # ---- 缓存读写
    def _load_cache(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            data = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if data.get("version") != CACHE_VERSION or data.get("pack") != _pack_version():
            return
        for rel, v in (data.get("files") or {}).items():
            try:
                self.files[rel] = FileTags(int(v[0]), int(v[1]), list(v[2]), dict(v[3]))
            except (TypeError, ValueError, IndexError):
                continue

    def _save_cache(self) -> None:
        if not self._dirty:
            return
        data = {"version": CACHE_VERSION, "pack": _pack_version(), "root": str(self.root),
                "files": {r: [t.mtime_ns, t.size, t.defs, t.refs] for r, t in self.files.items()}}
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_file.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.cache_file)
            self._dirty = False
        except OSError as e:
            log.info("仓库地图缓存写入失败：%s", e)

    # ---- 增量解析
    def refresh(self, max_files: int = 3000, time_limit: float = 20.0) -> BuildStats:
        """按 mtime/大小增量更新所有文件的 tags；超时就停（已解析的照样缓存，下次继续）。"""
        st = BuildStats()
        with self.lock:
            self._load_cache()
            rels, st.total = list_files(self.root, max_files)
            st.listed = len(rels)
            keep = set(rels)
            for r in list(self.files):
                if r not in keep:
                    del self.files[r]
                    self._dirty = True
            deadline = time.monotonic() + max(0.5, float(time_limit))
            missing: set[str] = set()
            for r in rels:
                p = self.root / r
                try:
                    s = p.stat()
                    # 只解析项目内的普通文件：FIFO/设备会让读取一直阻塞；指向项目外的软链接不属于这个仓库
                    ok = stat.S_ISREG(s.st_mode) and _inside(p, self.root)
                except OSError:
                    ok = False
                if not ok:
                    if self.files.pop(r, None) is not None:
                        self._dirty = True
                    continue
                old = self.files.get(r)
                if old is not None and old.mtime_ns == s.st_mtime_ns and old.size == s.st_size:
                    st.cached += 1
                    continue
                if time.monotonic() > deadline:
                    st.timed_out = True
                    self.files.pop(r, None)      # 旧数据已过期：不用，等下次补
                    continue
                lang = EXT_LANG[os.path.splitext(r)[1].lower()]
                tags = None
                if s.st_size <= MAX_FILE_BYTES:
                    try:
                        src = p.read_bytes()
                        if not _looks_minified(src):
                            res = extract(lang, src)
                            if res is None:
                                missing.add(lang)
                            else:
                                tags = FileTags(s.st_mtime_ns, s.st_size, res[0], res[1])
                    except Exception as e:  # noqa: BLE001  单个文件坏了不影响整体
                        log.debug("仓库地图解析失败 %s：%s", r, e)
                if tags is None:
                    tags = FileTags(s.st_mtime_ns, s.st_size, [], {})
                self.files[r] = tags
                self._dirty = True
                st.parsed += 1
            st.missing_langs = sorted(missing)
            self._save_cache()
        return st

    # ---- 排名
    def ranked_defs(self, focus_files: set[str] | None = None, focus_idents: set[str] | None = None) -> list[tuple]:
        """返回按得分降序的 [(得分, 相对路径, def)]。"""
        focus_files = focus_files or set()
        focus_idents = focus_idents or set()
        files = self.files
        # 只在同一语言族内连边（Python 里的 Session 不会指向 TS 里同名的类型）
        fam = {r: _family(r) for r in files}
        defines: dict[tuple[str, str], set[str]] = defaultdict(set)
        for r, t in files.items():
            for d in t.defs:
                defines[(fam[r], d[0])].add(r)
        # 边：referencer → definer，按名字记权重
        edges: dict[str, dict[tuple[str, str], float]] = defaultdict(dict)   # src -> {(dst, ident): w}
        popular: dict[tuple[str, str], float] = defaultdict(float)          # 被多少个别的文件用到（加权）
        for r, t in files.items():
            for ident, n in t.refs.items():
                definers = defines.get((fam[r], ident))
                if not definers:
                    continue
                mul = _ident_mul(ident, len(definers), focus_idents)
                for dst in definers:
                    w = mul * math.sqrt(n) * (0.1 if dst == r else 1.0)
                    key = (dst, ident)
                    edges[r][key] = edges[r].get(key, 0.0) + w
                    if dst != r:
                        popular[key] += mul
        nodes = list(files)
        if not nodes:
            return []
        pr = _pagerank(nodes, edges, focus_files)
        score: dict[tuple[str, str], float] = defaultdict(float)
        for src, outs in edges.items():
            tot = sum(outs.values())
            if tot <= 0:
                continue
            for (dst, ident), w in outs.items():
                score[(dst, ident)] += pr.get(src, 0.0) * w / tot
        # 综合两种信号：PageRank 流量（被重要文件引用）+ 被引用的广度（被很多文件用到，如测试大量使用的核心类）
        tot_flow = sum(score.values()) or 1.0
        tot_pop = sum(popular.values()) or 1.0
        out: list[tuple] = []
        for r, t in files.items():
            nd = max(1, len(t.defs))
            for d in t.defs:
                key = (r, d[0])
                s = 0.6 * score.get(key, 0.0) / tot_flow + 0.4 * popular.get(key, 0.0) / tot_pop
                # 没人引用的定义也给一个很小的底分（按文件重要度），保证小项目/叶子文件也能出现
                s += pr.get(r, 0.0) * 1e-3 / nd
                if not d[3][:1].isspace():
                    s *= 1.5                     # 顶层定义（类、模块级函数）比嵌套的方法更能代表文件
                if d[0] in focus_idents:
                    s += 1.0                     # 关注的符号：排在最前（PageRank 总和为 1，+1 必然领先）
                elif r in focus_files:
                    s += 0.5 + s                 # 关注的文件：它的定义排在其他文件前面
                out.append((s, r, d))
        out.sort(key=lambda x: (-x[0], x[1], x[2][2]))
        return out

    # ---- 输出
    def render(self, max_tokens: int, focus: list[str] | None = None, subdir: str = "",
               max_files: int = 3000, time_limit: float = 20.0, ignore=None) -> tuple[str, dict]:
        st = self.refresh(max_files=max_files, time_limit=time_limit)
        ffiles, fidents = self._split_focus(focus or [])
        ranked = self.ranked_defs(ffiles, fidents)
        if ignore is not None:      # 工作目录（低于地图根）自己的 .eeignore
            ranked = [x for x in ranked if not ignore.ignored(self.root / x[1], False)]
        sub = subdir.strip("/").replace("\\", "/") if subdir else ""
        if sub and sub != ".":
            ranked = [x for x in ranked if x[1] == sub or x[1].startswith(sub + "/")]
        n = _fit(ranked, max_tokens)
        text = _render_lines(ranked[:n])
        info = {"files_total": st.total, "files_listed": st.listed, "parsed": st.parsed, "cached": st.cached,
                "timed_out": st.timed_out, "missing_langs": st.missing_langs, "defs_total": len(ranked),
                "defs_shown": n, "files_shown": len({x[1] for x in ranked[:n]}), "tokens": _tokens(text)}
        return text, info

    def _split_focus(self, focus: list[str]) -> tuple[set[str], set[str]]:
        files: set[str] = set()
        idents: set[str] = set()
        known = list(self.files)
        for raw in focus:
            f = str(raw or "").strip().replace("\\", "/")
            if not f:
                continue
            if "/" in f or "." in os.path.basename(f) and os.path.splitext(f)[1].lower() in EXT_LANG:
                try:
                    p = Path(f)
                    if p.is_absolute():
                        f = p.resolve().relative_to(self.root).as_posix()
                except (ValueError, OSError):
                    pass
                f = f.removeprefix("./")
                hits = [k for k in known if k == f or k.endswith("/" + f)]
                if hits:
                    files.update(hits)
                    continue
            hits = [k for k in known if os.path.basename(k) == f]
            if hits:
                files.update(hits)
                continue
            for part in _split_words(f):
                idents.add(part)
        return files, idents


_FAMILY = {"javascript": "js", "typescript": "js", "tsx": "js", "c": "c", "cpp": "c"}


def _family(rel: str) -> str:
    lang = EXT_LANG.get(os.path.splitext(rel)[1].lower(), "")
    return _FAMILY.get(lang, lang)


def _split_words(s: str) -> list[str]:
    import re
    return [w for w in re.split(r"[^\w]+", s) if len(w) >= MIN_IDENT_LEN]


# 到处都有的通用方法名/字段名（get、add、run…）：引用多半指向标准库或别的类型，降权
_GENERIC = {"get", "set", "add", "put", "pop", "run", "start", "stop", "close", "open", "read", "write", "load",
            "save", "send", "emit", "call", "init", "update", "append", "remove", "delete", "clear", "copy", "keys",
            "values", "items", "text", "json", "data", "name", "path", "key", "value", "info", "message", "format",
            "join", "split", "strip", "replace", "next", "reset", "size", "length", "list", "map", "filter", "main",
            "test", "setup", "handle", "parse", "render", "build", "create", "error", "log", "debug", "warn", "cwd",
            "id", "type", "kind", "args", "kwargs", "options", "config", "result", "status", "toString", "String"}


def _ident_mul(ident: str, n_definers: int, focus: set[str]) -> float:
    mul = 1.0 / max(1, n_definers)          # 多处同名定义：一个引用不知道指向谁，平分
    is_snake = "_" in ident.strip("_") and any(c.isalpha() for c in ident)
    is_camel = any(c.isupper() for c in ident[1:]) and any(c.islower() for c in ident)
    if ident in focus:
        mul *= 10.0
    if len(ident) >= 8 and (is_snake or is_camel):
        mul *= 10.0
    if ident.startswith("_"):
        mul *= 0.1
    if n_definers > 5:
        mul *= 0.1
    if ident in _GENERIC or len(ident) <= 3:
        mul *= 0.1
    return mul


def _pagerank(nodes: list[str], edges: dict, personal: set[str], damping: float = 0.85,
              iters: int = 40, tol: float = 1e-8) -> dict[str, float]:
    N = len(nodes)
    idx = {n: i for i, n in enumerate(nodes)}
    pers = [0.0] * N
    ps = [n for n in personal if n in idx]
    if ps:
        for n in ps:
            pers[idx[n]] = 1.0 / len(ps)
    else:
        pers = [1.0 / N] * N
    out_w: list[dict[int, float]] = [dict() for _ in range(N)]
    for src, outs in edges.items():
        i = idx.get(src)
        if i is None:
            continue
        for (dst, _), w in outs.items():
            j = idx.get(dst)
            if j is not None and w > 0:
                out_w[i][j] = out_w[i].get(j, 0.0) + w
    tot = [sum(o.values()) for o in out_w]
    rank = [1.0 / N] * N
    for _ in range(iters):
        new = [0.0] * N
        dangling = 0.0
        for i in range(N):
            if tot[i] <= 0:
                dangling += rank[i]
                continue
            ri = rank[i] / tot[i]
            for j, w in out_w[i].items():
                new[j] += ri * w
        for j in range(N):
            new[j] = damping * (new[j] + dangling * pers[j]) + (1 - damping) * pers[j]
        diff = sum(abs(a - b) for a, b in zip(new, rank))
        rank = new
        if diff < tol:
            break
    return {n: rank[i] for n, i in idx.items()}


def _inside(p: Path, root: Path) -> bool:
    try:
        Path(os.path.realpath(p)).relative_to(root)
        return True
    except ValueError:
        return False


def _looks_minified(src: bytes) -> bool:
    if len(src) < 20000:
        return False
    nl = src.count(b"\n") + 1
    return len(src) / nl > 300


def _tokens(text: str) -> int:
    from ee.core.compaction import estimate_tokens
    return estimate_tokens(text)


def _render_lines(items: list[tuple]) -> str:
    """按文件分组：文件按其最高分定义的先后排，文件内按行号；每行“行号│签名行”。"""
    if not items:
        return ""
    order: list[str] = []
    by_file: dict[str, list] = defaultdict(list)
    for _, r, d in items:
        if r not in by_file:
            order.append(r)
        by_file[r].append(d)
    out: list[str] = []
    for r in order:
        out.append(f"{r}:")
        for d in sorted(by_file[r], key=lambda d: d[2]):
            out.append(f"{d[2]:>5}│{d[3]}")
    return "\n".join(out) + "\n"


def _fit(ranked: list[tuple], max_tokens: int) -> int:
    """二分：预算内最多能放前多少条定义。"""
    if not ranked or max_tokens <= 0:
        return 0
    lo, hi, best = 1, len(ranked), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if _tokens(_render_lines(ranked[:mid])) <= max_tokens:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    return best


# ------------------------------------------------------------------ 对外入口

_instances: dict[tuple[str, str], RepoMap] = {}
_inst_lock = threading.Lock()


def for_root(root: Path, home: Path) -> RepoMap:
    key = (str(Path(root).resolve()), str(Path(home)))
    with _inst_lock:
        rm = _instances.get(key)
        if rm is None:
            rm = _instances[key] = RepoMap(Path(root), Path(home))
        return rm


def repo_config(data: dict | None) -> dict:
    return {**DEFAULTS, **(((data or {}).get("repo_map")) or {})}


def map_root(cwd: Path) -> Path | None:
    """地图的根：git 项目根，否则 cwd；用户主目录或盘根返回 None（太大，不做）。"""
    from ee.core.instructions import project_root
    cwd = Path(cwd).resolve()
    root = project_root(cwd) or cwd
    if root == Path.home().resolve() or root == Path(root.anchor):
        return None
    return root


def build_map(cwd: Path, home: Path, max_tokens: int, focus: list[str] | None = None, subdir: str = "",
              cfg: dict | None = None) -> tuple[str, dict]:
    """生成地图文本；依赖不可用时返回 ("", {"unavailable": 原因})。"""
    cfg = cfg or DEFAULTS
    if not available():
        return "", {"unavailable": unavailable_reason() or "tree-sitter 不可用"}
    root = map_root(cwd)
    if root is None:
        return "", {"unavailable": "工作目录是用户主目录或磁盘根目录，范围太大，不生成仓库地图"}
    rm = for_root(root, home)
    # 地图根是 git 根，可能在工作目录之上；list_files 只加载根及以上的 .eeignore，工作目录里的那份要另外过滤
    ignore = None
    if Path(cwd).resolve() != root:
        try:
            from ee.core.eeignore import for_dir
            ig = for_dir(Path(cwd))
            ignore = None if ig.empty else ig
        except Exception:  # noqa: BLE001
            ignore = None
    text, info = rm.render(max_tokens, focus=focus, subdir=subdir, max_files=int(cfg.get("max_files", 3000)),
                           time_limit=float(cfg.get("time_limit", 20.0)), ignore=ignore)
    info["root"] = str(root)
    return text, info


INJECT_HEAD = ("<repo-map>\n以下是会话开始时生成的仓库地图：按引用关系排名的关键定义（行号│签名行），"
               "只是索引，代码可能已变；要细节请 read 对应文件，要换个范围或关注点请调用 repo_map。\n")


def startup_message(cwd: Path, home: Path, data: dict | None) -> dict | None:
    """会话开始时自动注入的一小份地图（配置 repo_map.auto_inject，默认关）。和 AGENTS.md 一样作为固定 user 消息，
    会话内不再改动（保住前缀缓存）。生成失败、超时没解析完或地图为空时不注入。"""
    cfg = repo_config(data)
    if not cfg.get("enabled", True) or not cfg.get("auto_inject", False):
        return None
    try:
        lim = {**cfg, "time_limit": min(float(cfg.get("time_limit", 20.0)), 5.0)}
        text, info = build_map(cwd, home, int(cfg.get("inject_tokens", 1024)), cfg=lim)
    except Exception as e:  # noqa: BLE001
        log.info("仓库地图自动注入失败：%s", e)
        return None
    if not text or info.get("timed_out"):
        return None
    return {"role": "user", "content": INJECT_HEAD + text + "</repo-map>", "_kind": "agents", "_sub": "repomap"}


def selftest() -> dict:
    """打包自检：真解析一小段 Python 和 TSX，确认语言库被收进安装包。"""
    py = extract("python", b"class Foo:\n    def bar(self):\n        return baz()\n\ndef baz():\n    return Foo()\n")
    tsx = extract("tsx", b"export interface P { a: number }\nexport function App(p: P) { return <div/>; }\n"
                         b"class Store { get(k: string) { return k; } }\n")
    missing = [lang for lang in LANGUAGES if _ts.get(lang) is None]
    ok = (py is not None and {d[0] for d in py[0]} >= {"Foo", "bar", "baz"}
          and tsx is not None and {d[0] for d in tsx[0]} >= {"P", "App", "Store", "get"} and not missing)
    return {"ok": bool(ok), "pack": _pack_version(), "missing_langs": missing,
            "python": [d[0] for d in py[0]] if py else None, "tsx": [d[0] for d in tsx[0]] if tsx else None,
            "error": unavailable_reason() or None}
