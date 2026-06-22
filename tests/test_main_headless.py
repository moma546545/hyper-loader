import sys
import json
import io
from types import SimpleNamespace

import pytest

try:
    from PySide6.QtCore import QCoreApplication, QTimer
except ImportError:
    from PyQt6.QtCore import QCoreApplication, QTimer

import main
from core.event_bus import DownloadFinishedEvent, event_bus


def _ensure_core_app():
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication(["pytest-headless"])
    return app


class _DummySignal:
    def connect(self, _callback):
        return None


class _FakeWorker:
    def __init__(self, *, success: bool, message: str):
        self.success = bool(success)
        self.message = str(message)
        self.log = _DummySignal()
        self.state = _DummySignal()
        self.wait_calls = []
        self.worker_id = "headless_cli"

    def start(self):
        QTimer.singleShot(
            0,
            lambda: event_bus.publish(
                DownloadFinishedEvent(
                    self.worker_id,
                    self.success,
                    self.message,
                    {"file_path": "D:/downloads/sample.mp4"},
                )
            ),
        )

    def wait(self, timeout: int):
        self.wait_calls.append(int(timeout))


def test_main_gui_mode_delegates_to_app_main(monkeypatch):
    called = []
    monkeypatch.setitem(sys.modules, "app", SimpleNamespace(main=lambda: called.append(True) or 17))

    result = main.main([])

    assert result == 17
    assert called == [True]


def test_main_headless_requires_url_and_out_dir():
    with pytest.raises(SystemExit) as exc:
        main.main(["--headless"])

    assert exc.value.code == 2


def test_main_headless_accepts_url_file_without_direct_url(monkeypatch, tmp_path):
    _ensure_core_app()
    url_file = tmp_path / "urls.txt"
    url_file.write_text("https://example.com/a\nhttps://example.com/b\n", encoding="utf-8")
    captured_urls = []

    def _fake_run(args):
        captured_urls.append(str(args.url or ""))
        return 0

    monkeypatch.setattr(main, "_ensure_headless_logging", lambda: None)
    monkeypatch.setattr(main, "_run_headless_download", _fake_run)

    code = main.main(
        [
            "--headless",
            "--url-file",
            str(url_file),
            "--out-dir",
            "D:/downloads",
        ]
    )

    assert code == 0
    assert captured_urls == ["https://example.com/a", "https://example.com/b"]


def test_main_headless_url_file_missing_returns_argument_error():
    with pytest.raises(SystemExit) as exc:
        main.main(
            [
                "--headless",
                "--url-file",
                "D:/no/such/file.txt",
                "--out-dir",
                "D:/downloads",
            ]
        )

    assert exc.value.code == 2


@pytest.mark.parametrize(
    ("success", "expected_exit"),
    [
        (True, 0),
        (False, 1),
    ],
)
def test_main_headless_runs_worker_and_returns_completion_code(monkeypatch, success, expected_exit):
    _ensure_core_app()
    worker = _FakeWorker(success=success, message="completed" if success else "failed")
    monkeypatch.setattr(main, "_ensure_headless_logging", lambda: None)
    monkeypatch.setattr(main, "_build_headless_worker", lambda _args: worker)

    exit_code = main.main(
        [
            "--headless",
            "--url",
            "https://example.com/video",
            "--out-dir",
            "D:/downloads",
        ]
    )

    assert exit_code == expected_exit
    assert worker.wait_calls == [2000]


def test_main_headless_url_and_url_file_combines_both(monkeypatch, tmp_path):
    _ensure_core_app()
    url_file = tmp_path / "urls.txt"
    url_file.write_text("https://example.com/from-file\n", encoding="utf-8")
    captured_urls = []

    def _fake_run(args):
        captured_urls.append(str(args.url or ""))
        return 0

    monkeypatch.setattr(main, "_ensure_headless_logging", lambda: None)
    monkeypatch.setattr(main, "_run_headless_download", _fake_run)

    code = main.main(
        [
            "--headless",
            "--url",
            "https://example.com/direct",
            "--url-file",
            str(url_file),
            "--out-dir",
            "D:/downloads",
        ]
    )

    assert code == 0
    assert captured_urls == ["https://example.com/direct", "https://example.com/from-file"]


def test_main_headless_batch_fail_fast_stops_on_first_failure(monkeypatch, tmp_path):
    _ensure_core_app()
    url_file = tmp_path / "urls.txt"
    url_file.write_text("https://example.com/a\nhttps://example.com/b\n", encoding="utf-8")
    captured_urls = []

    def _fake_run(args):
        url = str(args.url or "")
        captured_urls.append(url)
        return 1 if url.endswith("/a") else 0

    monkeypatch.setattr(main, "_ensure_headless_logging", lambda: None)
    monkeypatch.setattr(main, "_run_headless_download", _fake_run)

    code = main.main(
        [
            "--headless",
            "--url-file",
            str(url_file),
            "--out-dir",
            "D:/downloads",
            "--fail-fast",
        ]
    )

    assert code == 1
    assert captured_urls == ["https://example.com/a"]


def test_main_headless_batch_report_json_is_written(monkeypatch, tmp_path):
    _ensure_core_app()
    url_file = tmp_path / "urls.txt"
    url_file.write_text("https://example.com/a\nhttps://example.com/b\n", encoding="utf-8")
    report_path = tmp_path / "report.json"

    def _fake_run(args):
        return 0 if str(args.url or "").endswith("/a") else 1

    monkeypatch.setattr(main, "_ensure_headless_logging", lambda: None)
    monkeypatch.setattr(main, "_run_headless_download", _fake_run)

    code = main.main(
        [
            "--headless",
            "--url-file",
            str(url_file),
            "--out-dir",
            "D:/downloads",
            "--report-json",
            str(report_path),
        ]
    )

    assert code == 1
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["mode"] == "headless_batch"
    assert payload["total_requested"] == 2
    assert payload["executed"] == 2
    assert payload["failures"] == 1
    assert len(payload["results"]) == 2


def test_main_headless_accepts_url_file_from_stdin(monkeypatch):
    _ensure_core_app()
    captured_urls = []

    def _fake_run(args):
        captured_urls.append(str(args.url or ""))
        return 0

    monkeypatch.setattr(main, "_ensure_headless_logging", lambda: None)
    monkeypatch.setattr(main, "_run_headless_download", _fake_run)
    monkeypatch.setattr(main.sys, "stdin", io.StringIO("https://example.com/a\n# comment\n\nhttps://example.com/b\n"))

    code = main.main(
        [
            "--headless",
            "--url-file",
            "-",
            "--out-dir",
            "D:/downloads",
        ]
    )

    assert code == 0
    assert captured_urls == ["https://example.com/a", "https://example.com/b"]


def test_main_headless_stdin_without_urls_returns_argument_error(monkeypatch):
    monkeypatch.setattr(main.sys, "stdin", io.StringIO(" \n# only comments\n"))

    with pytest.raises(SystemExit) as exc:
        main.main(
            [
                "--headless",
                "--url-file",
                "-",
                "--out-dir",
                "D:/downloads",
            ]
        )

    assert exc.value.code == 2
