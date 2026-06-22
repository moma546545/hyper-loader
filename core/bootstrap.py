import sys

from core.qt_compat import QApplication
from core.error_handler import install_global_error_handlers


def start_app(window_cls):
    app = QApplication.instance() or QApplication(sys.argv)
    if hasattr(app, "setQuitOnLastWindowClosed"):
        app.setQuitOnLastWindowClosed(False)
    if hasattr(app, "font") and hasattr(app, "setFont"):
        app_font = app.font()
        if app_font.pointSize() <= 0:
            app_font.setPointSize(10)
            app.setFont(app_font)
    install_global_error_handlers()
    window = window_cls()
    lifecycle = getattr(window, "lifecycle_controller", None)
    if lifecycle is not None and hasattr(lifecycle, "close_event"):
        app.aboutToQuit.connect(lifecycle.close_event)
    window.show()
    return int(app.exec())
