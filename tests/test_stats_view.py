try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    from PyQt6.QtWidgets import QApplication

from ui.stats_view import StatsView


def _ensure_qt_app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _DummySignal:
    def __init__(self):
        self._callbacks = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def emit(self, *args, **kwargs):
        for callback in list(self._callbacks):
            callback(*args, **kwargs)


class _DummyStatsWorker:
    instances = []

    def __init__(self):
        self.finished = _DummySignal()
        self.running = True
        self.started = False
        self.interruption_requested = False
        self.quit_called = False
        self.wait_calls = []
        self.deleted = False
        type(self).instances.append(self)

    def isRunning(self):
        return self.running

    def start(self):
        self.started = True

    def requestInterruption(self):
        self.interruption_requested = True

    def quit(self):
        self.quit_called = True

    def wait(self, timeout_ms):
        self.wait_calls.append(timeout_ms)
        self.running = False
        return True

    def deleteLater(self):
        self.deleted = True


def test_stats_view_refresh_does_not_replace_running_worker(monkeypatch):
    _ensure_qt_app()
    _DummyStatsWorker.instances = []
    monkeypatch.setattr("ui.stats_view.StatsWorker", _DummyStatsWorker)

    view = StatsView()
    first_worker = view.worker

    view.refresh()

    assert len(_DummyStatsWorker.instances) == 1
    assert view.worker is first_worker
    assert first_worker.started is True
    view.close()


def test_stats_view_close_event_stops_and_deletes_running_worker(monkeypatch):
    _ensure_qt_app()
    _DummyStatsWorker.instances = []
    monkeypatch.setattr("ui.stats_view.StatsWorker", _DummyStatsWorker)

    view = StatsView()
    worker = view.worker

    view.close()

    assert worker is not None
    assert worker.interruption_requested is True
    assert worker.quit_called is True
    assert worker.wait_calls == [500]
    assert worker.deleted is True
    assert view.worker is None
