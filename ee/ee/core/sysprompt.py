"""自定义系统提示词：SYSTEM.md 替换内置基础段，APPEND_SYSTEM.md 追加在后面。

来源（两种文件各自独立，二选一，项目级优先）：
  1. <项目>/.ee/SYSTEM.md        从工作目录往上找到 git 根为止，取最近的一个；**必须先在设置里信任**才生效
  2. ~/.ee/SYSTEM.md              用户级，直接生效
  3. 都没有 → 内置基础段（context.builtin_base）
  APPEND_SYSTEM.md 规则相同，内容追加在模型补丁之后。

最终系统提示词 = 基础段（SYSTEM.md 或内置）+ 模型补丁 system/<family>.md（可关）+ APPEND_SYSTEM.md + 回答风格段。

配置（~/.ee/config.toml）：
  [system_prompt]
  model_patch = true                    # 是否追加按模型家族的补丁（项目 .ee/config.toml 也可覆盖）
  allowed_project = ["路径sha8:内容sha8"] # 信任过的项目文件（只认用户级配置；内容一变要重新信任）

信任记录与项目 Hooks 用同一套“路径指纹 + 内容指纹”（hooks.file_allow_id），但单独存放：
信任了项目 Hooks 不等于信任项目的 SYSTEM.md，反之亦然。

系统提示词在会话首个请求时冻结（见 context.FrozenPrefix），改文件或信任状态从新对话开始生效。
系统提示词不是安全边界：权限模式、审批、deny 规则、危险命令确认都在代码里执行，提示词写什么都绕不过去。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ee.core.hooks import file_allow_id

KINDS = ("system", "append")
FILE_NAME = {"system": "SYSTEM.md", "append": "APPEND_SYSTEM.md"}
KIND_LABEL = {"system": "系统提示词（替换内置基础段）", "append": "追加系统提示词"}
MAX_BYTES = 64 * 1024
CFG_KEY = "system_prompt"


@dataclass
class PromptFile:
    kind: str                  # system | append
    scope: str                 # project | user
    path: str
    text: str = ""
    allow_id: str = ""         # 项目级才有
    trusted: bool = True       # 用户级恒为 True
    active: bool = False       # 本次实际生效
    error: str = ""            # 读取失败 / 太大 / 为空

    def summary(self) -> dict:
        return {"kind": self.kind, "scope": self.scope, "path": self.path, "allow_id": self.allow_id,
                "trusted": self.trusted, "active": self.active, "error": self.error, "chars": len(self.text)}


@dataclass
class SystemSources:
    files: list[PromptFile] = field(default_factory=list)
    model_patch: bool = True

    def active(self, kind: str) -> PromptFile | None:
        return next((f for f in self.files if f.kind == kind and f.active), None)

    @property
    def system(self) -> PromptFile | None:
        return self.active("system")

    @property
    def append(self) -> PromptFile | None:
        return self.active("append")

    @property
    def pending(self) -> list[PromptFile]:
        """项目里有、但还没信任（或内容改过）的文件。"""
        return [f for f in self.files if f.scope == "project" and not f.trusted and not f.error]

    @property
    def customized(self) -> bool:
        return self.system is not None or self.append is not None or not self.model_patch

    def notices(self) -> list[tuple[str, str]]:
        """会话开始时给界面的提示：(文字, 级别)。没有自定义文件时为空。"""
        out: list[tuple[str, str]] = []
        for f in self.pending:
            name = FILE_NAME[f.kind]
            out.append((f"项目里的 .ee/{name} 还没被信任（或内容改过），本对话没有使用它。"
                        f"确认内容可信后在 设置 → 系统提示词 里信任，新对话生效。", "warn"))
        for f in self.files:
            if f.error:
                out.append((f"{f.path}：{f.error}，本对话没有使用它。", "warn"))
        used = [f for f in (self.system, self.append) if f is not None]
        if used:
            out.append(("本对话使用了自定义系统提示词：" + "、".join(f.path for f in used), "info"))
        return out

    def summary(self) -> dict:
        return {"files": [f.summary() for f in self.files], "model_patch": self.model_patch,
                "system": self.system.path if self.system else "", "append": self.append.path if self.append else "",
                "pending": [f.allow_id for f in self.pending]}


def _read(path: Path) -> tuple[str, str]:
    """读文件：(内容, 错误)。去 BOM、统一换行、去首尾空白。"""
    try:
        raw = path.read_bytes()
    except OSError as e:
        return "", f"读取失败（{e}）"
    if len(raw) > MAX_BYTES:
        return "", f"文件太大（{len(raw) // 1024} KiB，上限 {MAX_BYTES // 1024} KiB）"
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return "", "不是 UTF-8 编码"
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return "", "文件是空的"
    return text, ""


def project_file(cwd: Path | None, name: str, home: Path) -> Path | None:
    """从 cwd 往上（到 git 根为止）最近的 .ee/<name>；用户级 ~/.ee 本身不算项目。"""
    if cwd is None:
        return None
    try:
        cur = Path(cwd).resolve()
        home_ee = Path(home).resolve()
    except OSError:
        return None
    for d in [cur, *cur.parents]:
        p = d / ".ee" / name
        try:
            if p.is_file() and p.resolve().parent != home_ee:
                return p
        except OSError:
            pass
        if (d / ".git").exists():
            break
    return None


def allowed_ids(user_data: dict | None = None) -> set[str]:
    """用户信任过的项目文件。只读用户级配置（绝不用合并了项目配置的数据，免得项目给自己授权）。"""
    if user_data is None:
        from ee.config import load_user_file
        try:
            user_data = load_user_file()
        except Exception:  # noqa: BLE001
            return set()
    sp = user_data.get(CFG_KEY) if isinstance(user_data.get(CFG_KEY), dict) else {}
    return set(str(x) for x in (sp.get("allowed_project") or []))


def model_patch_enabled(config_data: dict | None) -> bool:
    sp = (config_data or {}).get(CFG_KEY)
    return bool(sp.get("model_patch", True)) if isinstance(sp, dict) else True


def resolve(cwd: Path | None, home: Path, config_data: dict | None = None,
            user_data: dict | None = None) -> SystemSources:
    """找出本次生效的 SYSTEM.md / APPEND_SYSTEM.md。config_data：合并后的配置（只取 model_patch）。"""
    allowed = allowed_ids(user_data)
    src = SystemSources(model_patch=model_patch_enabled(config_data))
    for kind in KINDS:
        name = FILE_NAME[kind]
        chosen = False
        pp = project_file(cwd, name, home)
        if pp is not None:
            text, err = _read(pp)
            aid = file_allow_id(pp, text) if not err else ""
            f = PromptFile(kind, "project", str(pp), text, aid, trusted=bool(aid) and aid in allowed, error=err)
            if f.trusted:
                f.active = chosen = True
            src.files.append(f)
        up = Path(home) / name
        if up.is_file():
            text, err = _read(up)
            f = PromptFile(kind, "user", str(up), text, error=err)
            if not chosen and not err:
                f.active = True
            src.files.append(f)
    return src


# ------------------------------------------------------------------ 信任（写用户级配置）
def _edit_allowed(fn) -> list[str]:
    from ee.config import load_user_file, save_user_file
    data = load_user_file()
    sp = dict(data.get(CFG_KEY) or {}) if isinstance(data.get(CFG_KEY), dict) else {}
    ids = [str(x) for x in (sp.get("allowed_project") or [])]
    ids = list(dict.fromkeys(fn(ids)))
    sp["allowed_project"] = ids
    data[CFG_KEY] = sp
    save_user_file(data)
    return ids


def trust(cwd: Path | None, home: Path, allow_id: str) -> list[str]:
    """信任一个项目文件。allow_id 必须对得上当前文件内容（防止界面上看到的是旧内容）。"""
    src = resolve(cwd, home)
    f = next((x for x in src.files if x.scope == "project" and x.allow_id and x.allow_id == allow_id), None)
    if f is None:
        raise ValueError("项目里的系统提示词文件已经改动或不存在，请刷新后重新确认")
    return _edit_allowed(lambda ids: ids + [allow_id])


def untrust(allow_id: str) -> list[str]:
    return _edit_allowed(lambda ids: [x for x in ids if x != allow_id])


def set_model_patch(on: bool) -> bool:
    from ee.config import load_user_file, save_user_file
    data = load_user_file()
    sp = dict(data.get(CFG_KEY) or {}) if isinstance(data.get(CFG_KEY), dict) else {}
    sp["model_patch"] = bool(on)
    data[CFG_KEY] = sp
    save_user_file(data)
    return bool(on)
