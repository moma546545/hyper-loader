import subprocess

import pytest

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    from PyQt6.QtWidgets import QApplication

from core import retry_utils
from core.workers import AnalyzeWorker, FormatProbeWorker, _CommandResult


def _ensure_qt_app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_analyze_worker_run_json_preserves_non_timeout_error(monkeypatch):
    _ensure_qt_app()
    monkeypatch.setattr(
        "core.workers.subprocess.Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("yt-dlp missing")),
    )
    worker = AnalyzeWorker("https://example.com/watch?v=1")

    result = worker._run_json(["python", "-m", "yt_dlp", "-J", worker.url], timeout_seconds=5)

    assert result.returncode == 1
    assert "yt-dlp missing" in result.stderr
    assert "مهلة" not in result.stderr


def test_analyze_worker_retries_rate_limit_then_succeeds(monkeypatch):
    _ensure_qt_app()
    worker = AnalyzeWorker("https://example.com/watch?v=1")
    sleeps = []
    attempts = {"count": 0}

    def _fake_run_json(command, timeout_seconds=45):
        attempts["count"] += 1
        if attempts["count"] < 3:
            return _CommandResult(1, "", "HTTP Error 429: Too Many Requests")
        return _CommandResult(0, '{"title":"Done"}', "")

    monkeypatch.setattr(worker, "_run_json", _fake_run_json)
    def _run_with_retries_probe(
        operation_name,
        action,
        retry_delays,
        *,
        should_retry_exception,
        logger,
        sleep_func=None,
        should_abort=None,
        abort_error_factory=None,
        sleep_quantum_seconds=0.1,
    ):
        return retry_utils.run_with_retries(
            operation_name,
            action,
            retry_delays,
            should_retry_exception=should_retry_exception,
            logger=logger,
            sleep_func=lambda seconds: sleeps.append(seconds),
            should_abort=should_abort,
            abort_error_factory=abort_error_factory,
            sleep_quantum_seconds=sleep_quantum_seconds,
        )
    monkeypatch.setattr("core.workers.run_with_retries", _run_with_retries_probe)

    result = worker._run_json_with_retries(["python", "-m", "yt_dlp"], timeout_seconds=45)

    assert attempts["count"] == 3
    assert sum(sleeps) == pytest.approx(3.0)
    assert sleeps
    assert result.returncode == 0
    assert '"title":"Done"' in result.stdout


def test_analyze_worker_interruption_aborts_retry_backoff(monkeypatch):
    _ensure_qt_app()
    worker = AnalyzeWorker("https://example.com/watch?v=1")
    attempts = {"count": 0}
    interrupted = {"value": False}
    sleeps = []

    def _fake_run_json(command, timeout_seconds=45):
        attempts["count"] += 1
        return _CommandResult(1, "", "HTTP Error 429: Too Many Requests")

    def _fake_sleep(seconds):
        sleeps.append(seconds)
        interrupted["value"] = True

    monkeypatch.setattr(worker, "_run_json", _fake_run_json)
    monkeypatch.setattr(worker, "isInterruptionRequested", lambda: interrupted["value"])
    monkeypatch.setattr("core.workers.time.sleep", _fake_sleep)

    result = worker._run_json_with_retries(["python", "-m", "yt_dlp"], timeout_seconds=45)

    assert attempts["count"] == 1
    assert result.returncode == 130
    assert result.stderr == "تم إلغاء تحليل الرابط."
    assert sum(sleeps) > 0


def test_format_probe_worker_timeout_is_reported_cleanly(monkeypatch):
    _ensure_qt_app()
    results = []
    worker = FormatProbeWorker("https://example.com/watch?v=1")
    worker.finished.connect(lambda ok, output, error: results.append((ok, output, error)))
    monkeypatch.setattr(
        worker,
        "_run_process",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("worker timed out")),
    )

    worker.run()

    assert results == [(False, "", "فحص الصيغ timed out")]


def test_analyze_worker_passes_cookies_and_only_safe_extra_args(monkeypatch):
    _ensure_qt_app()
    results = []
    captured = {}
    monkeypatch.setattr("core.workers._PreparedCookieFile.prepare", lambda self: "D:/secure/cookies.txt")
    monkeypatch.setattr("core.workers._PreparedCookieFile.cleanup", lambda self: None)
    worker = AnalyzeWorker(
        "https://example.com/watch?v=1",
        cookies_file="secret.cookies",
        extra_args=[
            "--user-agent", "TestAgent/1.0",
            "--add-header", "Accept-Language:en-US",
            "--proxy", "http://127.0.0.1:8080",
            "--extractor-args", "youtube:player_client=web,android",
            "--cookies", "forbidden.txt",
            "--add-header", "Cookie:bad=1",
        ],
    )

    def _fake_run_json_with_retries(command, timeout_seconds):
        captured["command"] = list(command)
        captured["timeout"] = timeout_seconds
        return _CommandResult(0, '{"title":"Done"}', "")

    monkeypatch.setattr(worker, "_run_single_incremental", lambda safe_cookies: None)
    monkeypatch.setattr(worker, "_run_json_with_retries", _fake_run_json_with_retries)
    worker.finished.connect(lambda ok, message, payload, items: results.append((ok, payload, items)))

    worker.run()

    command = captured["command"]
    assert results == [(
        True,
        {
            "kind": "single",
            "title": "Done",
            "channel": "--",
            "views": "--",
            "duration_seconds": 0,
            "thumbnail": "",
            "categories": [],
            "webpage_url": worker.url,
            "stream_url": "",
            "is_live": False,
            "was_live": False,
            "live_status": "",
            "video_id": "",
        },
        [],
    )]
    assert "--cookies" in command
    assert command.count("--cookies") == 1
    assert "D:/secure/cookies.txt" in command
    assert "--user-agent" in command
    assert "TestAgent/1.0" in command
    assert "--proxy" in command
    assert "http://127.0.0.1:8080" in command
    assert "--extractor-args" in command
    assert "youtube:player_client=web,android" in command
    assert "Accept-Language:en-US" in command
    assert "forbidden.txt" not in command
    assert "Cookie:bad=1" not in command
    assert captured["timeout"] == 60


