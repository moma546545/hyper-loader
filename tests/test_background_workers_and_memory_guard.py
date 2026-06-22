import json
import os
import threading

import pytest

try:
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QApplication
except ImportError:
    from PyQt6.QtCore import QThread
    from PyQt6.QtWidgets import QApplication

from core.background_workers import (
    AppUpdateCheckWorker,
    AppUpdateDownloadWorker,
    YtDlpCoreUpdateWorker,
    YtDlpPipUpdateWorker,
    check_production_safety,
)
from core.memory_guard import _safe_invoke_callback


def _ensure_qt_app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _DummyResponse:
    def __init__(self, payload: dict, final_url: str):
        self._payload = json.dumps(payload).encode("utf-8")
        self._final_url = final_url
        self.headers = {"Content-Length": str(len(self._payload))}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def geturl(self):
        return self._final_url

    def read(self, _size: int = -1):
        return self._payload


class _ChunkedResponse:
    def __init__(self, chunks, final_url: str):
        self._chunks = list(chunks or [])
        self._index = 0
        self._final_url = final_url
        self.headers = {"Content-Length": str(sum(len(c or b"") for c in self._chunks))}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def geturl(self):
        return self._final_url

    def read(self, _size: int = -1):
        if self._index >= len(self._chunks):
            return b""
        value = self._chunks[self._index]
        self._index += 1
        return value


