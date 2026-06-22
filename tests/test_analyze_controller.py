from types import SimpleNamespace

try:
    from PySide6.QtCore import QObject
    from PySide6.QtWidgets import QApplication
except ImportError:
    from PyQt6.QtCore import QObject
    from PyQt6.QtWidgets import QApplication

from core.window_controllers.analyze_controller import AnalyzeController


def _ensure_qt_app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _DummySignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)


class _DummyAnalyzeWorker:
    def __init__(self, *_args, **_kwargs):
        self.playlist_chunk = _DummySignal()
        self.finished = _DummySignal()
        self.started = False

    def start(self):
        self.started = True


class _DummyWindow(QObject):
    def __init__(self, playlist_view):
        super().__init__()
        self.playlist_view = playlist_view
        self.playlist_items = playlist_view.playlist_items
        self.analyze_worker = None
        self.cookies_path = ""
        self.search_history_ttl_days = 30
        self._search_history_limit = 20
        self.search_history = []
        self.search_history_model = SimpleNamespace(setStringList=lambda *_args, **_kwargs: None)
        self.search_view = SimpleNamespace(search_btn=SimpleNamespace(setText=lambda *_a, **_k: None, setEnabled=lambda *_a, **_k: None))
        self.logged_messages = []
        self.switched_views = []
        self.statuses = []
        self.warn_messages = []
        self.current_worker = object()

    def _update_search_spinner(self):
        return None

    def _force_reset_search_ui(self):
        return None

    def _append_log(self, message):
        self.logged_messages.append(message)

    def _switch_view(self, value):
        self.switched_views.append(value)

    def _set_status(self, value):
        self.statuses.append(value)

    def _warn(self, _message):
        self.warn_messages.append(_message)
        return None

    def _save_session(self):
        return None


def test_start_worker_preserves_existing_playlist_items_on_reanalyze(monkeypatch):
    _ensure_qt_app()
    preserved_flags = []
    existing_items = [{"id": "keep", "title": "Keep me"}]

    def _prepare_for_playlist_fetch(url, *, preserve_existing=False):
        preserved_flags.append((url, preserve_existing))
        return preserve_existing

    playlist_view = SimpleNamespace(
        playlist_items=existing_items,
        prepare_for_playlist_fetch=_prepare_for_playlist_fetch,
    )
    window = _DummyWindow(playlist_view)
    controller = AnalyzeController(window)

    monkeypatch.setattr(
        controller,
        "_prepare_playlist_diff_state",
        lambda _url: setattr(controller, "_playlist_cached_ids_before_fetch", {"keep"}),
    )
    monkeypatch.setattr(
        "core.window_controllers.analyze_controller.AnalyzeWorker",
        _DummyAnalyzeWorker,
    )
    monkeypatch.setattr(
        "core.window_controllers.analyze_controller.anti_detection_engine.get_yt_dlp_options",
        lambda: [],
    )

    controller.start_worker("https://example.com/playlist?list=abc", lambda *_args: None)

    assert preserved_flags == [("https://example.com/playlist?list=abc", True)]
    assert window.playlist_items is existing_items
    assert playlist_view.playlist_items is existing_items
    assert existing_items == [{"id": "keep", "title": "Keep me"}]
    assert isinstance(window.analyze_worker, _DummyAnalyzeWorker)
    assert window.analyze_worker.started is True


def test_start_worker_logs_playlist_backoff_hint(monkeypatch):
    _ensure_qt_app()
    playlist_view = SimpleNamespace(playlist_items=[])
    window = _DummyWindow(playlist_view)
    controller = AnalyzeController(window)

    monkeypatch.setattr(
        controller,
        "_prepare_playlist_diff_state",
        lambda _url: setattr(controller, "_playlist_cached_ids_before_fetch", set()),
    )
    monkeypatch.setattr(
        "core.window_controllers.analyze_controller.AnalyzeWorker",
        _DummyAnalyzeWorker,
    )
    monkeypatch.setattr(
        "core.window_controllers.analyze_controller._playlist_sync_service.get_backoff_info",
        lambda _url: {"is_active": True, "remaining_seconds": 90, "consecutive_failures": 2},
    )
    monkeypatch.setattr(
        "core.window_controllers.analyze_controller._playlist_sync_service.mark_sync_started",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "core.window_controllers.analyze_controller.anti_detection_engine.get_yt_dlp_options",
        lambda: [],
    )

    controller.start_worker("https://example.com/playlist?list=backoff", lambda *_args: None)

    assert any("backoff" in str(message).lower() for message in window.logged_messages)


