import threading
import time

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    from PyQt6.QtWidgets import QApplication

from core.qt_dispatch import run_on_qt_main_thread


def _ensure_qt_app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_run_on_qt_main_thread_queues_callback_from_worker_thread():
    app = _ensure_qt_app()
    called = []
    done = threading.Event()
    result = {}

    def callback():
        called.append(threading.current_thread() is threading.main_thread())
        done.set()

    def worker():
        result["queued"] = run_on_qt_main_thread(callback)

    thread = threading.Thread(target=worker, name="DispatchWorker")
    thread.start()
    thread.join()

    deadline = time.time() + 1.0
    while not done.is_set() and time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)

    assert result["queued"] is True
    assert done.is_set() is True
    assert called == [True]
