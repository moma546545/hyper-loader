import time
from typing import List, Tuple

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    from PyQt6.QtWidgets import QApplication

from core.queue_manager import QueueManager


def _ensure_qt_app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_queue_manager_add_and_start_emits_signals():
    _ensure_qt_app()
    qm = QueueManager()

    started: List[bool] = []
    stopped: List[bool] = []
    requests: List[Tuple[dict, int]] = []

    qm.queue_started.connect(lambda: started.append(True))
    qm.queue_stopped.connect(lambda: stopped.append(True))
    qm.start_worker_requested.connect(lambda task, idx: requests.append((task, idx)))

    idx = qm.add_task({"url": "http://example.com/video", "scheduled_at": 0})
    assert idx == 0

    qm.start_queue()

    assert len(started) == 1
    assert len(requests) == 1
    task, task_idx = requests[0]
    assert task_idx == 0
    assert task.get("url") == "http://example.com/video"


def test_queue_manager_progress_updated_and_state():
    _ensure_qt_app()
    qm = QueueManager()

    events: List[Tuple[int, float, str, str]] = []
    qm.progress_updated.connect(
        lambda index, progress, speed, eta: events.append((index, progress, speed, eta))
    )

    idx = qm.add_task({"url": "http://example.com/file"})
    assert idx == 0

    qm.set_task_progress(idx, 42.5, "1.0 MiB/s", "00:10")

    assert len(events) == 1
    event_idx, progress, speed, eta = events[0]
    assert event_idx == idx
    assert progress == 42.5
    assert speed == "1.0 MiB/s"
    assert eta == "00:10"

    items = qm.get_queue_items()
    assert items[idx]["progress"] == 42.5
    assert items[idx]["speed"] == "1.0 MiB/s"
    assert items[idx]["eta"] == "00:10"


def test_queue_manager_skips_saturated_domain_and_starts_next_eligible_task():
    _ensure_qt_app()
    qm = QueueManager()

    requests: List[Tuple[dict, int]] = []
    qm.start_worker_requested.connect(lambda task, idx: requests.append((task, idx)))

    qm.add_task({"url": "https://youtu.be/active-1", "status": "running"})
    qm.add_task({"url": "https://youtu.be/active-2", "status": "processing"})
    qm.add_task({"url": "https://youtu.be/pending-one", "status": "pending"})
    qm.add_task({"url": "https://example.com/file", "status": "pending"})

    qm.update_task_fields(0, {"status": "running"}, emit_changed=False)
    qm.update_task_fields(1, {"status": "processing"}, emit_changed=False)
    qm.update_task_fields(2, {"status": "pending"}, emit_changed=False)
    qm.update_task_fields(3, {"status": "pending"}, emit_changed=False)

    qm.start_queue()

    assert len(requests) == 1
    task, idx = requests[0]
    assert idx == 3
    assert task.get("url") == "https://example.com/file"


def test_queue_manager_plan_parallel_start_centralizes_retry_schedule_and_domain_limits():
    _ensure_qt_app()
    qm = QueueManager()

    qm.add_task({"url": "https://youtu.be/active-1", "status": "running"})
    qm.add_task({"url": "https://youtu.be/active-2", "status": "processing"})
    qm.add_task({"url": "https://youtu.be/pending-one", "status": "pending"})
    qm.add_task({"url": "https://example.com/late", "status": "pending", "next_retry_at": time.time() + 60})
    qm.add_task({"url": "https://example.com/ready", "status": "pending"})

    plan = qm.plan_parallel_start(2, active_worker_ids={0, 1}, now_ts=time.time())

    assert plan["active_count"] == 2
    assert plan["pending_indices"] == [4]
    assert isinstance(plan["next_retry_ts"], float)
    assert plan["queue_items"][4]["url"] == "https://example.com/ready"


def test_queue_manager_restore_stale_running_tasks_only_resets_orphaned_items():
    _ensure_qt_app()
    qm = QueueManager()

    qm.add_task({"url": "https://example.com/one", "status": "running"})
    qm.add_task({"url": "https://example.com/two", "status": "running"})
    qm.add_task({"url": "https://example.com/three", "status": "pending"})

    restored = qm.restore_stale_running_tasks(active_worker_ids={1})
    items = qm.get_queue_items()

    assert restored == 1
    assert items[0]["status"] == "pending"
    assert items[0]["next_retry_at"] == 0
    assert items[1]["status"] == "running"
    assert items[2]["status"] == "pending"


def test_queue_manager_add_task_applies_model_defaults():
    _ensure_qt_app()
    qm = QueueManager()

    idx = qm.add_task({"url": "https://example.com/watch"})
    task = qm.get_task(idx)

    assert idx == 0
    assert task is not None
    assert task["url"] == "https://example.com/watch"
    assert task["status"] == "pending"
    assert task["format"] == "MP4"
    assert task["mode"] == "video"


