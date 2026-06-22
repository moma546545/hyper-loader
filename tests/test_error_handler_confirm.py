import threading
import time

try:
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QApplication, QMessageBox
except ImportError:
    from PyQt6.QtCore import QThread
    from PyQt6.QtWidgets import QApplication, QMessageBox

from core.error_handler import ErrorHandler


def _ensure_qt_app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_confirm_dispatches_to_qt_main_thread_from_worker(monkeypatch):
    app = _ensure_qt_app()
    finished = threading.Event()
    result = {}
    called_threads = []

    def _fake_question(*_args, **_kwargs):
        called_threads.append(QThread.currentThread())
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", _fake_question)

    def _worker():
        result["accepted"] = ErrorHandler.confirm(None, "Confirm", "Proceed?")
        finished.set()

    t = threading.Thread(target=_worker, name="ConfirmWorker")
    t.start()

    deadline = time.time() + 2.0
    while not finished.is_set() and time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)

    t.join(timeout=0.2)

    assert finished.is_set() is True
    assert result["accepted"] is True
    assert called_threads
    assert called_threads[0] == app.thread()