def test_playlist_view_analyze_request_respects_backoff_when_cache_exists(monkeypatch):
    _ensure_qt_app()
    loading_states = []
    playlist_view = SimpleNamespace(
        playlist_items=[],
        set_loading_state=lambda value: loading_states.append(bool(value)),
    )
    window = _DummyWindow(playlist_view)
    window._on_playlist_analyze_finished = lambda *_args, **_kwargs: None
    controller = AnalyzeController(window)

    monkeypatch.setattr(
        "core.window_controllers.analyze_controller._playlist_sync_service.should_defer_sync",
        lambda _url, **_kwargs: {
            "should_defer": True,
            "is_active": True,
            "remaining_seconds": 45,
            "consecutive_failures": 3,
        },
    )
    monkeypatch.setattr(
        "core.window_controllers.analyze_controller._playlist_sync_service.get_known_ids",
        lambda _url: {"v1"},
    )
    monkeypatch.setattr(
        "core.window_controllers.analyze_controller.AnalyzeWorker",
        _DummyAnalyzeWorker,
    )

    controller.on_playlist_view_analyze_requested("https://example.com/playlist?list=blocked")

    assert window.analyze_worker is None
    assert loading_states == [False]
    assert window.warn_messages
    assert "مؤجلة" in str(window.warn_messages[-1])
    assert any("backoff" in str(message).lower() for message in window.logged_messages)


def test_playlist_view_force_analyze_bypasses_backoff(monkeypatch):
    _ensure_qt_app()
    loading_states = []
    playlist_view = SimpleNamespace(
        playlist_items=[],
        set_loading_state=lambda value: loading_states.append(bool(value)),
    )
    window = _DummyWindow(playlist_view)
    window._on_playlist_analyze_finished = lambda *_args, **_kwargs: None
    controller = AnalyzeController(window)

    monkeypatch.setattr(
        "core.window_controllers.analyze_controller._playlist_sync_service.should_defer_sync",
        lambda _url, **kwargs: {
            "should_defer": not bool(kwargs.get("force")),
            "is_active": True,
            "remaining_seconds": 60,
            "consecutive_failures": 2,
        },
    )
    monkeypatch.setattr(
        "core.window_controllers.analyze_controller._playlist_sync_service.get_known_ids",
        lambda _url: {"v1"},
    )
    monkeypatch.setattr(
        "core.window_controllers.analyze_controller.AnalyzeWorker",
        _DummyAnalyzeWorker,
    )
    monkeypatch.setattr(
        "core.window_controllers.analyze_controller._playlist_sync_service.get_backoff_info",
        lambda _url: {"is_active": True, "remaining_seconds": 60, "consecutive_failures": 2},
    )
    monkeypatch.setattr(
        "core.window_controllers.analyze_controller._playlist_sync_service.mark_sync_started",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "core.window_controllers.analyze_controller.anti_detection_engine.get_yt_dlp_options",
        lambda: [],
    )

    controller.on_playlist_view_analyze_requested("https://example.com/playlist?list=force", force=True)

    assert isinstance(window.analyze_worker, _DummyAnalyzeWorker)
    assert window.analyze_worker.started is True
    assert loading_states and loading_states[-1] is True
    assert any("تجاوز" in str(message) for message in window.logged_messages)


def test_apply_playlist_analysis_result_reuses_shared_playlist_reference(monkeypatch):
    _ensure_qt_app()
    finalize_calls = []
    removed_calls = []
    shared_items = [{"id": "keep", "title": "Keep me"}]

    def _remove_entries_by_ids(ids):
        removed_calls.append(set(ids))
        shared_items[:] = [item for item in shared_items if item["id"] not in ids]
        return 0

    playlist_view = SimpleNamespace(
        playlist_items=shared_items,
        remove_entries_by_ids=_remove_entries_by_ids,
        finalize_playlist_data=lambda payload, count, **kwargs: finalize_calls.append((payload, count, kwargs)),
        set_playlist_data=lambda *_args, **_kwargs: None,
    )
    window = _DummyWindow(playlist_view)
    controller = AnalyzeController(window)
    controller._playlist_reanalyze_mode = True

    monkeypatch.setattr(
        controller,
        "_finalize_playlist_diff_state",
        lambda: {"new_ids": set(), "removed_ids": {"gone"}},
    )
    monkeypatch.setattr(
        "core.window_controllers.analyze_controller.upsert_playlist_entries",
        lambda *_args, **_kwargs: None,
    )

    controller._apply_playlist_analysis_result({"kind": "playlist", "title": "Test"}, [])

    assert removed_calls == [{"gone"}]
    assert window.playlist_items is shared_items
    assert finalize_calls == [
        (
            {"kind": "playlist", "title": "Test"},
            1,
            {"new_entry_ids": set(), "removed_entry_ids": {"gone"}},
        )
    ]


def test_apply_playlist_analysis_result_filters_full_reanalyze_payload_to_new_items(monkeypatch):
    _ensure_qt_app()
    set_calls = []
    shared_items = [{"id": "keep", "title": "Existing"}]

    playlist_view = SimpleNamespace(
        playlist_items=shared_items,
        remove_entries_by_ids=lambda _ids: 0,
        finalize_playlist_data=lambda *_args, **_kwargs: None,
        set_playlist_data=lambda payload, items, **kwargs: set_calls.append((payload, list(items), kwargs)),
    )
    window = _DummyWindow(playlist_view)
    controller = AnalyzeController(window)
    controller._playlist_reanalyze_mode = True

    monkeypatch.setattr(
        controller,
        "_finalize_playlist_diff_state",
        lambda: {"new_ids": {"new1"}, "removed_ids": set()},
    )
    monkeypatch.setattr(
        "core.window_controllers.analyze_controller.upsert_playlist_entries",
        lambda *_args, **_kwargs: None,
    )

    controller._apply_playlist_analysis_result(
        {"kind": "playlist", "title": "Test"},
        [
            {"id": "keep", "title": "Existing"},
            {"id": "new1", "title": "Fresh"},
        ],
    )

    assert len(set_calls) == 1
    payload, items, kwargs = set_calls[0]
    assert payload == {"kind": "playlist", "title": "Test"}
    assert items == [{"id": "new1", "title": "Fresh"}]
    assert kwargs["new_entry_ids"] == {"new1"}
    assert kwargs["removed_entry_ids"] == set()
    assert kwargs["reset"] is False


