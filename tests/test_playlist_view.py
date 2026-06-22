try:
    from PySide6.QtWidgets import QApplication, QLabel
except ImportError:
    from PyQt6.QtWidgets import QApplication, QLabel

from ui.playlist_view import PlaylistView


def _ensure_qt_app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_playlist_view_windowed_render_keeps_widget_count_below_item_count():
    app = _ensure_qt_app()
    view = PlaylistView()
    view.resize(1200, 720)
    view.show()
    app.processEvents()

    payload = {"kind": "playlist", "title": "Stress Playlist"}
    items = [
        {
            "index": idx,
            "title": f"Video {idx}",
            "url": f"https://example.com/watch?v={idx}",
            "thumbnail": "",
            "duration_seconds": 120,
        }
        for idx in range(1, 251)
    ]

    view.set_playlist_data(payload, items)
    app.processEvents()

    assert len(view.playlist_items) == 250
    assert view.list_model.rowCount() == 250
    assert 0 < len(view.playlist_rows) < len(view.playlist_items)


def test_playlist_view_removal_keeps_shared_items_list_reference():
    app = _ensure_qt_app()
    view = PlaylistView()
    view.show()
    app.processEvents()

    payload = {"kind": "playlist", "title": "Shared Playlist"}
    items = [
        {"id": "one", "title": "Video 1", "url": "https://example.com/watch?v=1", "thumbnail": "", "duration_seconds": 60},
        {"id": "two", "title": "Video 2", "url": "https://example.com/watch?v=2", "thumbnail": "", "duration_seconds": 60},
    ]

    view.set_playlist_data(payload, items)
    shared_ref = view.playlist_items

    removed = view.remove_entries_by_ids({"one"})
    app.processEvents()

    assert removed == 1
    assert shared_ref is view.playlist_items
    assert len(shared_ref) == 1
    assert shared_ref[0]["id"] == "two"


def test_playlist_view_cached_metrics_follow_select_all_toggle():
    app = _ensure_qt_app()
    view = PlaylistView()
    view.show()
    app.processEvents()

    payload = {"kind": "playlist", "title": "Metrics Playlist"}
    items = [
        {"id": "one", "title": "Video 1", "url": "https://example.com/watch?v=1", "thumbnail": "", "duration_seconds": 120},
        {"id": "two", "title": "Video 2", "url": "https://example.com/watch?v=2", "thumbnail": "", "duration_seconds": 180},
    ]

    view.set_playlist_data(payload, items)
    app.processEvents()

    assert view._selected_item_count == 2
    assert view._selected_size_bytes == view._total_estimated_size_bytes

    view._toggle_select_all(False)
    app.processEvents()

    assert view._selected_item_count == 0
    assert view._selected_size_bytes == 0
    assert "0/2" in view.download_btn.text()


def test_playlist_view_global_quality_updates_total_size_for_non_visible_rows():
    app = _ensure_qt_app()
    view = PlaylistView()
    view.resize(1200, 720)
    view.show()
    app.processEvents()

    payload = {"kind": "playlist", "title": "Large Playlist"}
    items = [
        {
            "id": f"id{idx}",
            "title": f"Video {idx}",
            "url": f"https://example.com/watch?v={idx}",
            "thumbnail": "",
            "duration_seconds": 600,
        }
        for idx in range(1, 251)
    ]

    view.set_playlist_data(payload, items)
    app.processEvents()

    before = view._selected_size_bytes
    view.global_format.setCurrentText("MP4")
    view.global_quality.setCurrentText("360p")
    app.processEvents()
    low_quality_total = view._selected_size_bytes

    view.global_quality.setCurrentText("8K")
    app.processEvents()
    high_quality_total = view._selected_size_bytes

    assert before > 0
    assert low_quality_total > 0
    assert high_quality_total > low_quality_total


def test_playlist_view_download_selected_preserves_live_metadata():
    app = _ensure_qt_app()
    view = PlaylistView()
    view.show()
    app.processEvents()

    emitted = []
    view.downloadRequested.connect(lambda tasks: emitted.append(list(tasks)))

    payload = {"kind": "playlist", "title": "Live Playlist"}
    items = [
        {
            "id": "live1",
            "title": "Live Event",
            "url": "https://example.com/watch?v=live1",
            "thumbnail": "",
            "duration_seconds": 0,
            "is_live": True,
            "was_live": False,
            "live_status": "is_live",
        }
    ]

    view.set_playlist_data(payload, items)
    app.processEvents()
    view._on_download_clicked()

    assert len(emitted) == 1
    assert len(emitted[0]) == 1
    task = emitted[0][0]
    assert task["is_live"] is True
    assert task["was_live"] is False
    assert task["live_status"] == "is_live"


def test_playlist_view_deferred_append_loads_large_playlist_in_chunks(monkeypatch):
    app = _ensure_qt_app()
    view = PlaylistView()
    view.resize(1200, 720)
    view.show()
    app.processEvents()

    monkeypatch.setenv("SNAPDOWNLOADER_PLAYLIST_DEFER_THRESHOLD", "100")
    monkeypatch.setenv("SNAPDOWNLOADER_PLAYLIST_DEFER_CHUNK", "40")

    payload = {"kind": "playlist", "title": "Deferred Playlist"}
    items = [
        {
            "id": f"id{idx}",
            "index": idx,
            "title": f"Video {idx}",
            "url": f"https://example.com/watch?v={idx}",
            "thumbnail": "",
            "duration_seconds": 120,
        }
        for idx in range(1, 181)
    ]

    view.set_playlist_data(payload, items)

    for _ in range(20):
        app.processEvents()
        if len(view.playlist_items) == len(items):
            break

    assert len(view.playlist_items) == len(items)
    assert view.list_model.rowCount() == len(items)
    assert "180" in view.status_lbl.text()


