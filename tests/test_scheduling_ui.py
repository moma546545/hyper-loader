try:
    from PySide6.QtCore import QDateTime
    from PySide6.QtWidgets import QApplication
except ImportError:
    from PyQt6.QtCore import QDateTime
    from PyQt6.QtWidgets import QApplication

from ui.playlist_view import PlaylistView
from ui.views.search_view import SearchView



def _ensure_qt_app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_search_schedule_button_enables_professional_scheduler():
    _ensure_qt_app()
    view = SearchView()

    assert view.schedule_picker.get_schedule_settings()["scheduled_at"] == 0
    assert view.schedule_picker.details_widget.isHidden() is True
    view.schedule_btn.click()

    settings = view.schedule_picker.get_schedule_settings()
    assert settings["schedule_enabled"] is True
    assert settings["scheduled_at"] > 0
    assert view.schedule_picker.details_widget.isHidden() is False


def test_playlist_download_emits_schedule_for_selected_tasks():
    _ensure_qt_app()
    view = PlaylistView()
    captured = []
    view.downloadRequested.connect(captured.append)
    view.set_playlist_data(
        {"kind": "playlist", "title": "Scheduled"},
        [{"id": "v1", "title": "One", "url": "https://example.com/v1", "duration_seconds": 60}],
    )
    target = QDateTime.currentDateTime().addSecs(7200)
    assert view.schedule_picker.details_widget.isHidden() is True
    view.schedule_picker.set_schedule_enabled(True)
    view.schedule_picker.date_time_edit.setDateTime(target)
    view.schedule_picker.repeat_combo.setCurrentIndex(view.schedule_picker.repeat_combo.findData("weekly"))

    view._on_download_clicked()

    assert captured
    assert captured[0][0]["scheduled_at"] > 0
    assert captured[0][0]["schedule_repeat"] == "weekly"
