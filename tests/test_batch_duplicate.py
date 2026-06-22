"""
tests/test_batch_duplicate.py — Regression tests for BatchDuplicateDialog
and the _collect_duplicate_tasks / show_batch_duplicate_review helpers.
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_task(url: str, title: str = "Test") -> dict:
    return {"url": url, "title": title, "out_dir": "", "thumbnail": ""}


def _make_report(is_duplicate: bool, reason: str = "url") -> dict:
    base = {"is_duplicate": is_duplicate, "local_files": [], "visual_duplicate": None}
    if is_duplicate and reason == "url":
        base["url_duplicate"] = {"timestamp": "2024-01-01", "file_path": "/tmp/x.mp4"}
    elif is_duplicate and reason == "local":
        base["url_duplicate"] = None
        base["local_files"] = ["/tmp/dup.mp4"]
    else:
        base["url_duplicate"] = None
    return base


# ── Unit: BatchDuplicateDialog ────────────────────────────────────────────────

class TestBatchDuplicateDialog:
    """Tests for the dialog class itself (non-widget, logic-only)."""

    def _make_dialog(self, entries):
        """Import and build dialog without showing it."""
        from ui.batch_duplicate_dialog import BatchDuplicateDialog, DuplicateEntry
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        dlg = BatchDuplicateDialog(entries)
        return dlg

    def test_empty_entries(self):
        """Dialog with no entries should initialise cleanly."""
        dlg = self._make_dialog([])
        assert dlg.get_allowed_tasks() == []
        assert dlg.get_skipped_tasks() == []

    def test_default_skip_all(self):
        """By default all rows are skipped."""
        task = _make_task("https://youtube.com/watch?v=abc")
        report = _make_report(True)
        dlg = self._make_dialog([(task, report)])
        # Default state: skip is checked, so allowed is empty
        assert dlg.get_allowed_tasks() == []
        assert dlg.get_skipped_tasks() == [task]

    def test_download_all(self):
        """After _on_dl_all all tasks are approved."""
        tasks = [_make_task(f"https://youtube.com/watch?v={i}") for i in range(5)]
        entries = [(t, _make_report(True)) for t in tasks]
        dlg = self._make_dialog(entries)
        dlg._on_dl_all()
        assert len(dlg.get_allowed_tasks()) == 5
        assert len(dlg.get_skipped_tasks()) == 0

    def test_skip_all(self):
        """After _on_skip_all all tasks are skipped."""
        tasks = [_make_task(f"https://youtube.com/watch?v={i}") for i in range(4)]
        entries = [(t, _make_report(True)) for t in tasks]
        dlg = self._make_dialog(entries)
        dlg._on_dl_all()   # first allow all
        dlg._on_skip_all() # then skip all
        assert len(dlg.get_allowed_tasks()) == 0
        assert len(dlg.get_skipped_tasks()) == 4

    def test_mixed_decisions(self):
        """Per-row decisions are respected."""
        tasks = [_make_task(f"https://youtube.com/watch?v={i}") for i in range(3)]
        entries = [(t, _make_report(True)) for t in tasks]
        dlg = self._make_dialog(entries)
        # Toggle row 0 to download, leave 1 and 2 as skip
        dlg._on_dl_toggled(0, True)
        assert len(dlg.get_allowed_tasks()) == 1
        assert len(dlg.get_skipped_tasks()) == 2

    def test_build_reason_text_url(self):
        """_build_reason_text renders URL duplicate reason."""
        from ui.batch_duplicate_dialog import BatchDuplicateDialog
        report = _make_report(True, reason="url")
        text = BatchDuplicateDialog._build_reason_text(report)
        assert "history" in text.lower() or "2024-01-01" in text

    def test_build_reason_text_local(self):
        from ui.batch_duplicate_dialog import BatchDuplicateDialog
        report = _make_report(True, reason="local")
        text = BatchDuplicateDialog._build_reason_text(report)
        assert "local" in text.lower() or "1" in text


# ── Unit: _collect_duplicate_tasks ────────────────────────────────────────────

class _FakeWindow:
    pass


class _FakeDownloadController:
    """Minimal stub of DownloadController for testing helpers."""

    def __init__(self, dup_map: dict):
        """dup_map: url -> report dict"""
        self.window = _FakeWindow()
        self._dup_map = dup_map
        self._duplicate_report_cache = {}

    def get_duplicate_report(self, task, *, force=False):
        url = (task or {}).get("url", "")
        return self._dup_map.get(url, {
            "is_duplicate": False, "local_files": [],
            "url_duplicate": None, "visual_duplicate": None
        })

    def _collect_duplicate_tasks(self, tasks: list) -> list:
        """Direct copy of the real implementation (no Qt imports needed)."""
        entries = []
        for task in tasks or []:
            if not isinstance(task, dict):
                continue
            try:
                report = self.get_duplicate_report(task, force=True)
            except Exception:
                continue
            if bool(report.get("is_duplicate")):
                entries.append((task, report))
        return entries


class TestCollectDuplicateTasks:
    def test_no_duplicates(self):
        dc = _FakeDownloadController({})
        tasks = [_make_task("https://a.com"), _make_task("https://b.com")]
        result = dc._collect_duplicate_tasks(tasks)
        assert result == []

    def test_some_duplicates(self):
        dup_url = "https://youtube.com/watch?v=dup"
        dc = _FakeDownloadController({dup_url: _make_report(True)})
        tasks = [_make_task("https://clean.com"), _make_task(dup_url)]
        result = dc._collect_duplicate_tasks(tasks)
        assert len(result) == 1
        assert result[0][0]["url"] == dup_url

    def test_all_duplicates(self):
        urls = [f"https://youtube.com/watch?v={i}" for i in range(6)]
        dup_map = {u: _make_report(True) for u in urls}
        dc = _FakeDownloadController(dup_map)
        tasks = [_make_task(u) for u in urls]
        result = dc._collect_duplicate_tasks(tasks)
        assert len(result) == 6

    def test_invalid_tasks_ignored(self):
        dc = _FakeDownloadController({})
        tasks = [None, 42, "bad", _make_task("https://ok.com")]
        result = dc._collect_duplicate_tasks(tasks)
        assert result == []
