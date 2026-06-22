try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMainWindow
except ImportError:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication, QMainWindow

from ui.views.title_bar import build_title_bar


def _ensure_qt_app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _DummyEvent:
    def __init__(self, button):
        self._button = button
        self.accepted = False

    def button(self):
        return self._button

    def accept(self):
        self.accepted = True


class _DummyWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.theme = "Modern Dark"
        self._maximized = False

    def _toggle_theme(self):
        return None

    def _toggle_dark_light_mode(self):
        return None

    def _close_window(self):
        return None

    def isMaximized(self):
        return self._maximized

    def showNormal(self):
        self._maximized = False

    def showMaximized(self):
        self._maximized = True


def test_title_bar_double_click_toggles_maximize_restore():
    _ensure_qt_app()
    window = _DummyWindow()
    title_bar = build_title_bar(window)

    first_event = _DummyEvent(Qt.MouseButton.LeftButton)
    title_bar.mouseDoubleClickEvent(first_event)
    assert first_event.accepted is True
    assert window.isMaximized() is True

    second_event = _DummyEvent(Qt.MouseButton.LeftButton)
    title_bar.mouseDoubleClickEvent(second_event)
    assert second_event.accepted is True
    assert window.isMaximized() is False
