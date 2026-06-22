from types import SimpleNamespace

from core.window_bootstrap import close_window


class _DummyTrayIcon:
    def __init__(self, visible=True):
        self._visible = bool(visible)
        self.messages = []
        self.show_calls = 0

    def isVisible(self):
        return self._visible

    def show(self):
        self.show_calls += 1
        self._visible = True

    def showMessage(self, title, message, icon, timeout):
        self.messages.append((title, message, icon, timeout))


def test_close_window_uses_tray_manager_icon_when_legacy_attr_missing():
    tray_icon = _DummyTrayIcon(visible=True)
    window = SimpleNamespace(
        tray_manager=SimpleNamespace(tray_icon=tray_icon),
        hide_calls=0,
        close_calls=0,
        _active_workers_count=lambda: 3,
    )

    def _hide():
        window.hide_calls += 1

    def _close():
        window.close_calls += 1

    window.hide = _hide
    window.close = _close

    close_window(window)

    assert window.hide_calls == 1
    assert window.close_calls == 0
    assert len(tray_icon.messages) == 1
    assert "3" in tray_icon.messages[0][1]


def test_close_window_hides_to_tray_even_when_icon_starts_hidden():
    tray_icon = _DummyTrayIcon(visible=False)
    window = SimpleNamespace(
        close_to_tray_enabled=True,
        tray_manager=SimpleNamespace(tray_icon=tray_icon),
        hide_calls=0,
        close_calls=0,
        _active_workers_count=lambda: 1,
    )

    def _hide():
        window.hide_calls += 1

    def _close():
        window.close_calls += 1

    window.hide = _hide
    window.close = _close

    close_window(window)

    assert tray_icon.show_calls == 1
    assert window.hide_calls == 1
    assert window.close_calls == 0
    assert len(tray_icon.messages) == 1


def test_close_window_respects_close_to_tray_disabled_flag():
    tray_icon = _DummyTrayIcon(visible=True)
    window = SimpleNamespace(
        close_to_tray_enabled=False,
        tray_manager=SimpleNamespace(tray_icon=tray_icon),
        hide_calls=0,
        close_calls=0,
        _active_workers_count=lambda: 0,
    )

    def _hide():
        window.hide_calls += 1

    def _close():
        window.close_calls += 1

    window.hide = _hide
    window.close = _close

    close_window(window)

    assert window.hide_calls == 0
    assert window.close_calls == 1
    assert tray_icon.messages == []
