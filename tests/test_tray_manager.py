from core.window_controllers.tray_manager import TrayManager


class _DummyWindow:
    def __init__(self):
        self.show_calls = 0
        self.raise_calls = 0
        self.activate_calls = 0

    def showNormal(self):
        self.show_calls += 1

    def raise_(self):
        self.raise_calls += 1

    def activateWindow(self):
        self.activate_calls += 1


def test_tray_manager_show_main_window_restores_and_activates_window():
    window = _DummyWindow()
    manager = TrayManager(window)

    manager._show_main_window()

    assert window.show_calls == 1
    assert window.raise_calls == 1
    assert window.activate_calls == 1


def test_tray_manager_quit_application_sets_flags_and_quits_event_loop(monkeypatch):
    class _DummyApp:
        def __init__(self):
            self.quit_calls = 0

        def quit(self):
            self.quit_calls += 1

    class _DummyQApp:
        _instance = _DummyApp()

        @classmethod
        def instance(cls):
            return cls._instance

    class _DummyWindowForQuit:
        def __init__(self):
            self._quit_to_tray_bypass = False
            self._app_is_quitting = False
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    monkeypatch.setattr("core.window_controllers.tray_manager.QApplication", _DummyQApp)
    window = _DummyWindowForQuit()
    manager = TrayManager(window)

    manager._quit_application()

    assert window._quit_to_tray_bypass is True
    assert window._app_is_quitting is True
    assert window.close_calls == 1
    assert _DummyQApp._instance.quit_calls == 1
