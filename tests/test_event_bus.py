from pathlib import Path

from core.error_handler import _write_crash_report


def test_write_crash_report_creates_file(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))

    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        _write_crash_report(RuntimeError, exc, exc.__traceback__, thread_name="TestThread")

    base = tmp_path / "VidDownloader" / "crash_reports"
    files = list(base.glob("crash-*.log"))
    assert files

    content = files[0].read_text(encoding="utf-8")
    assert "RuntimeError" in content
    assert "boom" in content
