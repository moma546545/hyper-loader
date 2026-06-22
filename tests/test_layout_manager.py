try:
    from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QWidget
except ImportError:
    from PyQt6.QtWidgets import QApplication, QLabel, QMainWindow, QWidget

import ui.layout_manager as layout_manager


def _ensure_qt_app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _DummySignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)


class _DummyWidget(QWidget):
    def __init__(self, *args, **kwargs):
        super().__init__()


class _DummySidebar(QWidget):
    def __init__(self, *args, **kwargs):
        super().__init__()
        self.view_changed = _DummySignal()


class _DummyWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.theme = "Modern Dark"
        self.net_manager = object()
        self.toast_inits = 0
        self.trial_timer_inits = 0
        self.switched_view = None
        self.search_state = None

    def _qss(self):
        return ""

    def _build_title_bar(self):
        return QLabel("title")

    def _init_toast(self):
        self.toast_inits += 1

    def _init_trial_timer(self):
        self.trial_timer_inits += 1

    def _switch_view(self, view_name):
        self.switched_view = view_name

    def _set_search_state(self, state):
        self.search_state = state

    def _wire_views(self):
        return None


def test_build_main_window_ui_smoke(monkeypatch):
    _ensure_qt_app()
    monkeypatch.setattr(layout_manager, "PremiumSidebar", _DummySidebar)
    monkeypatch.setattr(layout_manager, "SearchView", _DummyWidget)
    monkeypatch.setattr(layout_manager, "SmartBrowserView", _DummyWidget)
    monkeypatch.setattr(layout_manager, "DownloadsView", _DummyWidget)
    monkeypatch.setattr(layout_manager, "SettingsView", _DummyWidget)
    monkeypatch.setattr(layout_manager, "ToolsView", _DummyWidget)
    monkeypatch.setattr(layout_manager, "StatsView", _DummyWidget)
    monkeypatch.setattr(layout_manager, "ErrorDashboard", _DummyWidget)
    monkeypatch.setattr(layout_manager, "PlaylistView", _DummyWidget)
    monkeypatch.setattr(layout_manager, "SubscriptionsView", _DummyWidget)
    monkeypatch.setattr(layout_manager, "NotificationOverlay", _DummyWidget)
    monkeypatch.setattr(layout_manager, "_bind_post_build_hooks", lambda window: None)

    window = _DummyWindow()
    layout_manager.build_main_window_ui(window)

    assert window.centralWidget() is not None
    assert window.sidebar is not None
    assert window.main_stack.count() == 9
    assert window.notification_overlay is not None
    assert window.toast_inits == 1
    assert window.trial_timer_inits == 1
    assert window.switched_view == "search"
    assert window.search_state == "empty"
