"""D3-F：API key 安全存储、迁移、脱敏。"""
from __future__ import annotations

import json
import logging
import os
import sys
import uuid

import pytest

from ee.core import secrets


def test_file_store_roundtrip(tmp_path):
    fs = secrets.FileStore(tmp_path / "credentials.json", dpapi=False)
    assert fs.get("provider/x") is None
    fs.set("provider/x", "sk-file-1234567890")
    assert fs.get("provider/x") == "sk-file-1234567890"
    if sys.platform != "win32":
        assert (os.stat(tmp_path / "credentials.json").st_mode & 0o777) == 0o600
    fs.delete("provider/x")
    assert fs.get("provider/x") is None
    assert not fs.secure


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 专用")
def test_wincred_and_dpapi_real():
    st = secrets.WinCredStore()
    name = f"pytest/{uuid.uuid4().hex[:8]}"
    try:
        st.set(name, "sk-wincred-测试-123456")
        assert st.get(name) == "sk-wincred-测试-123456"
    finally:
        st.delete(name)
    assert st.get(name) is None
    st.delete(name)                                  # 不存在也不报错
    assert secrets._dpapi(secrets._dpapi(b"abc", True), False) == b"abc"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 专用")
def test_file_store_dpapi(tmp_path):
    fs = secrets.FileStore(tmp_path / "c.json", dpapi=True)
    fs.set("a", "sk-dpapi-1234567890")
    raw = (tmp_path / "c.json").read_text(encoding="utf-8")
    assert "sk-dpapi" not in raw and "dpapi:" in raw
    assert fs.get("a") == "sk-dpapi-1234567890"


def test_redact():
    secrets.put("provider/a", "my-weird-key-value-xyz")
    t = secrets.redact("got my-weird-key-value-xyz and sk-abcdefghijklmnop1234 and Bearer abcdefghijklmnopq")
    assert "my-weird-key-value-xyz" not in t and "sk-abcdefghijklmnop1234" not in t and "abcdefghijklmnopq" not in t
    assert "••••" in t and "…" not in t
    assert secrets.redact("普通文字 sk-1") == "普通文字 sk-1"
    assert secrets.redact(None) is None


def test_redact_log_filter(tmp_path):
    secrets.put("provider/b", "zz-secret-value-0001")
    lg = logging.getLogger("ee.test_redact")
    h = logging.FileHandler(tmp_path / "x.log", encoding="utf-8")
    h.addFilter(secrets.RedactFilter())
    lg.addHandler(h)
    try:
        lg.warning("key=%s", "zz-secret-value-0001")
        try:
            raise ValueError("boom sk-abcdefghijklmnop9999")
        except ValueError:
            lg.exception("失败")
    finally:
        lg.removeHandler(h)
        h.close()
    text = (tmp_path / "x.log").read_text(encoding="utf-8")
    assert "zz-secret-value-0001" not in text and "sk-abcdefghijklmnop9999" not in text


def test_session_store_redacts(tmp_path):
    from ee.core.store import SessionStore
    secrets.put("provider/c", "abc-store-secret-777")
    st = SessionStore.__new__(SessionStore)
    st.enabled, st.root, st.path = True, tmp_path, tmp_path / "s.jsonl"
    st.append("message", {"message": {"role": "tool", "content": "echo abc-store-secret-777"}})
    assert "abc-store-secret-777" not in (tmp_path / "s.jsonl").read_text(encoding="utf-8")


def test_key_priority(ee_home, workspace, monkeypatch):
    from ee.config import load_config
    (ee_home / "config.toml").write_text(
        'config_version = 3\n[providers.deepseek]\nbase_url = "https://api.deepseek.com"\n'
        'api_key_env = "EE_TEST_KEY_ENV"\n', encoding="utf-8")
    monkeypatch.setenv("EE_TEST_KEY_ENV", "sk-from-env-000000")
    spec = load_config(workspace).resolve_model()
    assert spec.key_source() == ("env", "sk-from-env-000000")
    secrets.put("provider/deepseek", "sk-from-store-11111")
    assert spec.key_source() == ("store", "sk-from-store-11111")
    from ee.tools.web import deepseek_key
    assert deepseek_key(load_config(workspace).data) == "sk-from-store-11111"
    secrets.put("web/deepseek", "sk-web-only-2222222")
    assert deepseek_key(load_config(workspace).data) == "sk-web-only-2222222"