def test_queue_manager_get_queue_items_returns_isolated_deep_copy():
    _ensure_qt_app()
    qm = QueueManager()

    idx = qm.add_task(
        {
            "url": "https://example.com/watch",
            "resume": {"partials_count": 1},
            "trims": [{"start": "00:01"}],
        }
    )
    copied_items = qm.get_queue_items()
    copied_items[idx]["status"] = "running"
    copied_items[idx]["resume"]["partials_count"] = 99
    copied_items[idx]["trims"][0]["start"] = "00:05"

    original = qm.get_task(idx)

    assert original is not None
    assert original["status"] == "pending"
    assert original["resume"]["partials_count"] == 1
    assert original["trims"][0]["start"] == "00:01"


def test_queue_manager_reassigns_duplicate_task_uuid_when_adding_tasks():
    _ensure_qt_app()
    qm = QueueManager()

    first_idx = qm.add_task({"task_uuid": "shared-task", "url": "https://example.com/one"})
    second_idx = qm.add_task({"task_uuid": "shared-task", "url": "https://example.com/two"})

    first = qm.get_task(first_idx)
    second = qm.get_task(second_idx)

    assert first is not None
    assert second is not None
    assert first["task_uuid"] == "shared-task"
    assert second["task_uuid"] != "shared-task"
    assert second["task_uuid"] != first["task_uuid"]


def test_queue_manager_scheduler_emits_due_signal_instead_of_starting_worker_immediately():
    _ensure_qt_app()
    qm = QueueManager()

    requests: List[Tuple[dict, int]] = []
    due_signals: List[bool] = []
    qm.start_worker_requested.connect(lambda task, idx: requests.append((task, idx)))
    qm.scheduled_tasks_due.connect(lambda: due_signals.append(True))

    qm.add_task({"url": "https://example.com/scheduled", "status": "pending", "scheduled_at": 1})
    qm._check_scheduled_tasks()

    assert due_signals == [True]
    assert requests == []


def test_queue_manager_process_next_uses_lightweight_plan_path(monkeypatch):
    _ensure_qt_app()
    qm = QueueManager()
    requests: List[Tuple[dict, int]] = []
    qm.start_worker_requested.connect(lambda task, idx: requests.append((task, idx)))
    qm.add_task({"url": "https://example.com/video", "status": "pending"})

    def _fake_plan(max_tasks, **kwargs):
        assert max_tasks == 1
        assert kwargs.get("include_queue_items") is False
        return {"pending_indices": [0], "status_counts": {}, "next_retry_ts": None}

    monkeypatch.setattr(qm, "plan_parallel_start", _fake_plan)
    qm.start_queue()

    assert len(requests) == 1
    assert requests[0][1] == 0


def test_queue_manager_dispatch_parallel_ready_tasks_bumps_change_token_and_marks_running():
    _ensure_qt_app()
    qm = QueueManager()
    qm.add_task({"url": "https://example.com/video", "status": "pending"})
    before = qm.get_change_token()

    plan = qm.dispatch_parallel_ready_tasks(1, now_ts=time.time(), include_queue_items=False)
    task = qm.get_task(0)

    assert plan["dispatched_indices"] == [0]
    assert task is not None
    assert task["status"] == "running"
    assert qm.get_change_token() > before


def test_queue_manager_dispatch_parallel_ready_tasks_invalidates_db_page_cache():
    _ensure_qt_app()
    qm = QueueManager()
    qm.add_task({"url": "https://example.com/video", "status": "pending"})
    qm._db_page_cache[("queued", "all", "", 1, 50, 0)] = (time.monotonic() + 10.0, {"entries": []})

    qm.dispatch_parallel_ready_tasks(1, now_ts=time.time(), include_queue_items=False)

    assert qm._db_page_cache == {}


def test_queue_manager_scheduled_hint_skips_full_scan_until_due(monkeypatch):
    _ensure_qt_app()
    qm = QueueManager()
    due_signals: List[bool] = []
    qm.scheduled_tasks_due.connect(lambda: due_signals.append(True))
    qm.add_task({"url": "https://example.com/scheduled", "status": "pending", "scheduled_at": 1000})
    qm._next_scheduled_at_hint = 1000.0

    class _ExplodingIterable:
        def __iter__(self):
            raise AssertionError("should not iterate queue items when hint says not due")

    qm.items = _ExplodingIterable()
    monkeypatch.setattr("core.queue_manager.time.time", lambda: 100.0)
    qm._check_scheduled_tasks()

    assert due_signals == []


