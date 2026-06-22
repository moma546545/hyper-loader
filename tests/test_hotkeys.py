import core.hotkeys as hotkeys


def test_setup_default_hotkeys_skips_global_ctrl_v_by_default(monkeypatch):
    monkeypatch.setattr(hotkeys, "_KEYBOARD_AVAILABLE", True)
    monkeypatch.delenv("VIDDOWNLOADER_ENABLE_GLOBAL_CTRL_V", raising=False)

    manager = hotkeys.setup_default_hotkeys(object())
    combos = {item["hotkey"] for item in manager.get_registered()}

    assert "ctrl+shift+v" in combos
    assert "ctrl+v" not in combos


def test_setup_default_hotkeys_allows_global_ctrl_v_via_env(monkeypatch):
    monkeypatch.setattr(hotkeys, "_KEYBOARD_AVAILABLE", True)
    monkeypatch.setenv("VIDDOWNLOADER_ENABLE_GLOBAL_CTRL_V", "1")

    manager = hotkeys.setup_default_hotkeys(object())
    combos = {item["hotkey"] for item in manager.get_registered()}

    assert "ctrl+v" in combos