def test_migrate_plain_keys(ee_home, workspace):
    from ee.config import load_config, load_user_file
    cfg = ee_home / "config.toml"
    old = ('# 我的配置\nconfig_version = 2\n\n[providers.deepseek]\nbase_url = "https://api.deepseek.com"\n'
           'api_key = "sk-plain-deepseek-1234"  # 旧明文\n\n[web]\nsearch_backend = "tavily"\n'
           'tavily_api_key = "tvly-plain-5678abcd"\n')
    cfg.write_text(old, encoding="utf-8")
    (ee_home / "config.toml.bak").write_text(old, encoding="utf-8")
    (ee_home / "config.toml.pre-v2.bak").write_text(old, encoding="utf-8")
    data = load_user_file()
    assert data["config_version"] == 3
    assert "api_key" not in data["providers"]["deepseek"] and "tavily_api_key" not in data["web"]
    text = cfg.read_text(encoding="utf-8")
    assert "sk-plain" not in text and "tvly-plain" not in text
    assert "# 我的配置" in text and 'search_backend = "tavily"' in text
    for bak in ee_home.glob("config.toml*.bak"):
        b = bak.read_text(encoding="utf-8")
        assert "sk-plain" not in b and "tvly-plain" not in b, bak.name
    assert secrets.get("provider/deepseek") == "sk-plain-deepseek-1234"
    assert secrets.get("web/tavily") == "tvly-plain-5678abcd"
    assert load_config(workspace).resolve_model().key_source() == ("store", "sk-plain-deepseek-1234")


def test_migrate_keeps_store_value(ee_home):
    """存储里已经有（用户在新版设置页填过）就不被旧明文覆盖。"""
    from ee.config import load_user_file
    secrets.put("provider/deepseek", "sk-newer-from-store-99")
    (ee_home / "config.toml").write_text('[providers.deepseek]\nbase_url = "x"\napi_key = "sk-old-plain-000000"\n',
                                         encoding="utf-8")
    load_user_file()
    assert secrets.get("provider/deepseek") == "sk-newer-from-store-99"
    assert "sk-old-plain" not in (ee_home / "config.toml").read_text(encoding="utf-8")


class _Broken(secrets.MemoryStore):
    def set(self, key, value):
        raise secrets.SecretStoreError("坏了")


def test_migrate_store_failure_keeps_plain(ee_home, workspace):
    from ee.config import load_config
    secrets.use(_Broken())
    (ee_home / "config.toml").write_text('[providers.deepseek]\nbase_url = "https://api.deepseek.com"\n'
                                         'api_key = "sk-keep-plain-12345"\n', encoding="utf-8")
    spec = load_config(workspace).resolve_model()
    assert spec.key_source() == ("plain", "sk-keep-plain-12345")      # 存不进去也照样能用
    assert "sk-keep-plain-12345" in (ee_home / "config.toml").read_text(encoding="utf-8")


def test_settings_set_store_failure_is_error(ee_home):
    from ee.server import settings as S
    secrets.use(_Broken())
    st = S.get_settings()["settings"]
    st["providers"]["deepseek"]["api_key"] = "sk-wont-save-123456"
    with pytest.raises(ValueError, match="凭据存储|API key"):
        S.set_settings(st)
    assert "sk-wont-save" not in ((ee_home / "config.toml").read_text(encoding="utf-8")
                                  if (ee_home / "config.toml").exists() else "")


def test_settings_web_key_in_store(ee_home):
    from ee.server import settings as S
    S.set_settings({"web": {"keys": {"brave": {"api_key": "brave-key-abcdef1234"}}}})
    assert secrets.get("web/brave") == "brave-key-abcdef1234"
    assert "brave-key" not in (ee_home / "config.toml").read_text(encoding="utf-8")
    g = S.get_settings()
    assert g["settings"]["web"]["keys"]["brave"]["api_key_source"] == "store"
    assert "brave-key-abcdef1234" not in json.dumps(g)
    S.set_settings({"web": {"keys": {"brave": {"api_key": ""}}}})
    assert secrets.get("web/brave") is None


def test_delete_provider_deletes_secret(ee_home):
    from ee.server import settings as S
    st = S.get_settings()["settings"]
    st["providers"]["relay"] = {"base_url": "https://relay.example.com/v1", "api_key_env": "", "api_key": "sk-relay-12345678"}
    S.set_settings(st)
    assert secrets.get("provider/relay") == "sk-relay-12345678"
    st = S.get_settings()["settings"]
    del st["providers"]["relay"]
    S.set_settings(st)
    assert secrets.get("provider/relay") is None


def test_selftest_secrets(monkeypatch, capsys):
    from ee import cli
    rc = cli.main(["selftest", "--secrets"])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["secrets"] == "ok" and out["secrets_backend"] == "memory"
    assert rc in (0, 1)
