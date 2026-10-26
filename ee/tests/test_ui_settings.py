"""D4-5：通知 / 托盘设置读写。"""
import pytest

from ee.server import settings as S


def test_ui_defaults_and_roundtrip(ee_home):
    out = S.get_settings()["settings"]["ui"]
    assert out == {"theme": "system", "notify": "unfocused", "notify_approval": True, "close_to_tray": True}
    assert S.get_settings()["choices"]["notify_modes"] == ["never", "unfocused", "always"]
    st = S.get_settings()["settings"]
    st["ui"] = {"theme": "dark", "notify": "always", "close_to_tray": False, "notify_approval": False}
    res = S.set_settings(st)["settings"]["ui"]
    assert res == {"theme": "dark", "notify": "always", "notify_approval": False, "close_to_tray": False}
    text = (ee_home / "config.toml").read_text(encoding="utf-8")
    assert 'notify = "always"' in text and "close_to_tray = false" in text


def test_ui_bad_notify(ee_home):
    st = S.get_settings()["settings"]
    st["ui"] = {"notify": "sometimes"}
    with pytest.raises(ValueError):
        S.set_settings(st)
