from types import SimpleNamespace

from core import bootstrap


def test_start_app_disables_quit_on_last_window_closed(monkeypatch):
    calls = {"set_quit_on_last_window_closed": [], "show": 0, "exec": 0}

    class _DummyApp:
        _instance = None

        def __init__(self, *_args, **_kwargs):
            _DummyApp._instance = self

        @classmethod
        def instance(cls):
            return cls._instance

        def setQuitOnLastWindowClosed(self, value):
            calls["set_quit_on_last_window_closed"].append(bool(value))

        def font(self):
            return SimpleNamespace(pointSize=lambda: 10)

        def setFont(self, _font):
            return None

        @property
        def aboutToQuit(self):
            return SimpleNamespace(connect=lambda _fn: None)

        def exec(self):
            calls["exec"] += 1
            return 0

    class _DummyWindow:
        def __init__(self):
            self.lifecycle_controller = None

        def show(self):
            calls["show"] += 1

    monkeypatch.setattr(bootstrap, "QApplication", _DummyApp)
    monkeypatch.setattr(bootstrap, "install_global_error_handlers", lambda: None)

    result = bootstrap.start_app(_DummyWindow)

    assert result == 0
    assert calls["set_quit_on_last_window_closed"] == [False]
    assert calls["show"] == 1
    assert calls["exec"] == 1