def test_queue_manager_get_download_entries_page_uses_sqlite_for_large_queue(monkeypatch):
    _ensure_qt_app()
    qm = QueueManager()
    qm.DB_PAGE_THRESHOLD = 1
    qm.add_task({"task_uuid": "task-1", "url": "https://example.com/a", "status": "pending"})
    captured = {}

    def _fake_db_page(**kwargs):
        captured.update(kwargs)
        return {
            "entries": [{"task_uuid": "task-1", "queue_index": 0, "status": "pending"}],
            "total_matches": 1,
            "total_pages": 1,
            "page": 1,
            "page_size": 50,
        }

    monkeypatch.setattr("core.database.fetch_queue_entries_page_from_db", _fake_db_page)

    payload = qm.get_download_entries_page(
        view="queued",
        now_ts=time.time(),
        queue_state_filter="all",
        query="",
        page=1,
        page_size=50,
    )

    assert payload["entries"][0]["task_uuid"] == "task-1"
    assert captured["view"] == "queued"
    assert captured["page"] == 1
    assert captured["page_size"] == 50


def test_queue_manager_sqlite_paging_receives_query_and_state_filter(monkeypatch):
    _ensure_qt_app()
    qm = QueueManager()
    qm.DB_PAGE_THRESHOLD = 1
    qm.add_task({"task_uuid": "task-1", "url": "https://example.com/a", "status": "pending", "title": "Alpha"})
    captured = {}

    def _fake_db_page(**kwargs):
        captured.update(kwargs)
        return {"entries": [], "total_matches": 0, "total_pages": 1, "page": 1, "page_size": 20}

    monkeypatch.setattr("core.database.fetch_queue_entries_page_from_db", _fake_db_page)

    qm.get_download_entries_page(
        view="queued",
        now_ts=time.time(),
        queue_state_filter="pending",
        query="alpha",
        page=1,
        page_size=20,
    )

    assert captured["queue_state_filter"] == "pending"
    assert captured["query"] == "alpha"


def test_queue_manager_get_download_entries_page_filters_by_media_mode_and_reports_counts():
    _ensure_qt_app()
    qm = QueueManager()
    qm.add_task({"task_uuid": "v1", "url": "https://example.com/video", "status": "pending", "mode": "video"})
    qm.add_task({"task_uuid": "a1", "url": "https://example.com/audio", "status": "pending", "mode": "audio"})
    qm.add_task({"task_uuid": "v2", "url": "https://example.com/video-2", "status": "paused", "mode": "video"})

    payload = qm.get_download_entries_page(
        view="queued",
        now_ts=0,
        queue_state_filter="all",
        media_filter="audio",
        query="",
        page=1,
        page_size=20,
    )

    assert [entry["task_uuid"] for entry in payload["entries"]] == ["a1"]
    assert payload["media_counts"] == {"all": 3, "video": 2, "audio": 1}


def test_queue_manager_db_page_cache_reuses_recent_query_result(monkeypatch):
    _ensure_qt_app()
    qm = QueueManager()
    qm.DB_PAGE_THRESHOLD = 1
    qm.DB_PAGE_CACHE_TTL_SECONDS = 10.0
    qm.add_task({"task_uuid": "task-1", "url": "https://example.com/a", "status": "pending"})
    calls = {"count": 0}

    monkeypatch.setattr("core.queue_manager.time.monotonic", lambda: 100.0)

    def _fake_db_page(**_kwargs):
        calls["count"] += 1
        return {
            "entries": [{"task_uuid": "task-1", "queue_index": 0, "status": "pending"}],
            "total_matches": 1,
            "total_pages": 1,
            "page": 1,
            "page_size": 20,
        }

    monkeypatch.setattr("core.database.fetch_queue_entries_page_from_db", _fake_db_page)

    qm.get_download_entries_page(view="queued", now_ts=123.0, queue_state_filter="all", query="", page=1, page_size=20)
    qm.get_download_entries_page(view="queued", now_ts=123.0, queue_state_filter="all", query="", page=1, page_size=20)

    assert calls["count"] == 1


def test_queue_manager_db_page_cache_invalidates_after_progress_update(monkeypatch):
    _ensure_qt_app()
    qm = QueueManager()
    qm.DB_PAGE_THRESHOLD = 1
    qm.DB_PAGE_CACHE_TTL_SECONDS = 10.0
    qm.add_task({"task_uuid": "task-1", "url": "https://example.com/a", "status": "pending"})
    calls = {"count": 0}

    monkeypatch.setattr("core.queue_manager.time.monotonic", lambda: 200.0)

    def _fake_db_page(**_kwargs):
        calls["count"] += 1
        return {
            "entries": [{"task_uuid": "task-1", "queue_index": 0, "status": "pending"}],
            "total_matches": 1,
            "total_pages": 1,
            "page": 1,
            "page_size": 20,
        }

    monkeypatch.setattr("core.database.fetch_queue_entries_page_from_db", _fake_db_page)

    qm.get_download_entries_page(view="queued", now_ts=123.0, queue_state_filter="all", query="", page=1, page_size=20)
    qm.set_task_progress(0, 50.0, "1.0 MiB/s", "00:10")
    qm.get_download_entries_page(view="queued", now_ts=123.0, queue_state_filter="all", query="", page=1, page_size=20)

    assert calls["count"] == 2
