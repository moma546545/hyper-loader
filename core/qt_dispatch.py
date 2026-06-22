"""
core/qt_dispatch.py - Safe Qt main-thread callback dispatch helpers.

Use this from Python worker threads when UI-affecting callbacks must be
scheduled onto the Qt event loop.
"""
import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger("SnapDownloader.QtDispatch")
_dispatcher_lock = threading.Lock()

try:
    from PySide6.QtCore import QCoreApplication, QObject, Qt, Signal
except ImportError:
    try:
        from PyQt6.QtCore import QCoreApplication, QObject, Qt, pyqtSignal as Signal
    except ImportError:
        QCoreApplication = None
        QObject = None
        Qt = None
        Signal = None


if QObject is not None and Signal is not None and Qt is not None:
    class _QtMainThreadDispatcher(QObject):
        invoke = Signal(object, object, object)

        def __init__(self):
            super().__init__()
            self.invoke.connect(self._dispatch, Qt.ConnectionType.QueuedConnection)

        def _dispatch(self, callback, args, kwargs):
            try:
                callback(*(tuple(args or ())), **dict(kwargs or {}))
            except Exception as exc:
                logger.error(f"[QtDispatch] Callback failed: {exc}")
else:
    _QtMainThreadDispatcher = None


_dispatcher: Optional[object] = None


def _get_dispatcher():
    global _dispatcher
    if _QtMainThreadDispatcher is None or QCoreApplication is None:
        return None
    app = QCoreApplication.instance()
    if app is None:
        return None
    with _dispatcher_lock:
        if _dispatcher is None:
            _dispatcher = _QtMainThreadDispatcher()
        try:
            app_thread = app.thread()
            if _dispatcher.thread() != app_thread:
                _dispatcher.moveToThread(app_thread)
        except Exception as exc:
            logger.debug(f"[QtDispatch] Failed to move dispatcher to app thread: {exc}")
        return _dispatcher


def run_on_qt_main_thread(callback: Optional[Callable], *args, **kwargs) -> bool:
    """
    Queue `callback` on the Qt main thread when possible.

    Returns True when the callback is queued through Qt, otherwise False and the
    caller may fall back to a direct call if that is safe.
    """
    if callback is None:
        return False
    dispatcher = _get_dispatcher()
    if dispatcher is None:
        return False
    try:
        dispatcher.invoke.emit(callback, tuple(args), dict(kwargs))
        return True
    except Exception as exc:
        logger.debug(f"[QtDispatch] Failed to queue callback: {exc}")
        return False
