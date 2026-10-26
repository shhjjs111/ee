import tomllib

import pytest

import ee.tools.shellenv as SE
from ee.config import load_config, write_setting
from ee.core.context import env_parts
from ee.tools.shellenv import ShellInfo, ShellNotFound


def fake_which(avail: dict):
    return lambda name: avail.get(name)


def test_windows_auto_prefers_pwsh(monkeypatch):
    monkeypatch.setattr(SE, "IS_WINDOWS", True)
    monkeypatch.setattr(SE, "_which", fake_which({"pwsh": r"C:\Program Files\PowerShell\7\pwsh.exe",
                                                   "powershell": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"}))
    assert SE.resolve_shell("auto").kind == "pwsh"
    monkeypatch.setattr(SE, "_which", fake_which({"powershell": r"C:\Windows\powershell.exe"}))
    sh = SE.resolve_shell("auto")
    assert sh.kind == "powershell" and sh.syntax == "powershell"


def test_git_bash_search_and_error(monkeypatch, tmp_path):
    monkeypatch.setattr(SE, "IS_WINDOWS", True)
    gitdir = tmp_path / "Git"
    (gitdir / "bin").mkdir(parents=True)
    (gitdir / "cmd").mkdir()
    (gitdir / "bin" / "bash.exe").write_text("")
    (gitdir / "cmd" / "git.exe").write_text("")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "nope"))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(SE, "_which", fake_which({"git": str(gitdir / "cmd" / "git.exe")}))
    monkeypatch.setattr(SE, "Path", SE.Path)
    sh = SE.resolve_shell("git-bash")
    assert sh.kind == "git-bash" and sh.exe.endswith("bash.exe") and sh.syntax == "posix"
    # WSL 的 System32\bash.exe 不算
    monkeypatch.setattr(SE, "_which", fake_which({"bash": r"C:\Windows\System32\bash.exe"}))
    monkeypatch.setattr(SE, "find_git_bash", lambda: None)
    with pytest.raises(ShellNotFound, match="Git Bash"):
        SE.resolve_shell("git-bash")


def test_env_block_tells_shell():
    ps = ShellInfo("pwsh", "pwsh.exe", "PowerShell 7 (pwsh)")
    parts = env_parts(SE.Path("C:/proj"), ps, None, True, "默认")
    assert "PowerShell 7" in parts["shell"] and "$env:" in parts["shell"]
    assert "gh" in parts["tools"] and "不是 git 仓库" in parts["git"]


def test_write_setting_roundtrip(ee_home, tmp_path):
    p = write_setting("shell", "git-bash")
    write_setting("permissions.mode", "auto")
    write_setting("default_model", "deepseek-pro")
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    assert data == {"shell": "git-bash", "default_model": "deepseek-pro", "permissions": {"mode": "auto"}}
    cfg = load_config(tmp_path)
    assert cfg.data["shell"] == "git-bash" and cfg.default_model == "deepseek-pro"


def test_project_config_overrides(ee_home, tmp_path):
    (ee_home / "config.toml").write_text('default_model = "deepseek-pro"\n', encoding="utf-8")
    proj = tmp_path / "proj"
    (proj / ".ee").mkdir(parents=True)
    (proj / ".ee" / "config.toml").write_text(
        'default_model = "qwen"\n[providers.dashscope]\nbase_url = "https://x/v1"\napi_key_env = "DASHSCOPE_API_KEY"\n'
        '[models.qwen]\nprovider = "dashscope"\nmodel = "qwen3-coder-plus"\ncontext_window = 1000000\n', encoding="utf-8")
    cfg = load_config(proj)
    spec = cfg.resolve_model()
    assert spec.model == "qwen3-coder-plus" and spec.profile.family == "qwen" and spec.profile.context_window == 1000000
    assert cfg.resolve_model("deepseek/deepseek-v4-pro").model == "deepseek-v4-pro"