def test_playlist_view_visible_refresh_respects_materialize_budget(monkeypatch):
    app = _ensure_qt_app()
    view = PlaylistView()
    view.resize(1200, 720)
    view.show()
    app.processEvents()

    monkeypatch.setenv("SNAPDOWNLOADER_PLAYLIST_VISIBLE_BUDGET", "5")

    payload = {"kind": "playlist", "title": "Budget Playlist"}
    items = [
        {
            "id": f"id{idx}",
            "index": idx,
            "title": f"Video {idx}",
            "url": f"https://example.com/watch?v={idx}",
            "thumbnail": "",
            "duration_seconds": 120,
        }
        for idx in range(1, 101)
    ]
    view.set_playlist_data(payload, items)
    app.processEvents()

    view._clear_row_widgets()
    monkeypatch.setattr(view, "_visible_row_range", lambda: (0, 49))
    view._refresh_visible_rows()

    assert len(view.playlist_rows) <= 5
    assert view._visible_refresh_timer.isActive() is True


def test_playlist_view_global_updates_do_not_require_full_metrics_recalc(monkeypatch):
    app = _ensure_qt_app()
    view = PlaylistView()
    view.resize(1200, 720)
    view.show()
    app.processEvents()

    payload = {"kind": "playlist", "title": "No Full Recalc"}
    items = [
        {
            "id": f"id{idx}",
            "index": idx,
            "title": f"Video {idx}",
            "url": f"https://example.com/watch?v={idx}",
            "thumbnail": "",
            "duration_seconds": 240,
        }
        for idx in range(1, 81)
    ]
    view.set_playlist_data(payload, items)
    app.processEvents()

    def _forbid_full_recalc():
        raise AssertionError("_recalculate_cached_metrics should not be called for global updates")

    monkeypatch.setattr(view, "_recalculate_cached_metrics", _forbid_full_recalc)

    view.global_format.setCurrentText("MP4")
    app.processEvents()
    before = view._selected_size_bytes
    view.global_quality.setCurrentText("360p")
    app.processEvents()

    assert before > 0
    assert view._selected_size_bytes > 0


def test_playlist_view_size_label_refresh_respects_budget(monkeypatch):
    app = _ensure_qt_app()
    view = PlaylistView()
    view.resize(1200, 720)
    view.show()
    app.processEvents()

    monkeypatch.setenv("SNAPDOWNLOADER_PLAYLIST_SIZE_LABEL_BUDGET", "3")
    payload = {"kind": "playlist", "title": "Label Budget"}
    items = [
        {
            "id": f"id{idx}",
            "index": idx,
            "title": f"Video {idx}",
            "url": f"https://example.com/watch?v={idx}",
            "thumbnail": "",
            "duration_seconds": 120,
        }
        for idx in range(1, 21)
    ]
    view.set_playlist_data(payload, items)
    app.processEvents()

    calls = []
    monkeypatch.setattr(view, "_refresh_row_size_label", lambda row_index: calls.append(row_index))
    view._pending_size_label_rows = set(range(10))
    view._flush_pending_row_size_labels()

    assert len(calls) == 4
    assert len(view._pending_size_label_rows) == 6
    assert view._size_label_refresh_timer.isActive() is True


def test_playlist_view_global_updates_do_not_depend_on_ensure_item_state(monkeypatch):
    app = _ensure_qt_app()
    view = PlaylistView()
    view.resize(1200, 720)
    view.show()
    app.processEvents()

    payload = {"kind": "playlist", "title": "Ensure-Free Global Updates"}
    items = [
        {
            "id": f"id{idx}",
            "index": idx,
            "title": f"Video {idx}",
            "url": f"https://example.com/watch?v={idx}",
            "thumbnail": "",
            "duration_seconds": 180,
        }
        for idx in range(1, 41)
    ]
    view.set_playlist_data(payload, items)
    app.processEvents()

    monkeypatch.setattr(
        view,
        "_ensure_item_state",
        lambda _item: (_ for _ in ()).throw(AssertionError("_ensure_item_state should not be used in global updates")),
    )

    view.global_format.setCurrentText("MP4")
    app.processEvents()
    view.global_quality.setCurrentText("360p")
    app.processEvents()

    assert view._selected_item_count == len(view.playlist_items)
    assert view._selected_size_bytes == view._total_estimated_size_bytes


def test_playlist_view_thumbnail_loading_deduplicates_inflight_requests():
    app = _ensure_qt_app()
    view = PlaylistView()
    view.show()
    app.processEvents()

    class _DummySignal:
        def __init__(self):
            self._callbacks = []

        def connect(self, cb):
            self._callbacks.append(cb)

        def emit(self):
            for cb in list(self._callbacks):
                cb()

    class _DummyReply:
        def __init__(self):
            self.finished = _DummySignal()

        def error(self):
            return 1

        def readAll(self):
            return b""

        def deleteLater(self):
            return None

    calls = []
    reply = _DummyReply()
    view.net_manager = type(
        "_DummyManager",
        (),
        {"get": lambda _self, _request: calls.append(True) or reply},
    )()

    label_a = QLabel()
    label_b = QLabel()
    thumb_url = "https://example.com/thumb.jpg"

    view._async_load_thumbnail(thumb_url, label_a)
    view._async_load_thumbnail(thumb_url, label_b)

    assert len(calls) == 1
    assert thumb_url in view._thumbnail_inflight
    assert len(view._thumbnail_waiters.get(thumb_url, [])) == 2

    reply.finished.emit()

    assert thumb_url not in view._thumbnail_inflight
    assert thumb_url not in view._thumbnail_waiters
