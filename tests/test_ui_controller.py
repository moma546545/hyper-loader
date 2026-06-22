try:
    from PySide6.QtWidgets import QApplication, QWidget, QStackedWidget
except ImportError:
    from PyQt6.QtWidgets import QApplication, QWidget, QStackedWidget

from core.ui_controller import UIController


def _ensure_qt_app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _DummyWindow:
    def __init__(self):
        self.main_stack = QStackedWidget()
        self.search_view = QWidget()
        self.subscriptions_view = QWidget()
        self.main_stack.addWidget(self.search_view)
        self.main_stack.addWidget(self.subscriptions_view)
        self.active_view = None
        self.sidebar = None


def test_switch_view_supports_subscriptions_alias():
    _ensure_qt_app()
    window = _DummyWindow()
    controller = UIController(window)

    controller.switch_view("subscriptions")

    assert window.main_stack.currentWidget() is window.subscriptions_view
    assert window.active_view == "subscriptions"
