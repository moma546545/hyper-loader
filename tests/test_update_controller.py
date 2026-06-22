from types import SimpleNamespace
import os

try:
    from PySide6.QtCore import QObject
    from PySide6.QtWidgets import QApplication, QMessageBox
except ImportError:
    from PyQt6.QtCore import QObject
    from PyQt6.QtWidgets import QApplication, QMessageBox

from core.window_controllers.update_controller import UpdateController


def _ensure_qt_app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _DummyLabel:
    def __init__(self, text: str = ""):
        self._text = text

    def text(self) -> str:
        return self._text

    def setText(self, value: str):
        self._text = str(value or "")


class _DummyWindow(QObject):
    def __init__(self):
        super().__init__()
        self.logs = []
        self.warnings = []
        self.infos = []
        self.statuses = []
        self.search_view = SimpleNamespace(state_label=_DummyLabel("جاري التحليل"))

    def _append_log(self, value: str):
        self.logs.append(str(value or ""))

    def _warn(self, value: str):
        self.warnings.append(str(value or ""))

    def _info(self, value: str):
        self.infos.append(str(value or ""))

    def _set_status(self, value: str):
        text = str(value or "")
        self.statuses.append(text)
        self.search_view.state_label.setText(text)


class _RunningWorker:
    def isRunning(self):
        return True


class _CancellableRunningWorker:
    def __init__(self):
        self.cancel_requested = False

    def isRunning(self):
        return True

    def requestInterruption(self):
        self.cancel_requested = True


class _ShutdownWorker:
    def __init__(self, running=True):
        self.running = running
        self.interruption_requested = False
        self.quit_called = False
        self.wait_calls = []
        self.deleted = False

    def isRunning(self):
        return self.running

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


def test_start_app_update_download_rejects_duplicate_running_worker():
    _ensure_qt_app()
    window = _DummyWindow()
    controller = UpdateController(window)
    controller._app_update_download_worker = _RunningWorker()

    controller.start_app_update_download(
        {
            "version": "2.0.0",
            "windows": {
                "url": "https://example.com/app.zip",
                "sha256": "a" * 64,
            },
        }
    )

    assert window.warnings == ["يوجد تنزيل تحديث جارٍ بالفعل"]


def test_cancel_app_update_download_warns_when_no_running_worker():
    _ensure_qt_app()
    window = _DummyWindow()
    controller = UpdateController(window)

    ok = controller.cancel_app_update_download()

    assert ok is False
    assert window.warnings == ["لا يوجد تنزيل تحديث جارٍ لإلغائه"]


def test_cancel_app_update_download_requests_interruption_and_updates_status():
    _ensure_qt_app()
    window = _DummyWindow()
    controller = UpdateController(window)
    worker = _CancellableRunningWorker()
    controller._app_update_download_worker = worker

    ok = controller.cancel_app_update_download()

    assert ok is True
    assert worker.cancel_requested is True
    assert any("Cancellation requested" in line for line in window.logs)
    assert window.statuses[-1] == "جارٍ إلغاء تنزيل التحديث..."


def test_app_update_status_is_restored_after_failed_download():
    _ensure_qt_app()
    window = _DummyWindow()
    controller = UpdateController(window)
    controller._begin_app_update_status()
    window._set_status("تحميل التحديث: 55%")

    controller._on_app_update_download_finished(False, "", "network error")

    assert window.warnings == ["تعذر تنزيل التحديث: network error"]
    assert window.search_view.state_label.text() == "جاري التحليل"


def test_app_update_cancelled_download_uses_info_message_not_warning():
    _ensure_qt_app()
    window = _DummyWindow()
    controller = UpdateController(window)
    controller._begin_app_update_status()
    window._set_status("تحميل التحديث: 33%")

    controller._on_app_update_download_finished(False, "", "App update download cancelled")

    assert window.warnings == []
    assert window.infos == ["تم إلغاء تنزيل التحديث"]
    assert any("cancelled by user" in line.lower() for line in window.logs)
    assert window.search_view.state_label.text() == "جاري التحليل"


def test_app_update_status_is_restored_when_user_defers_install(monkeypatch):
    _ensure_qt_app()
    window = _DummyWindow()
    controller = UpdateController(window)
    controller._begin_app_update_status()
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.No)

    controller._on_app_update_download_finished(True, "C:/temp/update.zip", "")

    assert any("Update downloaded successfully" in line for line in window.logs)
    assert window.search_view.state_label.text() == "جاري التحليل"


