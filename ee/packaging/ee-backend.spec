# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the ee desktop backend (onedir, name "ee-backend").
# Build from anywhere:  pyinstaller ee/packaging/ee-backend.spec --distpath <out> --workpath <tmp>
# Output: <distpath>/ee-backend/ee-backend[.exe]; Electron runs `ee-backend serve --exit-on-stdin-eof`.
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

HERE = os.path.dirname(os.path.abspath(SPEC))  # ee/packaging

datas = collect_data_files("ee")  # prompts/*.md, core/providers/profiles/*.toml
datas += collect_data_files("certifi")
datas += collect_data_files("docx")   # python-docx 的默认模板（Document() 需要）
datas += collect_data_files("pptx")

hiddenimports = []
# openai/httpx/pydantic are found by static analysis + their contrib hooks; only force the
# packages that import lazily (ee tools/providers, websockets' lazy top-level names).
# anyio 按名字动态 import 事件循环后端；httpx2/httpcore2 的传输层、truststore 也有按需导入。
for pkg in ("ee", "websockets", "anyio._backends", "httpx2", "httpcore2", "httpx", "httpcore", "h11",
            "truststore", "certifi", "openai._utils", "encodings",
            "pypdf", "docx", "openpyxl", "pptx", "lxml"):
    try:
        hiddenimports += collect_submodules(pkg)
    except Exception:
        pass
hiddenimports += ["tomllib", "tomli"]
# 仓库地图（D5-6）：语言包按名字动态 import 各语言的扩展模块（.pyd/.so），静态分析看不到；
# 包里有 170 多种语言（约 170MB），只收 ee.core.repomap.LANGUAGES 用到的这些（C# 在单独的 tree_sitter_c_sharp 包里）。
REPOMAP_LANGS = ["python", "javascript", "typescript", "tsx", "go", "rust", "java", "c", "cpp", "ruby", "php",
                 "kotlin", "lua"]
hiddenimports += ["tree_sitter", "tree_sitter_language_pack", "tree_sitter_c_sharp", "tree_sitter_yaml",
                  "tree_sitter_embedded_template"]
hiddenimports += [f"tree_sitter_language_pack.bindings.{l}" for l in REPOMAP_LANGS]

a = Analysis(
    [os.path.join(HERE, "entry.py")],
    pathex=[os.path.dirname(HERE)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "_pytest", "matplotlib", "numpy", "pandas", "IPython", "setuptools", "pkg_resources", "pip", "wheel", "pygments", "rich", "openai.cli", "openai.lib._parsing._completions_cli"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ee-backend",
    debug=False,
    strip=False,
    upx=False,
    console=True,  # stdout carries EE_SERVE_READY; Electron spawns it with windowsHide
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="ee-backend")
