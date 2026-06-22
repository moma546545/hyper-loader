import sys
import types

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    from PyQt6.QtWidgets import QApplication

import pytest

from core.file_integrity_watcher import FileIntegrityWatcher
from core.tls_transport import TransportCancelled, download_direct_file


def _ensure_qt_app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _FakeResponse:
    def __init__(self, chunks, *, url="https://example.com/video.mp4", headers=None):
        self._chunks = list(chunks)
        self.url = url
        self.headers = dict(headers or {"Content-Length": str(sum(len(chunk) for chunk in chunks))})
        self.closed = False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=0):
        _ = chunk_size
        for chunk in self._chunks:
            yield chunk

    def close(self):
        self.closed = True


class _FakeSession:
    def __init__(self, response):
        self._response = response
        self.closed = False

    def get(self, *args, **kwargs):
        _ = args, kwargs
        return self._response

    def close(self):
        self.closed = True


def _install_fake_curl(monkeypatch, response):
    fake_session = _FakeSession(response)
    fake_requests = types.SimpleNamespace(Session=lambda: fake_session)
    fake_module = types.ModuleType("curl_cffi")
    fake_module.requests = fake_requests
    monkeypatch.setitem(sys.modules, "curl_cffi", fake_module)
    return fake_session


def test_tls_transport_writes_to_temp_then_atomically_replaces(monkeypatch, tmp_path):
    response = _FakeResponse([b"hello", b" ", b"world"])
    _install_fake_curl(monkeypatch, response)
    progress_updates = []

    out_path, written = download_direct_file(
        url="https://example.com/video.mp4",
        out_dir=str(tmp_path),
        headers=None,
        proxy="",
        impersonate="chrome",
        cancel_check=lambda: False,
        on_progress=lambda downloaded, total, speed: progress_updates.append((downloaded, total, speed)),
    )

    assert written == 11
    assert progress_updates
    assert out_path.endswith("video.mp4")
    assert (tmp_path / "video.mp4").read_bytes() == b"hello world"
    assert not list(tmp_path.glob("*.tmp"))


def test_tls_transport_cleans_up_temp_file_on_cancellation(monkeypatch, tmp_path):
    response = _FakeResponse([b"a", b"b", b"c"])
    _install_fake_curl(monkeypatch, response)
    checks = {"count": 0}

    def _cancel_check():
        checks["count"] += 1
        return checks["count"] >= 2

    with pytest.raises(TransportCancelled):
        download_direct_file(
            url="https://example.com/video.mp4",
            out_dir=str(tmp_path),
            headers=None,
            proxy="",
            impersonate="chrome",
            cancel_check=_cancel_check,
            on_progress=lambda *_args: None,
        )

    assert not list(tmp_path.iterdir())


def test_file_integrity_watcher_emits_missing_signal_for_tracked_output(tmp_path):
    _ensure_qt_app()
    watched_file = tmp_path / "complete.mp4"
    watched_file.write_bytes(b"data")
    watcher = FileIntegrityWatcher()
    missing_events = []
    watcher.file_missing.connect(lambda index, path: missing_events.append((index, path)))

    watcher.track_completed_file(7, str(watched_file))
    watched_file.unlink()
    watcher._queue_path_check(str(watched_file))
    watcher._flush_pending_checks()

    assert len(missing_events) == 1
    assert missing_events[0][0] == 7
    assert missing_events[0][1].endswith("complete.mp4")
