"""命令沙箱（v1，仅 Windows 真正隔离写入）。

做法：
- 每个工作区一个随机“能力 SID”（持久化在 ~/.ee/sandbox/sandbox.json），只有 ee 的沙箱令牌持有它。
- 给工作区和 ~/.ee/sandbox/tmp/<id> 加一条“能力 SID 可修改”的可继承 ACE；TEMP/TMP 指向后者。
- 命令经由一个小的启动器进程（`ee __sandbox_exec`）运行：启动器用
  CreateRestrictedToken(DISABLE_MAX_PRIVILEGE | LUA_TOKEN | WRITE_RESTRICTED) 造受限令牌，
  限制 SID = 能力 SID + 登录 SID + Everyone，再 CreateProcessAsUserW 挂起创建命令进程，放进
  KILL_ON_JOB_CLOSE 的 Job Object 后恢复运行，等它结束并原样返回退出码。
  “写受限”令牌做写访问检查时，除正常检查外还要求某个限制 SID 被授予写权限，所以只有工作区、
  沙箱临时目录以及本来就对 Everyone/登录会话开放写的位置可写；读不受影响。
- 标准输入输出句柄由启动器原样继承给命令进程，调用方的管道/异步读取逻辑不变；
  启动器被杀（taskkill /T 或进程退出）→ Job 句柄关闭 → 命令及其所有子孙进程一起结束。

ACE 不撤销（有意为之）：能力 SID 随机、只属于这个工作区、只有沙箱令牌持有，留着它不会给任何其他
主体多开权限；而每条命令前后加/撤 ACE 要在整个目录树上传播继承（大仓库很慢），并且同一工作区并发的
多条命令会互相撤掉对方的权限。需要清理时调用 revoke(工作区)。

禁网（network=false）v1 只是环境变量拦截：代理指向 127.0.0.1:9、pip/npm/cargo 离线、git ssh 直接失败。
这是尽力而为——不读这些变量、直接建 socket 的程序照样能联网。
非 Windows：文件系统隔离不支持、未生效（调用方会明确提示），只有禁网环境变量会设置。
"""
from __future__ import annotations

import json
import os
import secrets as _rand
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

HELPER_CMD = "__sandbox_exec"
FAIL_CODE = 125
FAIL_MARK = "[沙箱启动失败]"
NOTICE_UNSUPPORTED = "[沙箱：当前系统不支持文件写入隔离，未生效（仅 Windows 支持）]"
DEAD_PROXY = "http://127.0.0.1:9"

# 永远不交给沙箱里命令的变量（后端连接口令等）
SECRET_ENV_KEYS = ("EE_SERVE_TOKEN",)


class SandboxError(RuntimeError):
    pass


def settings(data: dict | None) -> dict:
    s = (data or {}).get("sandbox") or {}
    if not isinstance(s, dict):
        s = {}
    return {"enabled": bool(s.get("enabled", False)), "network": bool(s.get("network", True))}


def supported() -> bool:
    return IS_WINDOWS


# ------------------------------------------------------------------ 环境变量

def _set(env: dict, key: str, value: str) -> None:
    if IS_WINDOWS:              # Windows 环境变量不分大小写：先删掉同名的其他写法
        for k in [k for k in env if k.upper() == key.upper()]:
            env.pop(k, None)
        env[key] = value
    else:
        env[key] = value


def network_env(env: dict) -> dict:
    """禁网（尽力而为）：返回加了拦截变量的新字典。本机地址不走死代理，方便访问本地 dev server。"""
    env = dict(env)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        _set(env, k, DEAD_PROXY)
        if not IS_WINDOWS:
            env[k.lower()] = DEAD_PROXY
    _set(env, "NO_PROXY", "localhost,127.0.0.1,::1")
    if not IS_WINDOWS:
        env["no_proxy"] = "localhost,127.0.0.1,::1"
    for k, v in (("PIP_NO_INDEX", "1"), ("NPM_CONFIG_OFFLINE", "true"), ("CARGO_NET_OFFLINE", "true"),
                 ("UV_OFFLINE", "1"), ("GIT_SSH_COMMAND", "false"), ("EE_SANDBOX_NO_NETWORK", "1")):
        _set(env, k, v)
    return env


def strip_secrets(env: dict) -> dict:
    up = {k.upper() for k in SECRET_ENV_KEYS}
    return {k: v for k, v in env.items() if k.upper() not in up}


# ------------------------------------------------------------------ 持久化

def sandbox_home() -> Path:
    from ee.config import ee_home
    return ee_home() / "sandbox"