def test_ytdlp_auto_update_success_resets_retry_count_and_reschedules(monkeypatch):
    _ensure_qt_app()
    window = _DummyWindow()
    controller = UpdateController(window)
    controller.update_retry_count = 2
    controller._last_update_check_was_background = True
    scheduled = []
    monkeypatch.setattr(controller, "schedule_background_update_check", lambda initial_delay_ms=5000: scheduled.append(initial_delay_ms))

    controller._on_yt_dlp_update_finished(0, "ok", "")

    assert controller.update_retry_count == 0
    assert scheduled == [5000]
    assert any("updated successfully" in line for line in window.logs)


def test_ytdlp_manual_check_success_does_not_schedule_background_recheck(monkeypatch):
    _ensure_qt_app()
    window = _DummyWindow()
    controller = UpdateController(window)
    controller.update_retry_count = 2
    controller._last_update_check_was_background = False
    scheduled = []
    monkeypatch.setattr(controller, "schedule_background_update_check", lambda initial_delay_ms=5000: scheduled.append(initial_delay_ms))

    controller._on_yt_dlp_update_finished(0, "ok", "")

    assert controller.update_retry_count == 0
    assert scheduled == []
    assert any("updated successfully" in line for line in window.logs)


def test_check_updates_background_does_not_persist_timestamp_before_cycle_finishes(monkeypatch):
    _ensure_qt_app()
    window = _DummyWindow()
    controller = UpdateController(window)
    saved_settings = []

    class _Worker:
        def __init__(self, *args, **kwargs):
            self.finished = SimpleNamespace(connect=lambda callback: None)

        def isRunning(self):
            return False

        def start(self):
            return None

    monkeypatch.setattr("core.window_controllers.update_controller.load_session_settings", lambda: {})
    monkeypatch.setattr("core.window_controllers.update_controller.save_session_settings", lambda payload: saved_settings.append(dict(payload)))
    monkeypatch.setattr("core.window_controllers.update_controller.YtDlpCoreUpdateWorker", _Worker)

    controller._check_updates(background=True)

    assert saved_settings == []


def test_ytdlp_auto_update_success_persists_background_check_timestamp(monkeypatch):
    _ensure_qt_app()
    window = _DummyWindow()
    controller = UpdateController(window)
    controller._begin_background_update_cycle(app_check_pending=False, ytdlp_pending=True)
    saved_settings = []
    monkeypatch.setattr("core.window_controllers.update_controller.load_session_settings", lambda: {"theme": "Modern Dark"})
    monkeypatch.setattr("core.window_controllers.update_controller.save_session_settings", lambda payload: saved_settings.append(dict(payload)))
    monkeypatch.setattr(controller, "schedule_background_update_check", lambda initial_delay_ms=5000: None)

    controller._on_yt_dlp_update_finished(0, "ok", "")

    assert len(saved_settings) == 1
    assert saved_settings[0]["theme"] == "Modern Dark"
    assert "last_ytdlp_auto_check_ts" in saved_settings[0]


def test_background_cycle_waits_for_app_check_before_persisting_timestamp(monkeypatch):
    _ensure_qt_app()
    window = _DummyWindow()
    controller = UpdateController(window)
    saved_settings = []
    monkeypatch.setattr("core.window_controllers.update_controller.load_session_settings", lambda: {"theme": "Modern Dark"})
    monkeypatch.setattr("core.window_controllers.update_controller.save_session_settings", lambda payload: saved_settings.append(dict(payload)))
    monkeypatch.setattr(controller, "schedule_background_update_check", lambda initial_delay_ms=5000: None)

    controller._begin_background_update_cycle(app_check_pending=True, ytdlp_pending=True)
    controller._on_yt_dlp_update_finished(0, "ok", "")

    assert saved_settings == []
    controller._on_app_update_check_finished(True, {"available": False}, "", True)

    assert len(saved_settings) == 1
    assert saved_settings[0]["theme"] == "Modern Dark"
    assert "last_ytdlp_auto_check_ts" in saved_settings[0]


def test_ytdlp_auto_update_max_retries_schedules_daily_retry(monkeypatch):
    _ensure_qt_app()
    window = _DummyWindow()
    controller = UpdateController(window)
    controller.update_retry_count = 3
    controller._last_update_check_was_background = True
    scheduled = []
    monkeypatch.setattr(
        "core.window_controllers.update_controller.QTimer.singleShot",
        lambda delay_ms, callback: scheduled.append((delay_ms, callback)),
    )

    controller._on_yt_dlp_update_finished(1, "", "boom")

    assert controller.update_retry_count == 0
    assert len(scheduled) == 1
    delay_ms, callback = scheduled[0]
    assert delay_ms == 24 * 60 * 60 * 1000
    assert callback == controller.check_updates_background
    assert any("Max retries reached" in line for line in window.logs)


