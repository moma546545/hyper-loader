try:
    from PySide6.QtCore import Qt
except ImportError:
    from PyQt6.QtCore import Qt

from ui.models import DownloadListModel, EMPTY_ROW_HEIGHT, NORMAL_ROW_HEIGHT, EMPTY_STATE_SENTINEL
from ui.themes import DEFAULT_THEME, THEMES, get_theme


def test_get_theme_returns_requested_theme_when_known():
    assert get_theme("Modern Dark") is THEMES["Modern Dark"]


def test_get_theme_falls_back_to_default_for_unknown_name():
    assert get_theme("does-not-exist") is THEMES[DEFAULT_THEME]


def test_download_list_model_uses_empty_state_row_height():
    model = DownloadListModel([dict(EMPTY_STATE_SENTINEL)])
    size = model.data(model.index(0, 0), role=Qt.ItemDataRole.SizeHintRole)
    assert size.height() == EMPTY_ROW_HEIGHT


def test_download_list_model_uses_normal_row_height_for_regular_items():
    model = DownloadListModel([{"title": "Sample", "url": "https://example.com"}])
    size = model.data(model.index(0, 0), role=Qt.ItemDataRole.SizeHintRole)
    assert size.height() == NORMAL_ROW_HEIGHT
