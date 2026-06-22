from PySide6.QtWidgets import QApplication, QPushButton

from ui.views.downloads_view import DownloadsView


def _ensure_qt_app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_downloads_view_filter_labels_match_filter_keys():
    _ensure_qt_app()
    view = DownloadsView()

    assert view.filter_buttons["completed"].text().strip() == "المكتمل"
    assert view.filter_buttons["active"].text().strip() == "نشط"
    assert view.filter_buttons["queued"].text().strip() == "في الانتظار"
    assert view.filter_buttons["scheduled"].text().strip() == "مجدول"


def test_downloads_view_export_csv_button_emits_once():
    _ensure_qt_app()
    view = DownloadsView()
    emissions = []
    view.export_csv_requested.connect(lambda: emissions.append(True))

    export_btn = next(
        btn
        for btn in view.findChildren(QPushButton)
        if "CSV" in str(btn.text() or "").upper()
    )

    export_btn.click()

    assert emissions == [True]