def workspace_id(ws: Path) -> str:
    p = os.path.normcase(os.path.abspath(str(ws)))
    return sha256(p.encode("utf-8", "surrogatepass")).hexdigest()[:16]


_lock = threading.Lock()


def _state_path() -> Path:
    return sandbox_home() / "sandbox.json"


def _load_state() -> dict:
    try:
        d = json.loads(_state_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(d: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _random_sid() -> str:
    return "S-1-5-21-" + "-".join(str(_rand.randbelow(2**32 - 1) + 1) for _ in range(4))


def capability_sid(ws: Path) -> str:
    """工作区的能力 SID（没有就生成并保存）。"""
    wid = workspace_id(ws)
    with _lock:
        d = _load_state()
        wss = d.setdefault("workspaces", {})
        ent = wss.get(wid)
        if isinstance(ent, dict) and str(ent.get("sid", "")).startswith("S-1-"):
            return ent["sid"]
        sid = _random_sid()
        wss[wid] = {"sid": sid, "path": str(ws)}
        _save_state(d)
        return sid


def tmp_dir(ws: Path) -> Path:
    return sandbox_home() / "tmp" / workspace_id(ws)


# ------------------------------------------------------------------ 计划

@dataclass
class SandboxPlan:
    workspace: Path
    active: bool                 # 文件写入隔离是否生效（仅 Windows）
    network: bool                # True = 允许联网（不拦截）
    sid: str = ""
    tmp: Path | None = None
    notice: str = ""             # 要附在命令结果后面的说明（例如“不支持，未生效”）
    extra: dict = field(default_factory=dict)

    def env(self, env: dict) -> dict:
        env = strip_secrets(env)
        if not self.network:
            env = network_env(env)
        if self.active and self.tmp is not None:
            _set(env, "TEMP", str(self.tmp))
            _set(env, "TMP", str(self.tmp))
            _set(env, "EE_SANDBOX", "1")
        return env

    def wrap(self, argv: list[str], env: dict) -> tuple[list[str], dict]:
        env = self.env(env)
        if not self.active:
            return list(argv), env
        return helper_argv() + ["--sid", self.sid, "--"] + list(argv), env


def helper_argv() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, HELPER_CMD]
    return [sys.executable, "-m", "ee", HELPER_CMD]


_granted: set[tuple[str, str]] = set()      # 本进程已确认授权过的 (路径, SID)


def plan(data: dict | None, workspace: Path) -> SandboxPlan | None:
    """按配置得到沙箱计划；未开启返回 None。Windows 上准备失败抛 SandboxError（不静默回退）。"""
    s = settings(data)
    if not s["enabled"]:
        return None
    ws = Path(workspace).resolve()
    if not IS_WINDOWS:
        return SandboxPlan(workspace=ws, active=False, network=s["network"], notice=NOTICE_UNSUPPORTED)
    try:
        sid = capability_sid(ws)
        tmp = tmp_dir(ws)
        tmp.mkdir(parents=True, exist_ok=True)
        for p in (ws, tmp):
            key = (os.path.normcase(str(p)), sid)
            if key in _granted:
                continue
            _win().grant(str(p), sid)
            _granted.add(key)
    except SandboxError:
        raise
    except Exception as e:  # noqa: BLE001
        raise SandboxError(f"{FAIL_MARK} 准备沙箱失败：{e}") from e
    return SandboxPlan(workspace=ws, active=True, network=s["network"], sid=sid, tmp=tmp)


def revoke(workspace: Path) -> bool:
    """撤掉工作区及其临时目录上的能力 SID ACE（设置页“清理沙箱权限”之类用）。"""
    if not IS_WINDOWS:
        return False
    ws = Path(workspace).resolve()
    ent = (_load_state().get("workspaces") or {}).get(workspace_id(ws))
    if not isinstance(ent, dict):
        return False
    sid = ent.get("sid", "")
    for p in (ws, tmp_dir(ws)):
        if p.exists():
            _win().revoke(str(p), sid)
        _granted.discard((os.path.normcase(str(p)), sid))
    return True


def is_helper_failure(code, output: str) -> bool:
    return code == FAIL_CODE and FAIL_MARK in (output or "")


# ------------------------------------------------------------------ 启动器（在子进程里跑）

def helper_main(args: list[str]) -> int:
    """`ee __sandbox_exec --sid S-1-... -- 程序 参数...`：受限令牌 + Job 跑命令，返回它的退出码。"""
    try:
        sep = args.index("--")
        opts, cmd = args[:sep], args[sep + 1:]
        sid = opts[opts.index("--sid") + 1]
        if not cmd:
            raise ValueError("没有要运行的命令")
    except (ValueError, IndexError) as e:
        _fail(f"参数错误：{e}")
        return FAIL_CODE
    if not IS_WINDOWS:
        _fail("当前系统不支持沙箱")
        return FAIL_CODE
    try:
        return _win().run_restricted(subprocess.list2cmdline(cmd), sid)
    except Exception as e:  # noqa: BLE001
        _fail(str(e))
        return FAIL_CODE


def _fail(msg: str) -> None:
    try:
        sys.stderr.write(f"{FAIL_MARK} {msg}\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


_win_api = None


def _win():
    global _win_api
    if _win_api is None:
        _win_api = _WinApi()
    return _win_api


# ------------------------------------------------------------------ Windows API（ctypes）

class _WinApi:
    """只在 Windows 上实例化。"""

    TOKEN_ALL_ACCESS = 0xF01FF
    DISABLE_MAX_PRIVILEGE = 0x1
    LUA_TOKEN = 0x4
    WRITE_RESTRICTED = 0x8
    SE_GROUP_LOGON_ID = 0xC0000000
    TokenUser, TokenGroups, TokenDefaultDacl = 1, 2, 6
    SE_FILE_OBJECT = 1
    DACL_SECURITY_INFORMATION = 0x4
    GRANT_ACCESS, REVOKE_ACCESS = 1, 4
    SUB_CONTAINERS_AND_OBJECTS_INHERIT = 0x3
    NO_INHERITANCE = 0
    TRUSTEE_IS_SID = 0
    TRUSTEE_IS_UNKNOWN = 0
    FILE_MODIFY = 0x1301BF          # 读写执行删除（对应 icacls 的 M）
    GENERIC_ALL = 0x10000000
    CREATE_SUSPENDED = 0x4
    CREATE_UNICODE_ENVIRONMENT = 0x400
    CREATE_NO_WINDOW = 0x08000000
    STARTF_USESTDHANDLES = 0x100
    HANDLE_FLAG_INHERIT = 0x1
    INFINITE = 0xFFFFFFFF
    JobObjectExtendedLimitInformation = 9
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes as W
        self.ct, self.W = ctypes, W
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.adv = ctypes.WinDLL("advapi32", use_last_error=True)
        H, DW, BOOL, LP = W.HANDLE, W.DWORD, W.BOOL, ctypes.c_void_p

        class TRUSTEE_W(ctypes.Structure):
            _fields_ = [("pMultipleTrustee", LP), ("MultipleTrusteeOperation", ctypes.c_int),
                        ("TrusteeForm", ctypes.c_int), ("TrusteeType", ctypes.c_int), ("ptstrName", LP)]

        class EXPLICIT_ACCESS_W(ctypes.Structure):
            _fields_ = [("grfAccessPermissions", DW), ("grfAccessMode", ctypes.c_int),
                        ("grfInheritance", DW), ("Trustee", TRUSTEE_W)]

        class SID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Sid", LP), ("Attributes", DW)]

        class STARTUPINFOW(ctypes.Structure):
            _fields_ = [("cb", DW), ("lpReserved", W.LPWSTR), ("lpDesktop", W.LPWSTR), ("lpTitle", W.LPWSTR),
                        ("dwX", DW), ("dwY", DW), ("dwXSize", DW), ("dwYSize", DW), ("dwXCountChars", DW),
                        ("dwYCountChars", DW), ("dwFillAttribute", DW), ("dwFlags", DW), ("wShowWindow", W.WORD),
                        ("cbReserved2", W.WORD), ("lpReserved2", LP), ("hStdInput", H), ("hStdOutput", H),
                        ("hStdError", H)]

        class PROCESS_INFORMATION(ctypes.Structure):
            _fields_ = [("hProcess", H), ("hThread", H), ("dwProcessId", DW), ("dwThreadId", DW)]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount",
                        "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", DW), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", DW),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", DW), ("SchedulingClass", DW)]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self.TRUSTEE_W, self.EXPLICIT_ACCESS_W = TRUSTEE_W, EXPLICIT_ACCESS_W
        self.SID_AND_ATTRIBUTES, self.STARTUPINFOW, self.PROCESS_INFORMATION = (
            SID_AND_ATTRIBUTES, STARTUPINFOW, PROCESS_INFORMATION)
        self.JOBOBJECT_EXTENDED_LIMIT_INFORMATION = JOBOBJECT_EXTENDED_LIMIT_INFORMATION

        a, k = self.adv, self.k32
        a.ConvertStringSidToSidW.argtypes = [W.LPCWSTR, ctypes.POINTER(LP)]
        a.ConvertStringSidToSidW.restype = BOOL
        a.ConvertSidToStringSidW.argtypes = [LP, ctypes.POINTER(W.LPWSTR)]
        a.ConvertSidToStringSidW.restype = BOOL
        a.GetNamedSecurityInfoW.argtypes = [W.LPCWSTR, ctypes.c_int, DW, LP, LP, ctypes.POINTER(LP), LP,
                                            ctypes.POINTER(LP)]
        a.GetNamedSecurityInfoW.restype = DW
        a.SetNamedSecurityInfoW.argtypes = [W.LPWSTR, ctypes.c_int, DW, LP, LP, LP, LP]
        a.SetNamedSecurityInfoW.restype = DW
        a.SetEntriesInAclW.argtypes = [ctypes.c_ulong, ctypes.POINTER(EXPLICIT_ACCESS_W), LP, ctypes.POINTER(LP)]
        a.SetEntriesInAclW.restype = DW
        a.GetExplicitEntriesFromAclW.argtypes = [LP, ctypes.POINTER(ctypes.c_ulong),
                                                 ctypes.POINTER(ctypes.POINTER(EXPLICIT_ACCESS_W))]
        a.GetExplicitEntriesFromAclW.restype = DW
        a.EqualSid.argtypes = [LP, LP]
        a.EqualSid.restype = BOOL
        a.OpenProcessToken.argtypes = [H, DW, ctypes.POINTER(H)]
        a.OpenProcessToken.restype = BOOL
        a.GetTokenInformation.argtypes = [H, ctypes.c_int, LP, DW, ctypes.POINTER(DW)]
        a.GetTokenInformation.restype = BOOL
        a.SetTokenInformation.argtypes = [H, ctypes.c_int, LP, DW]
        a.SetTokenInformation.restype = BOOL
        a.CreateRestrictedToken.argtypes = [H, DW, DW, LP, DW, LP, DW, ctypes.POINTER(SID_AND_ATTRIBUTES),
                                            ctypes.POINTER(H)]
        a.CreateRestrictedToken.restype = BOOL
        a.CreateProcessAsUserW.argtypes = [H, W.LPCWSTR, W.LPWSTR, LP, LP, BOOL, DW, LP, W.LPCWSTR,
                                           ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION)]
        a.CreateProcessAsUserW.restype = BOOL
        k.LocalFree.argtypes = [LP]
        k.LocalFree.restype = LP
        k.GetCurrentProcess.restype = H
        k.CloseHandle.argtypes = [H]
        k.GetStdHandle.argtypes = [DW]
        k.GetStdHandle.restype = H
        k.SetHandleInformation.argtypes = [H, DW, DW]
        k.SetHandleInformation.restype = BOOL
        k.CreateJobObjectW.argtypes = [LP, W.LPCWSTR]
        k.CreateJobObjectW.restype = H
        k.SetInformationJobObject.argtypes = [H, ctypes.c_int, LP, DW]
        k.SetInformationJobObject.restype = BOOL
        k.AssignProcessToJobObject.argtypes = [H, H]
        k.AssignProcessToJobObject.restype = BOOL
        k.ResumeThread.argtypes = [H]
        k.ResumeThread.restype = DW
        k.TerminateProcess.argtypes = [H, ctypes.c_uint]
        k.WaitForSingleObject.argtypes = [H, DW]
        k.WaitForSingleObject.restype = DW
        k.GetExitCodeProcess.argtypes = [H, ctypes.POINTER(DW)]
        k.GetExitCodeProcess.restype = BOOL

    # ---- 小工具
    def _err(self, what: str, code: int | None = None) -> SandboxError:
        code = self.ct.get_last_error() if code is None else code
        try:
            msg = self.ct.FormatError(code).strip()
        except Exception:  # noqa: BLE001
            msg = ""
        return SandboxError(f"{what}失败（错误码 {code}{'：' + msg if msg else ''}）")

    def _sid(self, s: str):
        p = self.ct.c_void_p()
        if not self.adv.ConvertStringSidToSidW(s, self.ct.byref(p)):
            raise self._err(f"解析 SID {s} ")
        return p                                  # 用完 LocalFree

    def _ea(self, sid_ptr, access: int, mode: int, inherit: int):
        ea = self.EXPLICIT_ACCESS_W()
        ea.grfAccessPermissions = access
        ea.grfAccessMode = mode
        ea.grfInheritance = inherit
        ea.Trustee.TrusteeForm = self.TRUSTEE_IS_SID
        ea.Trustee.TrusteeType = self.TRUSTEE_IS_UNKNOWN
        ea.Trustee.ptstrName = sid_ptr.value if hasattr(sid_ptr, "value") else sid_ptr
        return ea

    # ---- 目录 ACL
    def _has_ace(self, dacl, sid_ptr) -> bool:
        ct = self.ct
        n = ct.c_ulong()
        arr = ct.POINTER(self.EXPLICIT_ACCESS_W)()
        if self.adv.GetExplicitEntriesFromAclW(dacl, ct.byref(n), ct.byref(arr)) != 0:
            return False
        try:
            for i in range(n.value):
                e = arr[i]
                if (e.grfAccessMode == self.GRANT_ACCESS and e.Trustee.TrusteeForm == self.TRUSTEE_IS_SID
                        and e.Trustee.ptstrName and self.adv.EqualSid(e.Trustee.ptstrName, sid_ptr)
                        and (e.grfAccessPermissions & self.FILE_MODIFY) == self.FILE_MODIFY
                        and e.grfInheritance & self.SUB_CONTAINERS_AND_OBJECTS_INHERIT):
                    return True
            return False
        finally:
            if arr:
                self.k32.LocalFree(ct.cast(arr, ct.c_void_p))

    def _set_ace(self, path: str, sid: str, mode: int) -> None:
        ct = self.ct
        sid_ptr = self._sid(sid)
        sd = ct.c_void_p()
        dacl = ct.c_void_p()
        new = ct.c_void_p()
        try:
            rc = self.adv.GetNamedSecurityInfoW(path, self.SE_FILE_OBJECT, self.DACL_SECURITY_INFORMATION,
                                                None, None, ct.byref(dacl), None, ct.byref(sd))
            if rc != 0:
                raise self._err(f"读取 {path} 的权限", rc)
            if mode == self.GRANT_ACCESS and self._has_ace(dacl, sid_ptr):
                return
            ea = self._ea(sid_ptr, self.FILE_MODIFY, mode, self.SUB_CONTAINERS_AND_OBJECTS_INHERIT)
            rc = self.adv.SetEntriesInAclW(1, ct.byref(ea), dacl, ct.byref(new))
            if rc != 0:
                raise self._err("构造访问控制列表", rc)
            rc = self.adv.SetNamedSecurityInfoW(path, self.SE_FILE_OBJECT, self.DACL_SECURITY_INFORMATION,
                                                None, None, new, None)
            if rc != 0:
                raise self._err(f"给 {path} 添加沙箱写权限", rc)
        finally:
            for p in (new, sd, sid_ptr):
                if p:
                    self.k32.LocalFree(p)

    def grant(self, path: str, sid: str) -> None:
        self._set_ace(path, sid, self.GRANT_ACCESS)

    def revoke(self, path: str, sid: str) -> None:
        self._set_ace(path, sid, self.REVOKE_ACCESS)

    # ---- 令牌
    def _token_info(self, h, cls: int):
        ct = self.ct
        n = self.W.DWORD()
        self.adv.GetTokenInformation(h, cls, None, 0, ct.byref(n))
        if not n.value:
            raise self._err("读取令牌信息")
        buf = ct.create_string_buffer(n.value)
        if not self.adv.GetTokenInformation(h, cls, buf, n, ct.byref(n)):
            raise self._err("读取令牌信息")
        return buf

    def _restricted_token(self, cap_sid: str):
        ct, W = self.ct, self.W
        tok = W.HANDLE()
        if not self.adv.OpenProcessToken(self.k32.GetCurrentProcess(), self.TOKEN_ALL_ACCESS, ct.byref(tok)):
            raise self._err("打开进程令牌")
        frees = []
        try:
            # 登录 SID（访问窗口站/桌面需要）与用户 SID（默认 DACL 用）
            gbuf = self._token_info(tok, self.TokenGroups)
            count = ct.cast(gbuf, ct.POINTER(W.DWORD))[0]
            off = ct.sizeof(ct.c_void_p)              # GroupCount 之后按指针对齐
            groups = (self.SID_AND_ATTRIBUTES * count).from_buffer(gbuf, off)
            logon = None
            for g in groups:
                if (g.Attributes & self.SE_GROUP_LOGON_ID) == self.SE_GROUP_LOGON_ID:
                    logon = g.Sid
                    break
            ubuf = self._token_info(tok, self.TokenUser)
            user_sid = self.SID_AND_ATTRIBUTES.from_buffer(ubuf).Sid

            cap = self._sid(cap_sid)
            everyone = self._sid("S-1-1-0")
            system = self._sid("S-1-5-18")
            frees += [cap, everyone, system]
            sids = [cap.value, everyone.value] + ([logon] if logon else [])
            arr = (self.SID_AND_ATTRIBUTES * len(sids))()
            for i, s in enumerate(sids):
                arr[i].Sid = s
                arr[i].Attributes = 0
            new = W.HANDLE()
            flags = self.DISABLE_MAX_PRIVILEGE | self.LUA_TOKEN | self.WRITE_RESTRICTED
            if not self.adv.CreateRestrictedToken(tok, flags, 0, None, 0, None, len(sids), arr, ct.byref(new)):
                raise self._err("创建受限令牌")

            # 默认 DACL：命令进程自己新建的内核对象/文件（无继承时）要能被受限令牌再次写打开
            owners = [cap.value, user_sid, system.value] + ([logon] if logon else [])
            eas = (self.EXPLICIT_ACCESS_W * len(owners))()
            for i, s in enumerate(owners):
                eas[i] = self._ea(s, self.GENERIC_ALL, self.GRANT_ACCESS, self.NO_INHERITANCE)
            acl = ct.c_void_p()
            rc = self.adv.SetEntriesInAclW(len(owners), eas, None, ct.byref(acl))
            if rc != 0:
                self.k32.CloseHandle(new)
                raise self._err("构造默认访问控制列表", rc)
            frees.append(acl)
            dd = ct.c_void_p(acl.value)              # TOKEN_DEFAULT_DACL { PACL DefaultDacl; }
            if not self.adv.SetTokenInformation(new, self.TokenDefaultDacl, ct.byref(dd), ct.sizeof(dd)):
                self.k32.CloseHandle(new)
                raise self._err("设置受限令牌的默认权限")
            return new
        finally:
            for p in frees:
                if p:
                    self.k32.LocalFree(p)
            self.k32.CloseHandle(tok)

    # ---- 启动
    def run_restricted(self, cmdline: str, cap_sid: str) -> int:
        ct, W = self.ct, self.W
        token = self._restricted_token(cap_sid)
        job = None
        pi = self.PROCESS_INFORMATION()
        try:
            job = self.k32.CreateJobObjectW(None, None)
            if not job:
                raise self._err("创建 Job Object")
            info = self.JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.k32.SetInformationJobObject(job, self.JobObjectExtendedLimitInformation,
                                                    ct.byref(info), ct.sizeof(info)):
                raise self._err("设置 Job Object")
            si = self.STARTUPINFOW()
            si.cb = ct.sizeof(si)
            si.dwFlags = self.STARTF_USESTDHANDLES
            for attr, n in (("hStdInput", -10), ("hStdOutput", -11), ("hStdError", -12)):
                h = self.k32.GetStdHandle(W.DWORD(n & 0xFFFFFFFF))
                if h and h != W.HANDLE(-1).value:
                    self.k32.SetHandleInformation(h, self.HANDLE_FLAG_INHERIT, self.HANDLE_FLAG_INHERIT)
                setattr(si, attr, h)
            buf = ct.create_unicode_buffer(cmdline)
            flags = self.CREATE_SUSPENDED | self.CREATE_UNICODE_ENVIRONMENT | self.CREATE_NO_WINDOW
            if not self.adv.CreateProcessAsUserW(token, None, buf, None, None, True, flags, None, None,
                                                 ct.byref(si), ct.byref(pi)):
                raise self._err("以受限令牌启动命令")
            if not self.k32.AssignProcessToJobObject(job, pi.hProcess):
                err = self._err("把命令放进 Job Object")
                self.k32.TerminateProcess(pi.hProcess, FAIL_CODE)
                raise err
            self.k32.ResumeThread(pi.hThread)
            self.k32.WaitForSingleObject(pi.hProcess, self.INFINITE)
            code = W.DWORD()
            self.k32.GetExitCodeProcess(pi.hProcess, ct.byref(code))
            return int(code.value)
        finally:
            for h in (pi.hThread, pi.hProcess, token):
                if h:
                    self.k32.CloseHandle(h)
            if job:
                self.k32.CloseHandle(job)       # KILL_ON_JOB_CLOSE：残留的子孙进程一起结束