def test_analyze_worker_streams_playlist_chunks_with_ytdlp_api(monkeypatch):
    _ensure_qt_app()
    chunk_events = []
    finished = []

    class _FakeYDL:
        def __init__(self, opts):
            self.opts = dict(opts)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, url, download=False):
            assert download is False
            assert url == "https://example.com/playlist?list=abc"
            return {
                "_type": "playlist",
                "title": "Big Playlist",
                "entries": (
                    {
                        "id": f"id{idx}",
                        "title": f"Video {idx}",
                        "duration": idx,
                        "is_live": idx == 1,
                        "was_live": idx == 2,
                        "live_status": "is_live" if idx == 1 else ("was_live" if idx == 2 else ""),
                    }
                    for idx in range(1, 206)
                ),
            }

    monkeypatch.setattr("core.workers.YoutubeDL", _FakeYDL)
    monkeypatch.setattr("core.workers._PreparedCookieFile.prepare", lambda self: "D:/secure/cookies.txt")
    monkeypatch.setattr("core.workers._PreparedCookieFile.cleanup", lambda self: None)

    worker = AnalyzeWorker("https://example.com/playlist?list=abc", cookies_file="secret.cookies")
    worker.playlist_chunk.connect(lambda payload, items: chunk_events.append((payload, list(items))))
    worker.finished.connect(lambda ok, message, payload, items: finished.append((ok, message, payload, items)))

    worker.run()

    assert [len(items) for _payload, items in chunk_events] == [100, 100, 5]
    assert all(chunk[0]["kind"] == "playlist" for chunk in chunk_events)
    assert chunk_events[0][1][0]["is_live"] is True
    assert chunk_events[0][1][0]["live_status"] == "is_live"
    assert chunk_events[0][1][1]["was_live"] is True
    assert chunk_events[0][1][1]["live_status"] == "was_live"
    assert finished == [(
        True,
        "تم تحليل 205 عنصر",
        {
            "kind": "playlist",
            "title": "Big Playlist",
            "url": "https://example.com/playlist?list=abc",
            "webpage_url": "https://example.com/playlist?list=abc",
        },
        [],
    )]


def test_analyze_worker_maps_plain_keyword_to_ytsearch():
    _ensure_qt_app()
    worker = AnalyzeWorker("lofi hip hop")
    assert worker.url == "ytsearch1:lofi hip hop"


def test_analyze_worker_keyword_search_returns_single_payload(monkeypatch):
    _ensure_qt_app()
    finished = []

    class _FakeYDL:
        def __init__(self, opts):
            self.opts = dict(opts)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, url, download=False):
            assert download is False
            assert url == "ytsearch1:best coding music"
            return {
                "_type": "playlist",
                "entries": [
                    {
                        "id": "abc123",
                        "title": "Best Coding Music",
                        "uploader": "Code Radio",
                        "duration": 321,
                        "thumbnail": "https://img.example/abc123.jpg",
                        "view_count": 1234,
                        "webpage_url": "https://www.youtube.com/watch?v=abc123",
                    }
                ],
            }

    monkeypatch.setattr("core.workers.YoutubeDL", _FakeYDL)
    monkeypatch.setattr("core.workers._PreparedCookieFile.prepare", lambda self: "")
    monkeypatch.setattr("core.workers._PreparedCookieFile.cleanup", lambda self: None)

    worker = AnalyzeWorker("best coding music")
    worker.finished.connect(lambda ok, message, payload, items: finished.append((ok, message, payload, items)))

    worker.run()

    assert finished == [(
        True,
        "تم تحليل الرابط بنجاح",
        {
            "kind": "single",
            "title": "Best Coding Music",
            "channel": "Code Radio",
            "views": "1,234",
            "duration_seconds": 321,
            "thumbnail": "https://img.example/abc123.jpg",
            "categories": [],
            "webpage_url": "https://www.youtube.com/watch?v=abc123",
            "stream_url": "",
            "is_live": False,
            "was_live": False,
            "live_status": "",
            "video_id": "abc123",
        },
        [],
    )]