def test_ytdlp_auto_update_retry_does_not_persist_background_timestamp_early(monkeypatch):
    _ensure_qt_app()
    window = _DummyWindow()
    controller = UpdateController(window)
    controller.update_retry_count = 0
    controller._last_update_check_was_background = True
    saved_settings = []
    scheduled = []
    monkeypatch.setattr("core.window_controllers.update_controller.load_session_settings", lambda: {})
    monkeypatch.setattr("core.window_controllers.update_controller.save_session_settings", lambda payload: saved_settings.append(dict(payload)))
    monkeypatch.setattr(
        "core.window_controllers.update_controller.QTimer.singleShot",
        lambda delay_ms, callback: scheduled.append((delay_ms, callback)),
    )

    controller._on_yt_dlp_update_finished(1, "", "temporary failure")

    assert saved_settings == []
    assert len(scheduled) == 1


def test_ytdlp_manual_check_failure_does_not_schedule_background_retry(monkeypatch):
    _ensure_qt_app()
    window = _DummyWindow()
    controller = UpdateController(window)
    controller.update_retry_count = 2
    controller._last_update_check_was_background = False
    scheduled = []
    monkeypatch.setattr(
        "core.window_controllers.update_controller.QTimer.singleShot",
        lambda delay_ms, callback: scheduled.append((delay_ms, callback)),
    )

    controller._on_yt_dlp_update_finished(1, "", "temporary failure")

    assert controller.update_retry_count == 0
    assert scheduled == []
    assert any("Failed to update yt-dlp core" in line for line in window.logs)


def test_update_controller_shutdown_stops_and_deletes_running_workers():
    _ensure_qt_app()
    window = _DummyWindow()
    controller = UpdateController(window)
    app_check = _ShutdownWorker()
    app_download = _ShutdownWorker()
    ytdlp_core = _ShutdownWorker()
    ytdlp_pip = _ShutdownWorker(running=False)
    controller._app_update_check_worker = app_check
    controller._app_update_download_worker = app_download
    controller._yt_dlp_update_worker = ytdlp_core
    controller._yt_dlp_pip_update_worker = ytdlp_pip
    controller.update_retry_count = 3
    controller._last_update_check_was_background = True
    controller._begin_background_update_cycle(app_check_pending=True, ytdlp_pending=True)

    controller.shutdown()

    assert controller._app_update_check_worker is None
    assert controller._app_update_download_worker is None
    assert controller._yt_dlp_update_worker is None
    assert controller._yt_dlp_pip_update_worker is None
    assert app_check.interruption_requested is True
    assert app_check.quit_called is True
    assert app_check.wait_calls == [1000]
    assert app_check.deleted is True
    assert app_download.interruption_requested is True
    assert app_download.quit_called is True
    assert app_download.wait_calls == [1000]
    assert app_download.deleted is True
    assert ytdlp_core.interruption_requested is True
    assert ytdlp_core.quit_called is True
    assert ytdlp_core.wait_calls == [1000]
    assert ytdlp_core.deleted is True
    assert ytdlp_pip.interruption_requested is False
    assert ytdlp_pip.quit_called is False
    assert ytdlp_pip.wait_calls == []
    assert ytdlp_pip.deleted is True
    assert controller.update_retry_count == 0
    assert controller._last_update_check_was_background is False
    assert controller._background_update_cycle_active is False
    assert controller._background_app_check_pending is False
    assert controller._background_ytdlp_terminal_pending is False


def test_start_app_update_download_uses_private_runtime_dir(monkeypatch, tmp_path):
    _ensure_qt_app()
    window = _DummyWindow()
    monkeypatch.setattr("core.window_controllers.update_controller.get_app_data_dir", lambda: str(tmp_path))
    captured = {}

    class _Worker:
        def __init__(self, url, sha256, out_path):
            captured["url"] = url
            captured["sha256"] = sha256
            captured["out_path"] = out_path
            self.progress = SimpleNamespace(connect=lambda callback: None)
            self.finished = SimpleNamespace(connect=lambda callback: None)

        def start(self):
            captured["started"] = True

        def isRunning(self):
            return False

    monkeypatch.setattr("core.window_controllers.update_controller.AppUpdateDownloadWorker", _Worker)

    controller = UpdateController(window)
    controller.start_app_update_download(
        {
            "version": "2.0.0 rc/1",
            "windows": {
                "url": "https://example.com/app.zip",
                "sha256": "a" * 64,
            },
        }
    )

    assert captured["started"] is True
    assert captured["url"] == "https://example.com/app.zip"
    assert captured["sha256"] == "a" * 64
    assert captured["out_path"].startswith(str(tmp_path))
    assert captured["out_path"].endswith(os.path.join("update_runtime", "VidDownloader-2.0.0-rc-1.zip"))