def test_apply_playlist_analysis_result_uses_payload_ids_when_chunks_missing(monkeypatch):
    _ensure_qt_app()
    set_calls = []
    shared_items = []

    playlist_view = SimpleNamespace(
        playlist_items=shared_items,
        remove_entries_by_ids=lambda _ids: 0,
        finalize_playlist_data=lambda *_args, **_kwargs: None,
        set_playlist_data=lambda payload, items, **kwargs: set_calls.append((payload, list(items), kwargs)),
    )
    window = _DummyWindow(playlist_view)
    controller = AnalyzeController(window)
    controller._current_playlist_url = "https://example.com/playlist?list=abc"
    controller._playlist_cached_ids_before_fetch = {"keep", "gone"}
    controller._playlist_seen_ids_current_fetch = set()
    controller._playlist_new_ids_current_fetch = set()
    controller._playlist_reanalyze_mode = False

    seen_args = {}
    sync_args = {}

    def _fake_diff(url, current_ids):
        seen_args["url"] = url
        seen_args["ids"] = set(current_ids)
        return {
            "new_ids": {"new"},
            "removed_ids": {"gone"},
            "known_ids": {"keep"},
            "is_first_fetch": False,
        }

    def _fake_sync(url, current_ids):
        sync_args["url"] = url
        sync_args["ids"] = set(current_ids)
        return 1

    monkeypatch.setattr("core.window_controllers.analyze_controller.diff_playlist_entries", _fake_diff)
    monkeypatch.setattr("core.window_controllers.analyze_controller.sync_playlist_snapshot", _fake_sync)
    upsert_calls = []
    monkeypatch.setattr(
        "core.window_controllers.analyze_controller._playlist_sync_service.mark_sync_started",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "core.window_controllers.analyze_controller._playlist_sync_service.upsert_entries",
        lambda url, entries, **_kwargs: upsert_calls.append((url, len(entries))),
    )

    payload = {"kind": "playlist", "title": "No Chunks Playlist"}
    items = [
        {"id": "keep", "title": "Keep"},
        {"id": "new", "title": "New Item"},
    ]
    controller._apply_playlist_analysis_result(payload, items)

    assert seen_args["url"] == "https://example.com/playlist?list=abc"
    assert seen_args["ids"] == {"keep", "new"}
    assert sync_args["url"] == "https://example.com/playlist?list=abc"
    assert sync_args["ids"] == {"keep", "new"}
    assert len(set_calls) == 1
    _payload, captured_items, kwargs = set_calls[0]
    assert captured_items == items
    assert kwargs["new_entry_ids"] == {"new"}
    assert kwargs["removed_entry_ids"] == {"gone"}
    assert upsert_calls == [("https://example.com/playlist?list=abc", 2)]


def test_has_active_process_depends_on_analyze_worker_only():
    _ensure_qt_app()
    playlist_view = SimpleNamespace(playlist_items=[])
    window = _DummyWindow(playlist_view)
    controller = AnalyzeController(window)

    window.current_worker = object()
    window.analyze_worker = None
    assert controller.has_active_process() is False


def test_start_analyze_warns_when_analyze_worker_is_running():
    _ensure_qt_app()
    playlist_view = SimpleNamespace(playlist_items=[])
    window = _DummyWindow(playlist_view)
    window.search_view = SimpleNamespace(
        get_url=lambda: "https://example.com/watch?v=1",
        search_btn=SimpleNamespace(setText=lambda *_a, **_k: None, setEnabled=lambda *_a, **_k: None),
    )
    controller = AnalyzeController(window)

    window.analyze_worker = SimpleNamespace(isRunning=lambda: True)
    controller.start_analyze()

    assert window.warn_messages
    assert "قيد التنفيذ" in str(window.warn_messages[-1])


def test_start_analyze_rejects_truncated_youtube_id():
    _ensure_qt_app()
    playlist_view = SimpleNamespace(playlist_items=[])
    window = _DummyWindow(playlist_view)
    window.search_view = SimpleNamespace(
        get_url=lambda: "https://www.youtube.com/watch?v=SP0E1XXcyw",
        search_btn=SimpleNamespace(setText=lambda *_a, **_k: None, setEnabled=lambda *_a, **_k: None),
    )
    controller = AnalyzeController(window)

    controller.start_analyze()

    assert window.warn_messages
    assert "غير مكتمل" in str(window.warn_messages[-1])
    assert window.analyze_worker is None