def test_app_update_check_worker_retries_timeout_then_succeeds(monkeypatch):
    _ensure_qt_app()
    monkeypatch.setenv("VIDDOWNLOADER_ALLOW_UNSIGNED_UPDATES", "1")
    monkeypatch.setenv("VIDDOWNLOADER_ENABLE_UPDATE_SECURITY_OVERRIDES", "1")
    attempts = {"count": 0}
    sleeps = []
    results = []

    def _fake_urlopen(_req, timeout):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise TimeoutError(f"timeout #{attempts['count']}")
        assert timeout == 8
        return _DummyResponse({"version": "9.9.9"}, "https://example.com/manifest.json")

    monkeypatch.setattr("core.background_workers.time.sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr("core.background_workers.urllib.request.urlopen", _fake_urlopen)

    worker = AppUpdateCheckWorker("1.0.0", "https://example.com/manifest.json")
    worker.finished.connect(lambda ok, payload, error: results.append((ok, payload, error)))
    worker.run()

    assert attempts["count"] == 3
    assert sum(sleeps) == pytest.approx(3.0)
    assert sleeps
    assert len(results) == 1
    ok, payload, error = results[0]
    assert ok is True
    assert error == ""
    assert payload["available"] is True
    assert payload["version"] == "9.9.9"


def test_app_update_check_worker_ignores_unsigned_override_without_explicit_ack(monkeypatch):
    _ensure_qt_app()
    results = []
    monkeypatch.setenv("VIDDOWNLOADER_ALLOW_UNSIGNED_UPDATES", "1")
    monkeypatch.delenv("VIDDOWNLOADER_ENABLE_UPDATE_SECURITY_OVERRIDES", raising=False)
    monkeypatch.setattr(
        "core.background_workers.urllib.request.urlopen",
        lambda _req, timeout: _DummyResponse({"version": "9.9.9"}, "https://example.com/manifest.json"),
    )

    worker = AppUpdateCheckWorker("1.0.0", "https://example.com/manifest.json")
    worker.finished.connect(lambda ok, payload, error: results.append((ok, payload, error)))
    worker.run()

    assert len(results) == 1
    ok, payload, error = results[0]
    assert ok is False
    assert payload == {}
    assert "Unsigned updates are blocked" in error
    assert "VIDDOWNLOADER_ENABLE_UPDATE_SECURITY_OVERRIDES=1" in error


def test_check_production_safety_allows_overrides_only_outside_production(monkeypatch):
    monkeypatch.setenv("VIDDOWNLOADER_ALLOW_INSECURE_UPDATES", "1")
    monkeypatch.setenv("VIDDOWNLOADER_ALLOW_UNSIGNED_UPDATES", "1")
    monkeypatch.setenv("VIDDOWNLOADER_ENABLE_UPDATE_SECURITY_OVERRIDES", "1")
    monkeypatch.delenv("VIDDOWNLOADER_ENV", raising=False)
    monkeypatch.delenv("VIDDOWNLOADER_SIGNED_BUILD", raising=False)

    policy = check_production_safety()

    assert policy["is_production"] is False
    assert policy["allow_insecure"] is True
    assert policy["allow_unsigned"] is True


def test_check_production_safety_force_disables_overrides_in_production(monkeypatch):
    monkeypatch.setenv("VIDDOWNLOADER_ALLOW_INSECURE_UPDATES", "1")
    monkeypatch.setenv("VIDDOWNLOADER_ALLOW_UNSIGNED_UPDATES", "1")
    monkeypatch.setenv("VIDDOWNLOADER_ENABLE_UPDATE_SECURITY_OVERRIDES", "1")
    monkeypatch.setenv("VIDDOWNLOADER_ENV", "production")

    policy = check_production_safety()

    assert policy["is_production"] is True
    assert policy["allow_insecure"] is False
    assert policy["allow_unsigned"] is False


def test_app_update_check_worker_allows_unsigned_override_with_explicit_ack(monkeypatch):
    _ensure_qt_app()
    results = []
    monkeypatch.setenv("VIDDOWNLOADER_ALLOW_UNSIGNED_UPDATES", "1")
    monkeypatch.setenv("VIDDOWNLOADER_ENABLE_UPDATE_SECURITY_OVERRIDES", "1")
    monkeypatch.setattr(
        "core.background_workers.urllib.request.urlopen",
        lambda _req, timeout: _DummyResponse({"version": "9.9.9"}, "https://example.com/manifest.json"),
    )

    worker = AppUpdateCheckWorker("1.0.0", "https://example.com/manifest.json")
    worker.finished.connect(lambda ok, payload, error: results.append((ok, payload, error)))
    worker.run()

    assert len(results) == 1
    ok, payload, error = results[0]
    assert ok is True
    assert error == ""
    assert payload["available"] is True
    assert payload["version"] == "9.9.9"


def test_memory_guard_safe_invoke_callback_queues_to_qt_thread():
    app = _ensure_qt_app()
    done = threading.Event()
    captured = []

    def _callback(value):
        captured.append((value, QThread.currentThread()))
        done.set()

    worker = threading.Thread(target=lambda: _safe_invoke_callback(_callback, 42), daemon=True)
    worker.start()
    worker.join()

    for _ in range(100):
        if done.wait(0.01):
            break
        app.processEvents()
    app.processEvents()

    assert done.is_set()
    assert captured[0][0] == 42
    assert captured[0][1] == app.thread()


def test_ytdlp_pip_update_worker_retries_timeout_then_succeeds(monkeypatch):
    _ensure_qt_app()
    attempts = {"count": 0}
    sleeps = []
    results = []

    class _ProcResult:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def _fake_run(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise TimeoutError("network timeout")
        return _ProcResult(0, "updated", "")

    monkeypatch.setattr("core.background_workers.subprocess.run", _fake_run)
    monkeypatch.setattr("core.background_workers.time.sleep", lambda seconds: sleeps.append(seconds))

    worker = YtDlpPipUpdateWorker()
    worker.finished.connect(lambda ok, message: results.append((ok, message)))
    worker.run()

    assert attempts["count"] == 3
    assert sum(sleeps) == pytest.approx(3.0)
    assert sleeps
    assert results == [(True, "updated")]


def test_ytdlp_core_update_worker_retries_retryable_error_then_succeeds(monkeypatch):
    _ensure_qt_app()
    attempts = {"count": 0}
    sleeps = []
    results = []

    class _ProcResult:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def _fake_run(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] < 3:
            return _ProcResult(1, "", "HTTP Error 429: Too Many Requests")
        return _ProcResult(0, "already up to date", "")

    monkeypatch.setattr("core.background_workers.subprocess.run", _fake_run)
    monkeypatch.setattr("core.background_workers.time.sleep", lambda seconds: sleeps.append(seconds))

    worker = YtDlpCoreUpdateWorker()
    worker.finished.connect(lambda code, stdout, stderr: results.append((code, stdout, stderr)))
    worker.run()

    assert attempts["count"] == 3
    assert sum(sleeps) == pytest.approx(3.0)
    assert sleeps
    assert results == [(0, "already up to date", "")]


def test_ytdlp_core_update_worker_interruption_aborts_retry_backoff(monkeypatch):
    _ensure_qt_app()
    attempts = {"count": 0}
    interrupted = {"value": False}
    sleeps = []
    results = []

    class _ProcResult:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def _fake_run(*args, **kwargs):
        attempts["count"] += 1
        return _ProcResult(1, "", "HTTP Error 429: Too Many Requests")

    def _fake_sleep(seconds):
        sleeps.append(seconds)
        interrupted["value"] = True

    monkeypatch.setattr("core.background_workers.subprocess.run", _fake_run)
    monkeypatch.setattr("core.background_workers.time.sleep", _fake_sleep)

    worker = YtDlpCoreUpdateWorker()
    monkeypatch.setattr(worker, "isInterruptionRequested", lambda: interrupted["value"])
    worker.finished.connect(lambda code, stdout, stderr: results.append((code, stdout, stderr)))
    worker.run()

    assert attempts["count"] == 1
    assert sum(sleeps) > 0
    assert results == [(-1, "", "yt-dlp core update cancelled")]


def test_app_update_download_worker_honors_pre_start_interruption(tmp_path, monkeypatch):
    _ensure_qt_app()
    out_path = tmp_path / "update.zip"
    results = []
    opened = []

    def _fake_urlopen(_req, timeout):
        opened.append(timeout)
        return _ChunkedResponse([b"abc"], "https://example.com/update.zip")

    monkeypatch.setattr("core.background_workers.urllib.request.urlopen", _fake_urlopen)

    worker = AppUpdateDownloadWorker("https://example.com/update.zip", "", str(out_path))
    monkeypatch.setattr(worker, "isInterruptionRequested", lambda: True)
    worker.finished.connect(lambda ok, path, error: results.append((ok, path, error)))
    worker.run()

    assert opened == []
    assert len(results) == 1
    ok, path, error = results[0]
    assert ok is False
    assert path == ""
    assert "cancelled" in error.lower()
    assert not out_path.exists()


def test_app_update_check_worker_interruption_aborts_retry_backoff(monkeypatch):
    _ensure_qt_app()
    monkeypatch.setenv("VIDDOWNLOADER_ALLOW_UNSIGNED_UPDATES", "1")
    monkeypatch.setenv("VIDDOWNLOADER_ENABLE_UPDATE_SECURITY_OVERRIDES", "1")
    attempts = {"count": 0}
    interrupted = {"value": False}
    sleeps = []
    results = []

    def _fake_urlopen(_req, timeout):
        attempts["count"] += 1
        raise TimeoutError("timeout")

    def _fake_sleep(seconds):
        sleeps.append(seconds)
        interrupted["value"] = True

    monkeypatch.setattr("core.background_workers.urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setattr("core.background_workers.time.sleep", _fake_sleep)

    worker = AppUpdateCheckWorker("1.0.0", "https://example.com/manifest.json")
    monkeypatch.setattr(worker, "isInterruptionRequested", lambda: interrupted["value"])
    worker.finished.connect(lambda ok, payload, error: results.append((ok, payload, error)))
    worker.run()

    assert attempts["count"] == 1
    assert sum(sleeps) > 0
    assert results == [(False, {}, "App update check cancelled")]


def test_app_update_download_worker_cleans_partial_file_when_interrupted_mid_stream(tmp_path, monkeypatch):
    _ensure_qt_app()
    out_path = tmp_path / "update.zip"
    results = []
    interrupted = {"value": False}

    class _InterruptingResponse(_ChunkedResponse):
        def read(self, size: int = -1):
            value = super().read(size)
            if value:
                interrupted["value"] = True
            return value

    monkeypatch.setattr(
        "core.background_workers.urllib.request.urlopen",
        lambda _req, timeout: _InterruptingResponse([b"part-1", b"part-2"], "https://example.com/update.zip"),
    )

    worker = AppUpdateDownloadWorker("https://example.com/update.zip", "", str(out_path))
    monkeypatch.setattr(worker, "isInterruptionRequested", lambda: bool(interrupted["value"]))
    worker.finished.connect(lambda ok, path, error: results.append((ok, path, error)))
    worker.run()

    assert len(results) == 1
    ok, path, error = results[0]
    assert ok is False
    assert path == ""
    assert "cancelled" in error.lower()
    assert not os.path.exists(str(out_path))


def test_app_update_download_worker_ignores_insecure_override_without_explicit_ack(tmp_path, monkeypatch):
    _ensure_qt_app()
    out_path = tmp_path / "update.zip"
    results = []
    monkeypatch.setenv("VIDDOWNLOADER_ALLOW_INSECURE_UPDATES", "1")
    monkeypatch.delenv("VIDDOWNLOADER_ENABLE_UPDATE_SECURITY_OVERRIDES", raising=False)

    worker = AppUpdateDownloadWorker("http://example.com/update.zip", "", str(out_path))
    worker.finished.connect(lambda ok, path, error: results.append((ok, path, error)))
    worker.run()

    assert len(results) == 1
    ok, path, error = results[0]
    assert ok is False
    assert path == ""
    assert error == "Update download URL must use HTTPS"
