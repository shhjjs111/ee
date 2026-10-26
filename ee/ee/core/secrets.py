"""API key 安全存储（D3-F）。

- 存储后端：
  - Windows：凭据管理器（ctypes 调 advapi32 CredWriteW/CredReadW/CredDeleteW，GENERIC 凭据，
    目标名 ee/<key>，不依赖 keyring 包）；不可用时回退 ~/.ee/credentials.json，值用 DPAPI 加密。
  - 其他平台：装了 keyring 就用 keyring，否则 ~/.ee/credentials.json（权限 0600，设置页提示不安全）。
  - 环境变量 EE_SECRETS_STORE=auto|wincred|keyring|file|memory 可强制选择（测试/排错用）。
- key 名：provider/<供应商名>、web/<tavily|brave|exa|deepseek>。
- config.toml 不再存明文：旧明文由迁移（core/migrate.py v3）挪进这里，并抹掉 .bak 备份里的明文。
- 读取优先级（config.ModelSpec.key_source / tools/web.py）：凭据存储 > 旧明文 > 环境变量。
- redact()：日志、会话记录、错误信息、RPC 返回统一脱敏（已知 key 值 + sk-/tvly-/Bearer 形态）。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
import threading
from pathlib import Path
from typing import Iterable

log = logging.getLogger("ee.secrets")

TARGET_PREFIX = "ee/"
WEB_KEY_NAMES = ("tavily", "brave", "exa", "deepseek")
MASK = "••••"


class SecretStoreError(Exception):
    """凭据存储读写失败。"""


def provider_key_name(name: str) -> str:
    return f"provider/{name}"


def web_key_name(name: str) -> str:
    return f"web/{name}"


# ---------------------------------------------------------------- 后端

class MemoryStore:
    name = "memory"
    label = "内存（仅本次运行）"
    secure = False

    def __init__(self) -> None:
        self._d: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._d.get(key)

    def set(self, key: str, value: str) -> None:
        self._d[key] = value

    def delete(self, key: str) -> None:
        self._d.pop(key, None)


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    """Windows DPAPI（当前用户范围）加/解密。"""
    if sys.platform != "win32":
        raise SecretStoreError("DPAPI 只在 Windows 上可用")
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    buf = ctypes.create_string_buffer(data, len(data))
    src = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    dst = BLOB()
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(BLOB), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(BLOB)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(dst)):   # UI_FORBIDDEN
        raise SecretStoreError(f"DPAPI {'加密' if encrypt else '解密'}失败（错误 {ctypes.get_last_error()}）")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree(ctypes.cast(dst.pbData, ctypes.c_void_p))


class WinCredStore:
    """Windows 凭据管理器（控制面板 → 凭据管理器 → Windows 凭据，条目名 ee/...）。"""
    name = "wincred"
    label = "Windows 凭据管理器"
    secure = True

    _CRED_TYPE_GENERIC = 1
    _CRED_PERSIST_LOCAL_MACHINE = 2
    _ERROR_NOT_FOUND = 1168

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise SecretStoreError("Windows 凭据管理器只在 Windows 上可用")
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        class CREDENTIALW(ctypes.Structure):
            _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                        ("LastWritten", FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                        ("CredentialBlob", ctypes.POINTER(ctypes.c_char)), ("Persist", wintypes.DWORD),
                        ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                        ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

        self._ct = ctypes
        self._CRED = CREDENTIALW
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        self._write = adv.CredWriteW
        self._write.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
        self._write.restype = wintypes.BOOL
        self._read = adv.CredReadW
        self._read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
        self._read.restype = wintypes.BOOL
        self._del = adv.CredDeleteW
        self._del.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._del.restype = wintypes.BOOL
        self._free = adv.CredFree
        self._free.argtypes = [ctypes.c_void_p]
        self._free.restype = None

    def get(self, key: str) -> str | None:
        ct = self._ct
        p = ct.POINTER(self._CRED)()
        if not self._read(TARGET_PREFIX + key, self._CRED_TYPE_GENERIC, 0, ct.byref(p)):
            err = ct.get_last_error()
            if err == self._ERROR_NOT_FOUND:
                return None
            raise SecretStoreError(f"读取凭据失败（错误 {err}）")
        try:
            c = p.contents
            raw = ct.string_at(c.CredentialBlob, c.CredentialBlobSize) if c.CredentialBlobSize else b""
            return raw.decode("utf-8")
        finally:
            self._free(ct.cast(p, ct.c_void_p))

    def set(self, key: str, value: str) -> None:
        ct = self._ct
        blob = value.encode("utf-8")
        buf = ct.create_string_buffer(blob, len(blob))
        c = self._CRED()
        c.Type = self._CRED_TYPE_GENERIC
        c.TargetName = TARGET_PREFIX + key
        c.Comment = "ee API key"
        c.CredentialBlobSize = len(blob)
        c.CredentialBlob = ct.cast(buf, ct.POINTER(ct.c_char))
        c.Persist = self._CRED_PERSIST_LOCAL_MACHINE
        c.UserName = "ee"
        if not self._write(ct.byref(c), 0):
            raise SecretStoreError(f"写入凭据失败（错误 {ct.get_last_error()}）")

    def delete(self, key: str) -> None:
        if not self._del(TARGET_PREFIX + key, self._CRED_TYPE_GENERIC, 0):
            err = self._ct.get_last_error()
            if err != self._ERROR_NOT_FOUND:
                raise SecretStoreError(f"删除凭据失败（错误 {err}）")


class KeyringStore:
    name = "keyring"
    label = "系统钥匙串"
    secure = True
    SERVICE = "ee"

    def __init__(self) -> None:
        try:
            import keyring  # type: ignore
            from keyring.backends import fail  # type: ignore
        except Exception as e:  # noqa: BLE001
            raise SecretStoreError(f"keyring 不可用：{type(e).__name__}") from e
        if isinstance(keyring.get_keyring(), fail.Keyring):
            raise SecretStoreError("keyring 没有可用后端")
        self._k = keyring

    def get(self, key: str) -> str | None:
        try:
            return self._k.get_password(self.SERVICE, key)
        except Exception as e:  # noqa: BLE001
            raise SecretStoreError(f"读取钥匙串失败：{type(e).__name__}") from e

    def set(self, key: str, value: str) -> None:
        try:
            self._k.set_password(self.SERVICE, key, value)
        except Exception as e:  # noqa: BLE001
            raise SecretStoreError(f"写入钥匙串失败：{type(e).__name__}") from e

    def delete(self, key: str) -> None:
        try:
            if self._k.get_password(self.SERVICE, key) is not None:
                self._k.delete_password(self.SERVICE, key)
        except Exception as e:  # noqa: BLE001
            raise SecretStoreError(f"删除钥匙串条目失败：{type(e).__name__}") from e


class FileStore:
    """~/.ee/credentials.json。dpapi=True（Windows）时值写成 "dpapi:<base64>"；否则明文 + 0600。"""
    name = "file"

    def __init__(self, path: Path | str, dpapi: bool | None = None) -> None:
        self.path = Path(path)
        self.dpapi = (sys.platform == "win32") if dpapi is None else bool(dpapi)
        self._lock = threading.Lock()

    @property
    def secure(self) -> bool:
        return self.dpapi

    @property
    def label(self) -> str:
        return ("本机加密文件（DPAPI）" if self.dpapi else "本机文件（未加密，仅限当前用户可读）") + f" {self.path}"

    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            raise SecretStoreError(f"读取 {self.path.name} 失败：{type(e).__name__}") from e

    def _save(self, d: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
            if sys.platform != "win32":
                os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as e:
            raise SecretStoreError(f"写入 {self.path.name} 失败：{e.strerror or type(e).__name__}") from e

    def get(self, key: str) -> str | None:
        with self._lock:
            v = self._load().get(key)
        if not isinstance(v, str):
            return None
        if v.startswith("dpapi:"):
            return _dpapi(base64.b64decode(v[6:]), False).decode("utf-8")
        return v[6:] if v.startswith("plain:") else v

    def set(self, key: str, value: str) -> None:
        enc = ("dpapi:" + base64.b64encode(_dpapi(value.encode("utf-8"), True)).decode("ascii")
               if self.dpapi else value)
        with self._lock:
            d = self._load()
            d[key] = enc
            self._save(d)

    def delete(self, key: str) -> None:
        with self._lock:
            d = self._load()
            if key in d:
                del d[key]
                self._save(d)


# ---------------------------------------------------------------- 当前后端

_store = None
_store_lock = threading.Lock()


def _credentials_path() -> Path:
    from ee.config import ee_home
    return ee_home() / "credentials.json"


def _make_default():
    mode = (os.environ.get("EE_SECRETS_STORE") or "auto").strip().lower()
    if mode == "memory":
        return MemoryStore()
    if mode == "file":
        return FileStore(_credentials_path())
    tries = []
    if mode in ("auto", "wincred") and sys.platform == "win32":
        tries.append(WinCredStore)
    if mode in ("auto", "keyring") and sys.platform != "win32":
        tries.append(KeyringStore)
    for cls in tries:
        try:
            return cls()
        except Exception as e:  # noqa: BLE001
            log.warning("凭据存储 %s 不可用，改用文件：%s", cls.name, e)
    return FileStore(_credentials_path())


def store():
    """当前凭据存储（第一次调用时按平台选择）。"""
    global _store
    with _store_lock:
        if _store is None:
            _store = _make_default()
        return _store


def use(st) -> None:
    """替换当前后端（测试用 MemoryStore）。"""
    global _store
    with _store_lock:
        _store = st


def info() -> dict:
    st = store()
    return {"backend": st.name, "label": st.label, "secure": bool(st.secure)}


def get(key: str) -> str | None:
    """读不到或存储出错都返回 None（出错记日志，不抛）。"""
    try:
        v = store().get(key)
    except Exception as e:  # noqa: BLE001
        log.warning("读取凭据 %s 失败：%s", key, e)
        return None
    if v:
        _remember(v)
        return v
    return None


def put(key: str, value: str) -> None:
    """写入；value 为空等于删除。失败抛 SecretStoreError。"""
    value = (value or "").strip()
    if not value:
        delete(key)
        return
    try:
        store().set(key, value)
    except SecretStoreError:
        raise
    except Exception as e:  # noqa: BLE001
        raise SecretStoreError(f"{type(e).__name__}: {e}") from e
    _remember(value)


set_secret = put
get_secret = get


def delete(key: str) -> None:
    try:
        store().delete(key)
    except Exception as e:  # noqa: BLE001
        log.warning("删除凭据 %s 失败：%s", key, e)


delete_secret = delete


# ---------------------------------------------------------------- 明文迁移

def plain_keys(data: dict) -> list[tuple[str, str, str]]:
    """config 数据里的明文 key：[(存储名, 表路径, 键名)]。"""
    out: list[tuple[str, str, str]] = []
    for name, p in ((data or {}).get("providers") or {}).items():
        if isinstance(p, dict) and str(p.get("api_key") or "").strip():
            out.append((provider_key_name(name), f"providers.{name}", "api_key"))
    w = (data or {}).get("web")
    if isinstance(w, dict):
        for name in WEB_KEY_NAMES:
            if str(w.get(f"{name}_api_key") or "").strip():
                out.append((web_key_name(name), "web", f"{name}_api_key"))
    return out


def _table(data: dict, dotted: str) -> dict:
    t = data
    for part in dotted.split("."):
        t = t[part]
    return t


def move_plain_keys(data: dict, overwrite: bool = False) -> tuple[int, list[tuple[str, str]]]:
    """把 data 里的明文 key 挪进凭据存储并从 data 删掉。

    overwrite=False（启动迁移）：存储里已有值就不覆盖（只删明文）；存不进去的保留明文、只记日志。
    overwrite=True（设置页保存）：新值覆盖存储；任何一个存不进去就抛 SecretStoreError。
    返回 (写入存储的个数, 已从 data 删除的 [(表路径, 键名)])。
    """
    n = 0
    moved: list[tuple[str, str]] = []
    for key, table, field in plain_keys(data):
        t = _table(data, table)
        val = str(t.get(field) or "").strip()
        try:
            if overwrite or not get(key):
                put(key, val)
                n += 1
        except Exception as e:  # noqa: BLE001
            if overwrite:
                raise SecretStoreError(str(e)) from e
            log.warning("把 %s 存进凭据存储失败，暂时保留明文：%s", key, e)
            continue
        t.pop(field, None)
        moved.append((table, field))
    return n, moved


_SECRET_LINE = re.compile(r"^\s*(?:api_key|[A-Za-z0-9]+_api_key)\s*=")
_TABLE_LINE = re.compile(r"^\s*\[\s*([^\[\]]+?)\s*\]\s*(?:#.*)?$")


def _norm_table(s: str) -> str:
    return ".".join(p.strip().strip('"').strip("'") for p in s.split("."))


def strip_secret_lines(text: str, moved: Iterable[tuple[str, str]] | None = None) -> str:
    """删掉 TOML 文本里的明文 key 行。moved=None 删所有 *api_key = 行；否则只删指定 (表, 键)。"""
    targets = None if moved is None else {(t, k) for t, k in moved}
    table = ""
    out = []
    for line in text.splitlines(keepends=True):
        m = _TABLE_LINE.match(line)
        if m:
            table = _norm_table(m.group(1))
            out.append(line)
            continue
        if _SECRET_LINE.match(line):
            field = line.split("=", 1)[0].strip()
            if targets is None or (table, field) in targets:
                continue
        out.append(line)
    return "".join(out)


def scrub_backups(config_path: Path) -> int:
    """抹掉 config.toml*.bak 里的明文 key，返回改动的文件数。"""
    n = 0
    try:
        baks = list(Path(config_path).parent.glob(Path(config_path).name + "*.bak"))
    except OSError:
        return 0
    for bak in baks:
        try:
            raw = bak.read_text(encoding="utf-8")
            clean = strip_secret_lines(raw)
            if clean != raw:
                bak.write_text(clean, encoding="utf-8")
                n += 1
        except (OSError, UnicodeDecodeError) as e:
            log.warning("清理备份 %s 失败：%s", bak.name, type(e).__name__)
    return n


# ---------------------------------------------------------------- 脱敏

_known: set[str] = set()
_known_lock = threading.Lock()
_PATTERNS = [
    (re.compile(r"\b(sk-)[A-Za-z0-9_\-]{12,}"), r"\1" + MASK),
    (re.compile(r"\b(tvly-)[A-Za-z0-9_\-]{8,}"), r"\1" + MASK),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-~+/=]{8,}", re.I), r"\1" + MASK),
    (re.compile(r"((?:api[_-]?key|exaApiKey)[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9._\-]{12,}", re.I), r"\1" + MASK),
]


def _remember(v: str) -> None:
    v = (v or "").strip()
    if len(v) >= 8:
        with _known_lock:
            _known.add(v)


def remember_env_keys(names: Iterable[str]) -> None:
    """环境变量里的 key 也纳入脱敏。"""
    for n in names:
        if n:
            _remember(os.environ.get(n, ""))


def redact(text):
    if not text or not isinstance(text, str):
        return text
    with _known_lock:
        known = sorted(_known, key=len, reverse=True)
    for v in known:
        if v in text:
            text = text.replace(v, MASK)
    for rx, rep in _PATTERNS:
        text = rx.sub(rep, text)
    return text


class RedactFilter(logging.Filter):
    """挂在日志 handler 上：消息和异常堆栈里的 key 一律打码。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            record.msg, record.args = redact(msg), None
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = redact(record.exc_text)
            if record.stack_info:
                record.stack_info = redact(record.stack_info)
        except Exception:  # noqa: BLE001
            pass
        return True
