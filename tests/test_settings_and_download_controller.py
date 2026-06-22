import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    from PyQt6.QtWidgets import QApplication

from core.window_controllers.settings_controller import SettingsController
from core.window_controllers.download_controller import DownloadController
from core.window_bootstrap import _handle_queue_stopped
from ui.views.settings_view import SettingsView


def _ensure_qt_app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _DummyLineEdit:
    def __init__(self, value: str = ""):
        self._value = value

    def text(self) -> str:
        return self._value

    def setText(self, value: str):
        self._value = str(value)

    def blockSignals(self, _flag: bool):
        return None


class _DummyCheckBox:
    def __init__(self, checked: bool = False):
        self._checked = checked

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, checked: bool):
        self._checked = bool(checked)

    def blockSignals(self, _flag: bool):
        return None


class _DummyCombo:
    def __init__(self, text: str = "", data=None, items=None):
        values = list(items or [])
        if text and text not in values:
            values.append(text)
        if not values:
            values.append(str(text))
        self._items = [str(item) for item in values]
        self._index = max(0, self._items.index(str(text))) if str(text) in self._items else 0
        self._data = data if data is not None else self._items[self._index]

    def currentText(self) -> str:
        return self._items[self._index]

    def currentData(self):
        return self._data

    def findText(self, value: str) -> int:
        value = str(value)
        try:
            return self._items.index(value)
        except ValueError:
            return -1

    def setCurrentIndex(self, index: int):
        index = int(index)
        if 0 <= index < len(self._items):
            self._index = index
            self._data = self._items[index]

    def blockSignals(self, _flag: bool):
        return None


class _DummyProgressBar:
    def __init__(self):
        self.values = []
        self.statuses = []

    def setValue(self, value: int):
        self.values.append(int(value))

    def set_status(self, status: str):
        self.statuses.append(str(status))


class _DummySpinBox:
    def __init__(self, value: int = 0):
        self._value = int(value)

    def value(self) -> int:
        return self._value

    def setValue(self, value: int):
        self._value = int(value)

    def blockSignals(self, _flag: bool):
        return None


class _DummyContainer:
    def __init__(self):
        self.hidden = 0
        self.shown = 0

    def hide(self):
        self.hidden += 1

    def show(self):
        self.shown += 1


class _DummyQueueManager:
    def __init__(self, items=None):
        self.items = list(items or [])
        self._change_token = 0
        self.updated = []
        self.is_running = False
        self.is_paused = False
        self.start_calls = 0
        self.resume_calls = 0
        self.restore_calls = []

    def get_queue_items_snapshot(self):
        return [dict(item) for item in self.items]

    def get_item_count(self):
        return len(self.items)

    def get_dashboard_queue_counts(self):
        active = 0
        queued = 0
        for item in self.items:
            status = str(item.get("status", "pending") or "pending").lower()
            if status == "running":
                active += 1
            if status in {"pending", "paused", "queued", "waiting"}:
                queued += 1
        return {"active": active, "queued": queued}

    def get_change_token(self):
        return self._change_token

    def update_task_fields(self, index, fields, emit_changed=True):
        self.updated.append((index, dict(fields), emit_changed))
        if 0 <= index < len(self.items):
            self.items[index].update(fields)
            self._change_token += 1
            return True
        return False

    def get_task(self, index):
        if 0 <= index < len(self.items):
            return dict(self.items[index])
        return None

    def set_task_status(self, index, status):
        if 0 <= index < len(self.items):
            self.items[index]["status"] = status
            self._change_token += 1

    def set_runtime_state(self, *, is_running=None, is_paused=None):
        if is_running is not None:
            self.is_running = bool(is_running)
        if is_paused is not None:
            self.is_paused = bool(is_paused) if self.is_running else False

    def pause_queue(self):
        self.is_paused = True if self.is_running else self.is_paused

    def stop_queue(self):
        self.is_running = False
        self.is_paused = False

    def start_queue(self):
        self.start_calls += 1
        if self.items:
            self.is_running = True
            self.is_paused = False

    def resume_queue(self):
        self.resume_calls += 1
        if self.is_running:
            self.is_paused = False

    def restore_stale_running_tasks(self, active_worker_ids=None):
        normalized_active_ids = {
            int(idx)
            for idx in (active_worker_ids or set())
            if isinstance(idx, int) and int(idx) >= 0
        }
        self.restore_calls.append(set(normalized_active_ids))
        restored = 0
        for idx, item in enumerate(self.items):
            status = str(item.get("status", "pending") or "pending").lower()
            if status == "running" and idx not in normalized_active_ids:
                item["status"] = "pending"
                item["next_retry_at"] = 0
                restored += 1
        if restored:
            self._change_token += 1
        return restored

    def plan_parallel_start(self, max_tasks, *, active_worker_ids=None, priority="fifo", task_size_getter=None, now_ts=None, include_queue_items=True):
        now_value = float(now_ts or time.time())
        normalized_active_ids = {
            int(idx)
            for idx in (active_worker_ids or set())
            if isinstance(idx, int) and int(idx) >= 0
        }
        active_domains = {}
        for idx, item in enumerate(self.items):
            status = str(item.get("status", "pending") or "pending").lower()
            if idx not in normalized_active_ids and status not in {"downloading", "processing", "merging", "running"}:
                continue
            url = str(item.get("url", "") or "")
            domain = "youtube" if "youtu" in url else ("example.com" if "example.com" in url else "")
            if domain:
                active_domains[domain] = active_domains.get(domain, 0) + 1
        ready_indices = []
        next_retry_ts = None
        for idx, item in enumerate(self.items):
            if idx in normalized_active_ids:
                continue
            status = str(item.get("status", "pending") or "pending").lower()
            if status != "pending":
                continue
            scheduled_at = float(item.get("scheduled_at", 0) or 0)
            if scheduled_at > now_value:
                next_retry_ts = scheduled_at if next_retry_ts is None else min(next_retry_ts, scheduled_at)
                continue
            retry_at = float(item.get("next_retry_at", 0) or 0)
            if retry_at > now_value:
                next_retry_ts = retry_at if next_retry_ts is None else min(next_retry_ts, retry_at)
                continue
            ready_indices.append(idx)
        if priority == "smallest_first" and callable(task_size_getter):
            ready_indices.sort(key=lambda idx: (int(task_size_getter(dict(self.items[idx])) or 0), idx))
        pending_indices = []
        domain_usage = dict(active_domains)
        for idx in ready_indices:
            if len(pending_indices) >= max(0, int(max_tasks or 0)):
                break
            url = str(self.items[idx].get("url", "") or "")
            domain = "youtube" if "youtu" in url else ("example.com" if "example.com" in url else "")
            limit = 2 if domain == "youtube" else 4
            if domain and domain_usage.get(domain, 0) >= limit:
                continue
            pending_indices.append(idx)
            if domain:
                domain_usage[domain] = domain_usage.get(domain, 0) + 1
        result = {
            "pending_indices": pending_indices,
            "next_retry_ts": next_retry_ts,
            "active_count": len(normalized_active_ids),
        }
        if include_queue_items:
            result["queue_items"] = self.get_queue_items_snapshot()
        return result


class _DummyActiveWorker:
    def __init__(self, running=True, finished=False):
        self._running = running
        self._finished = finished

    def isRunning(self):
        return self._running

    def isFinished(self):
        return self._finished


def _make_settings_window(form_settings=None, queue_items=None):
    search_view = SimpleNamespace(
        out_dir_input=_DummyLineEdit("D:/downloads"),
        aria2_checkbox=_DummyCheckBox(True),
    )
    settings_view = SimpleNamespace(
        get_form_settings=lambda: dict(form_settings or {}),
    )
    return SimpleNamespace(
        queue_manager=_DummyQueueManager(queue_items or []),
        settings_view=settings_view,
        search_view=search_view,
        auto_retry_delay_seconds=7,
        queue_auto_retry_limit=3,
        max_concurrent=4,
        _search_history_limit=120,
        search_history_ttl_days=30,
        thumbnail_cache_max=200,
        storage_guard_enabled=True,
        storage_min_free_gb=5,
        cookies_path="D:/cookies.txt",
        theme="Modern Dark",
        trial_started_at="",
        trial_total_days=14,
        search_history=["https://example.com/watch?v=1"],
        ui_language="ar",
    )


def _make_download_window(queue_items=None):
    progress_bar = _DummyProgressBar()
    status_container = _DummyContainer()
    search_view = SimpleNamespace(
        out_dir_input=_DummyLineEdit("D:/fallback"),
        progress_bar=progress_bar,
        status_container=status_container,
    )
    window = SimpleNamespace(
        queue_running=True,
        queue_paused=False,
        max_concurrent=2,
        active_workers={},
        _active_workers_lock=threading.RLock(),
        queue_manager=_DummyQueueManager(queue_items or []),
        search_view=search_view,
        settings_view=SimpleNamespace(get_form_settings=lambda: {}),
        downloads_filter="active",
        statuses=[],
        logs=[],
        warnings=[],
        infos=[],
        refresh_calls=0,
        pruned=0,
        saved=0,
        started_workers=[],
        cookies_path="D:/cookies.txt",
        bandwidth_scheduler_enabled=False,
        _display_progress_wid=None,
        auto_retry_delay_seconds=7,
        current_worker=None,
        progress_size="--",
        _speed_history={},
        cancel_requested_workers=set(),
        bandwidth_restart_requested_workers=set(),
        pause_requested_workers=set(),
    )
    window.queue_manager.is_running = True
    window.queue_manager.is_paused = False

    def _set_status(value):
        window.statuses.append(str(value))

    def _warn(value):
        window.warnings.append(str(value))

    def _info(value):
        window.infos.append(str(value))

    def _append_log(value):
        window.logs.append(str(value))

    def _refresh():
        window.refresh_calls += 1

    def _prune():
        window.pruned += 1

    def _save():
        window.saved += 1

    window._set_status = _set_status
    window._append_log = _append_log
    window._warn = _warn
    window._info = _info
    window._refresh_downloads_list = _refresh
    window._prune_inactive_workers = _prune
    window._save_session = _save
    window._active_workers_count = lambda: 0
    window._current_bandwidth_limit_kbps = lambda: 0
    window._on_download_progress = lambda wid, progress, speed, eta: None
    window._on_download_log = lambda line: None
    window._on_download_state = lambda state: None
    window._on_worker_thread_finished = lambda wid, worker: None
    return window


def test_settings_controller_omits_redundant_cookie_path_when_profile_matches(monkeypatch):
    _ensure_qt_app()
    cookie_path = "D:/profiles/work/cookies.txt"
    window = _make_settings_window(
        form_settings={
            "cookies_path": cookie_path,
            "cookie_profile_name": "work",
        }
    )
    controller = SettingsController(window)
    monkeypatch.setattr(controller.cookie_profiles, "get_profile_path", lambda name: cookie_path)

    payload = controller.build_settings_payload()

    assert payload["cookie_profile_name"] == "work"
    assert payload["cookies_path"] == ""


def test_settings_controller_keeps_cookie_path_when_profile_points_elsewhere(monkeypatch):
    _ensure_qt_app()
    cookie_path = "D:/profiles/work/override.txt"
    window = _make_settings_window(
        form_settings={
            "cookies_path": cookie_path,
            "cookie_profile_name": "work",
        }
    )
    controller = SettingsController(window)
    monkeypatch.setattr(controller.cookie_profiles, "get_profile_path", lambda name: "D:/profiles/work/cookies.txt")

    payload = controller.build_settings_payload()

    assert payload["cookie_profile_name"] == "work"
    assert controller._resolve_cookies_path(payload["cookies_path"]) == cookie_path
    if os.name == "nt":
        assert payload["cookies_path"].startswith("protected://")
        assert cookie_path not in payload["cookies_path"]
    else:
        assert payload["cookies_path"] == cookie_path


def test_settings_view_round_trips_browser_cookie_and_advanced_processing_toggles():
    _ensure_qt_app()
    view = SimpleNamespace(
        settings_out_dir=_DummyLineEdit("D:/downloads"),
        settings_retries=_DummySpinBox(3),
        settings_retry_delay=_DummySpinBox(5),
        settings_queue_retry_limit=_DummySpinBox(1),
        settings_concurrent=_DummySpinBox(2),
        settings_search_history_limit=_DummySpinBox(40),
        settings_search_history_ttl_days=_DummySpinBox(30),
        settings_thumbnail_cache_max=_DummySpinBox(200),
        settings_storage_guard=_DummyCheckBox(True),
        settings_storage_min_free_gb=_DummySpinBox(5),
        settings_system_theme_sync=_DummyCheckBox(True),
        settings_bandwidth_scheduler=_DummyCheckBox(False),
        settings_cookies=_DummyLineEdit(""),
        settings_cookie_profile_combo=_DummyCombo("Default (no profile)", items=["Default (no profile)", "work"]),
        settings_cookies_from_browser=_DummyCombo("edge", items=["none", "chrome", "firefox", "edge"]),
        settings_post_download_script=_DummyLineEdit(""),
        settings_embed_subs=_DummyCheckBox(True),
        settings_whisper_fallback_enabled=_DummyCheckBox(True),
        settings_split_chapters=_DummyCheckBox(False),
        settings_verify_checksum=_DummyCheckBox(False),
        settings_virus_scan_after_download=_DummyCheckBox(False),
        settings_sponsorblock_enabled=_DummyCheckBox(True),
        settings_normalize_audio_postprocess=_DummyCheckBox(True),
        settings_auto_categorize_downloads=_DummyCheckBox(True),
        settings_auto_categorize_mode=_DummyCombo("mode_then_extension", items=["mode", "extension", "mode_then_extension"]),
        settings_rename_template=_DummyCombo("Default", items=["Default"]),
        settings_use_ytdlp_api=_DummyCheckBox(False),
        settings_use_native_engine=_DummyCheckBox(True),
        settings_aria2=_DummyCheckBox(True),
        proxy_enabled_cb=_DummyCheckBox(False),
        proxy_input=_DummyLineEdit(""),
        sustainability_combo=_DummyCombo("notify", items=["notify"]),
        sustainability_spin=_DummySpinBox(60),
        settings_language_combo=SimpleNamespace(currentData=lambda: "en"),
        merge_group=_DummyCheckBox(False),
        settings_video_codec=_DummyCombo("copy", items=["copy"]),
        settings_video_crf=_DummySpinBox(23),
        settings_audio_codec=_DummyCombo("aac", items=["aac"]),
        settings_audio_bitrate=_DummyLineEdit("192k"),
        settings_merge_hw_encoder=_DummyCombo("auto", items=["off", "auto", "nvenc", "qsv", "amf"]),
        settings_merge_force_reencode=_DummyCheckBox(True),
        settings_merge_video_preset=_DummyCombo("p6", items=["p5", "p6", "p7"]),
    )

    payload = SettingsView.get_form_settings(view)

    assert payload["cookies_from_browser"] == "edge"
    assert payload["whisper_fallback"] is True
    assert payload["sponsorblock_enabled"] is True
    assert payload["normalize_audio_postprocess"] is True
    assert payload["auto_categorize_downloads"] is True
    assert payload["auto_categorize_mode"] == "mode_then_extension"
    assert payload["custom_merge_hw_encoder"] == "auto"
    assert payload["custom_merge_force_reencode"] is True
    assert payload["custom_merge_video_preset"] == "p6"

    SettingsView.apply_form_settings(
        view,
        {
            "cookies_from_browser": "chrome",
            "whisper_fallback": False,
            "sponsorblock_enabled": False,
            "normalize_audio_postprocess": False,
            "auto_categorize_downloads": False,
            "auto_categorize_mode": "extension",
            "custom_merge_hw_encoder": "nvenc",
            "custom_merge_force_reencode": False,
            "custom_merge_video_preset": "p7",
        },
    )

    assert view.settings_cookies_from_browser.currentText() == "chrome"
    assert view.settings_whisper_fallback_enabled.isChecked() is False
    assert view.settings_sponsorblock_enabled.isChecked() is False
    assert view.settings_normalize_audio_postprocess.isChecked() is False
    assert view.settings_auto_categorize_downloads.isChecked() is False
    assert view.settings_auto_categorize_mode.currentText() == "extension"
    assert view.settings_merge_hw_encoder.currentText() == "nvenc"
    assert view.settings_merge_force_reencode.isChecked() is False
    assert view.settings_merge_video_preset.currentText() == "p7"


def test_settings_controller_build_settings_payload_persists_advanced_download_flags():
    _ensure_qt_app()
    window = _make_settings_window(
        form_settings={
            "cookies_from_browser": "firefox",
            "whisper_fallback": True,
            "sponsorblock_enabled": True,
            "normalize_audio_postprocess": True,
            "auto_categorize_downloads": True,
            "auto_categorize_mode": "mode",
            "custom_merge_hw_encoder": "auto",
            "custom_merge_force_reencode": True,
            "custom_merge_video_preset": "p6",
        }
    )
    controller = SettingsController(window)

    payload = controller.build_settings_payload()

    assert payload["cookies_from_browser"] == "firefox"
    assert payload["whisper_fallback"] is True
    assert payload["sponsorblock_enabled"] is True
    assert payload["normalize_audio_postprocess"] is True
    assert payload["auto_categorize_downloads"] is True
    assert payload["auto_categorize_mode"] == "mode"
    assert payload["custom_merge_hw_encoder"] == "auto"
    assert payload["custom_merge_force_reencode"] is True
    assert payload["custom_merge_video_preset"] == "p6"


def test_download_controller_process_parallel_queue_schedules_retry_when_waiting(monkeypatch):
    _ensure_qt_app()
    future_ts = time.time() + 30
    window = _make_download_window(
        queue_items=[
            {"url": "https://example.com/file", "status": "pending", "next_retry_at": future_ts},
        ]
    )
    controller = DownloadController(window)
    scheduled = []
    monkeypatch.setattr(
        "core.window_controllers.download_controller.QTimer.singleShot",
        lambda delay_ms, callback: scheduled.append((delay_ms, callback)),
    )

    controller.process_parallel_queue()

    assert len(scheduled) == 1
    delay_ms, callback = scheduled[0]
    assert delay_ms >= 300
    assert callback == controller.process_parallel_queue
    assert window.statuses[-1] == "بانتظار إعادة المحاولة"
    assert window.refresh_calls == 1


def test_download_controller_process_parallel_queue_marks_missing_url_failed():
    _ensure_qt_app()
    window = _make_download_window(queue_items=[{"status": "pending"}])
    controller = DownloadController(window)
    started = []
    controller.start_download_worker = lambda task, worker_id=None: started.append((task, worker_id))

    controller.process_parallel_queue()

    assert started == []
    assert window.queue_manager.updated == [(0, {"status": "failed", "error_msg": "Missing URL"}, True)]
    assert window.refresh_calls == 1


def test_download_controller_process_parallel_queue_respects_domain_throttling():
    _ensure_qt_app()
    window = _make_download_window(
        queue_items=[
            {"url": "https://youtu.be/active-1", "status": "running"},
            {"url": "https://www.youtube.com/watch?v=active-2", "status": "processing"},
            {"url": "https://www.youtube.com/watch?v=pending-youtube", "status": "pending"},
            {"url": "https://example.com/file", "status": "pending"},
        ]
    )
    window.max_concurrent = 3
    window.active_workers = {
        0: _DummyActiveWorker(),
        1: _DummyActiveWorker(),
    }
    controller = DownloadController(window)
    started = []
    controller.start_download_worker = lambda task, worker_id=None: started.append((task.get("url"), worker_id))

    controller.process_parallel_queue()

    assert started == [("https://example.com/file", 3)]


def test_download_controller_process_parallel_queue_uses_queue_manager_running_state_when_window_flag_is_stale():
    _ensure_qt_app()
    window = _make_download_window(
        queue_items=[
            {"url": "https://example.com/file", "status": "pending", "out_dir": "D:/target"},
        ]
    )
    window.queue_running = False
    window.queue_manager.is_running = True
    controller = DownloadController(window)
    started = []
    controller.start_download_worker = lambda task, worker_id=None: started.append((task.get("url"), worker_id))

    controller.process_parallel_queue()

    assert started == [("https://example.com/file", 0)]


def test_download_controller_process_parallel_queue_uses_queue_manager_running_as_source_of_truth():
    _ensure_qt_app()
    window = _make_download_window(
        queue_items=[
            {"url": "https://example.com/file", "status": "pending", "out_dir": "D:/target"},
        ]
    )
    window.queue_running = True
    window.queue_manager.is_running = False
    controller = DownloadController(window)
    started = []
    controller.start_download_worker = lambda task, worker_id=None: started.append((task.get("url"), worker_id))

    controller.process_parallel_queue()

    assert started == []
    assert window.queue_running is False


def test_download_controller_queue_paused_uses_queue_manager_paused_as_source_of_truth():
    _ensure_qt_app()
    window = _make_download_window(queue_items=[{"url": "https://example.com/file", "status": "pending"}])
    window.queue_running = True
    window.queue_paused = True
    window.queue_manager.is_running = True
    window.queue_manager.is_paused = False
    controller = DownloadController(window)
    started = []
    controller.start_download_worker = lambda task, worker_id=None: started.append((task.get("url"), worker_id))

    controller.process_parallel_queue()

    assert started == [("https://example.com/file", 0)]
    assert window.queue_paused is False


def test_download_controller_pause_queue_download_syncs_runtime_state():
    _ensure_qt_app()
    window = _make_download_window()
    worker = SimpleNamespace(stop=lambda: None)
    window.active_workers = {"w1": worker}
    controller = DownloadController(window)
    window.queue_manager.is_running = True

    controller.pause_queue_download()

    assert window.queue_paused is True
    assert window.queue_manager.is_paused is True


def test_download_controller_start_queue_download_uses_queue_manager_start_and_restores_stale_tasks():
    _ensure_qt_app()
    window = _make_download_window(
        queue_items=[
            {"url": "https://example.com/a", "status": "running"},
            {"url": "https://example.com/b", "status": "pending"},
        ]
    )
    controller = DownloadController(window)
    window.queue_manager.is_running = False
    window.queue_manager.is_paused = False
    process_calls = []
    controller.process_parallel_queue = lambda: process_calls.append(True)
    window._switch_view = lambda value: setattr(window, "switched_to", value)
    window._set_downloads_filter = lambda value: setattr(window, "filter_set_to", value)

    controller.start_queue_download()

    assert window.queue_manager.restore_calls == [set()]
    assert window.queue_manager.items[0]["status"] == "pending"
    assert window.queue_manager.start_calls == 1
    assert window.queue_manager.resume_calls == 0
    assert window.queue_running is True
    assert window.queue_paused is False
    assert process_calls == [True]
    assert window.saved == 1


def test_download_controller_start_queue_download_applies_batch_duplicate_skip():
    _ensure_qt_app()
    window = _make_download_window(
        queue_items=[
            {"url": "https://example.com/dup", "status": "pending", "task_uuid": "dup-1"},
            {"url": "https://example.com/clean", "status": "pending", "task_uuid": "ok-1"},
        ]
    )
    controller = DownloadController(window)
    window.queue_manager.is_running = False
    window.queue_manager.is_paused = False
    process_calls = []
    controller.process_parallel_queue = lambda: process_calls.append(True)
    window._switch_view = lambda value: setattr(window, "switched_to", value)
    window._set_downloads_filter = lambda value: setattr(window, "filter_set_to", value)

    def _fake_review(tasks, *, allowed_url_set=None):
        if allowed_url_set is not None:
            allowed_url_set.add(tasks[1]["url"])
        return [tasks[1]], [tasks[0]]

    controller.show_batch_duplicate_review = _fake_review

    controller.start_queue_download()

    assert window.queue_manager.items[0]["status"] == "paused"
    assert window.queue_manager.items[1]["status"] == "pending"
    assert window.queue_manager.start_calls == 1
    assert process_calls == [True]
    assert controller._batch_duplicate_allowed_urls == {"https://example.com/clean"}
    assert any("Batch Review" in line for line in window.logs)


def test_download_controller_start_queue_download_stops_when_batch_review_cancelled():
    _ensure_qt_app()
    window = _make_download_window(
        queue_items=[
            {"url": "https://example.com/dup1", "status": "pending", "task_uuid": "d1"},
            {"url": "https://example.com/dup2", "status": "pending", "task_uuid": "d2"},
        ]
    )
    controller = DownloadController(window)
    process_calls = []
    controller.process_parallel_queue = lambda: process_calls.append(True)

    def _fake_review(tasks, *, allowed_url_set=None):
        controller._last_batch_duplicate_review_cancelled = True
        return [], list(tasks)

    controller.show_batch_duplicate_review = _fake_review

    controller.start_queue_download()

    assert window.queue_manager.start_calls == 0
    assert process_calls == []
    assert window.queue_running is False
    assert any("إلغاء" in line for line in window.logs)


def test_download_controller_start_queue_download_keeps_queue_running_for_waiting_retry(monkeypatch):
    _ensure_qt_app()

    class _RetryWaitingQueueManager(_DummyQueueManager):
        def start_queue(self):
            self.start_calls += 1
            self.is_running = False
            self.is_paused = False

    future_ts = time.time() + 30
    window = _make_download_window(
        queue_items=[
            {"url": "https://example.com/retry", "status": "pending", "next_retry_at": future_ts},
        ]
    )
    window.queue_manager = _RetryWaitingQueueManager(window.queue_manager.items)
    window.queue_manager.is_running = False
    window.queue_manager.is_paused = False
    window._switch_view = lambda value: setattr(window, "switched_to", value)
    window._set_downloads_filter = lambda value: setattr(window, "filter_set_to", value)
    controller = DownloadController(window)
    scheduled = []
    monkeypatch.setattr(
        "core.window_controllers.download_controller.QTimer.singleShot",
        lambda delay_ms, callback: scheduled.append((delay_ms, callback)),
    )

    controller.start_queue_download()

    assert window.queue_manager.start_calls == 1
    assert window.queue_running is True
    assert window.queue_manager.is_running is True
    assert window.statuses[-1] == "بانتظار إعادة المحاولة"
    assert window.refresh_calls == 1
    assert any(callback == controller.process_parallel_queue for _delay, callback in scheduled)


def test_download_controller_resume_queue_download_prefers_queue_manager_resume():
    _ensure_qt_app()
    window = _make_download_window(queue_items=[{"url": "https://example.com/a", "status": "paused"}])
    window.queue_manager.is_running = True
    window.queue_manager.is_paused = True
    controller = DownloadController(window)
    process_calls = []
    controller.process_parallel_queue = lambda: process_calls.append(True)

    controller.resume_queue_download()

    assert window.queue_manager.start_calls == 0
    assert window.queue_manager.resume_calls == 1
    assert window.queue_running is True
    assert window.queue_paused is False
    assert process_calls == [True]


def test_download_controller_resume_queue_download_keeps_queue_running_for_waiting_retry(monkeypatch):
    _ensure_qt_app()

    class _RetryWaitingQueueManager(_DummyQueueManager):
        def resume_queue(self):
            self.resume_calls += 1
            self.is_running = False
            self.is_paused = False

    future_ts = time.time() + 30
    window = _make_download_window(
        queue_items=[
            {"url": "https://example.com/retry", "status": "pending", "next_retry_at": future_ts},
        ]
    )
    window.queue_manager = _RetryWaitingQueueManager(window.queue_manager.items)
    window.queue_manager.is_running = True
    window.queue_manager.is_paused = True
    controller = DownloadController(window)
    scheduled = []
    monkeypatch.setattr(
        "core.window_controllers.download_controller.QTimer.singleShot",
        lambda delay_ms, callback: scheduled.append((delay_ms, callback)),
    )

    controller.resume_queue_download()

    assert window.queue_manager.resume_calls == 1
    assert window.queue_running is True
    assert window.queue_manager.is_running is True
    assert window.queue_paused is False
    assert window.statuses[-1] == "بانتظار إعادة المحاولة"
    assert any(callback == controller.process_parallel_queue for _delay, callback in scheduled)


def test_window_bootstrap_queue_stopped_keeps_non_runnable_status():
    _ensure_qt_app()
    window = _make_download_window(
        queue_items=[
            {"url": "https://example.com/a", "status": "paused"},
            {"url": "https://example.com/b", "status": "failed"},
        ]
    )
    enabled_calls = []
    window._set_controls_enabled = lambda value: enabled_calls.append(bool(value))

    _handle_queue_stopped(window)

    assert enabled_calls == [True]
    assert window.statuses[-1] == "لا توجد عناصر قابلة للتشغيل"


def test_window_bootstrap_queue_stopped_keeps_ready_when_pending_items_remain():
    _ensure_qt_app()
    window = _make_download_window(
        queue_items=[
            {"url": "https://example.com/a", "status": "pending"},
        ]
    )
    enabled_calls = []
    window._set_controls_enabled = lambda value: enabled_calls.append(bool(value))

    _handle_queue_stopped(window)

    assert enabled_calls == [True]
    assert window.statuses[-1] in {"Ready", "جاهز"}


def test_download_controller_handle_download_finished_keeps_non_runnable_status(monkeypatch):
    _ensure_qt_app()
    window = _make_download_window(
        queue_items=[
            {"url": "https://example.com/file", "status": "running", "title": "Clip"},
        ]
    )
    window.queue_manager.is_running = False
    window.error_dashboard = SimpleNamespace(report_error=lambda **kwargs: None)
    window._set_controls_enabled = lambda value: window.statuses.append(f"controls:{bool(value)}")
    window._show_tray_message = lambda *args, **kwargs: None
    controller = DownloadController(window)
    monkeypatch.setattr(controller, "append_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(controller, "_flush_progress_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "core.window_controllers.download_controller.QTimer.singleShot",
        lambda *args, **kwargs: None,
    )

    event = SimpleNamespace(
        worker_id=0,
        success=False,
        message="cancelled",
        data={"error": "cancelled"},
    )

    controller.handle_download_finished_event(event)

    assert "لا توجد عناصر قابلة للتشغيل" in window.statuses


def test_download_controller_handle_download_finished_keeps_ready_when_pending_items_remain(monkeypatch):
    _ensure_qt_app()
    window = _make_download_window(
        queue_items=[
            {"url": "https://example.com/file", "status": "running", "title": "Clip"},
            {"url": "https://example.com/next", "status": "pending", "title": "Next"},
        ]
    )
    window.queue_manager.is_running = False
    window.error_dashboard = SimpleNamespace(report_error=lambda **kwargs: None)
    window._set_controls_enabled = lambda value: window.statuses.append(f"controls:{bool(value)}")
    window._show_tray_message = lambda *args, **kwargs: None
    controller = DownloadController(window)
    monkeypatch.setattr(controller, "append_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(controller, "_flush_progress_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "core.window_controllers.download_controller.QTimer.singleShot",
        lambda *args, **kwargs: None,
    )

    event = SimpleNamespace(
        worker_id=0,
        success=False,
        message="cancelled",
        data={"error": "cancelled"},
    )

    controller.handle_download_finished_event(event)

    assert "جاهز" in window.statuses


def test_download_controller_start_download_worker_rejects_invalid_task_type():
    _ensure_qt_app()
    window = _make_download_window()
    controller = DownloadController(window)

    ok = controller.start_download_worker("not-a-dict")

    assert ok is False
    assert window.warnings == ["تعذر بدء التحميل: بيانات المهمة غير صالحة"]


def test_download_controller_start_download_worker_pauses_when_storage_guard_blocks(monkeypatch):
    _ensure_qt_app()
    window = _make_download_window(queue_items=[{"url": "https://example.com/file", "status": "pending"}])
    controller = DownloadController(window)
    task = {"url": "https://example.com/file", "status": "pending", "out_dir": "D:/target"}
    monkeypatch.setattr(controller, "check_storage_guard", lambda path, pause_on_low=False: False)

    ok = controller.start_download_worker(task, worker_id=0)

    assert ok is False
    assert window.queue_manager.items[0]["status"] == "paused"
    assert window.queue_manager.items[0]["next_retry_at"] == 0
    assert window.saved == 1
    assert window.refresh_calls == 1


def test_download_controller_build_task_snapshots_execution_settings():
    _ensure_qt_app()
    search_view = SimpleNamespace(
        is_audio_mode=lambda: False,
        url_input=_DummyLineEdit("https://example.com/watch?v=1"),
        out_dir_input=_DummyLineEdit("D:/downloads"),
        aria2_checkbox=_DummyCheckBox(True),
        format_combo=_DummyCombo("mp4"),
        subtitle_combo=_DummyCombo("en"),
        start_input=_DummyLineEdit("00:01"),
        end_input=_DummyLineEdit("00:11"),
        category_combo=_DummyCombo("Clips"),
        schedule_repeat_combo=_DummyCombo("daily", "daily"),
        post_action_combo=_DummyCombo("none", "none"),
        post_script_input=_DummyLineEdit(""),
    )
    window = SimpleNamespace(
        search_view=search_view,
        settings_view=SimpleNamespace(
            get_form_settings=lambda: {
                "retries": 5,
                "use_aria2": True,
                "embed_subs": False,
                "split_chapters": True,
                "whisper_fallback": True,
                "sponsorblock_enabled": True,
                "verify_checksum": True,
                "virus_scan_after_download": True,
                "normalize_audio_postprocess": True,
                "use_ytdlp_api": True,
                "cookies_from_browser": "edge",
                "rename_template": "Smart",
                "use_native_engine": True,
                "custom_merge_enabled": True,
                "custom_merge_video_codec": "libx264",
                "custom_merge_video_crf": 19,
                "custom_merge_audio_codec": "aac",
                "custom_merge_audio_bitrate": "256k",
                "custom_merge_hw_encoder": "auto",
                "custom_merge_force_reencode": True,
                "custom_merge_video_preset": "p6",
                "post_download_script": "",
            }
        ),
        AUDIO_FORMATS={"mp3", "wav"},
        AUDIO_QUALITIES=["320kbps"],
        preview_data={
            "duration_seconds": 321,
            "title": "Preview Title",
            "thumbnail": "thumb.jpg",
            "channel": "Preview Channel",
            "is_live": True,
            "was_live": False,
            "live_status": "is_live",
            "video_id": "vid-321",
            "entry_id": "entry-321",
            "playlist_url": "https://example.com/playlist?list=abc",
            "playlist_index": 3,
            "playlist_title": "Preview Playlist",
            "source": "browser_extension",
            "trims": [{"start": "00:01", "end": "00:10", "title": "intro"}],
        },
        auto_retry_delay_seconds=9,
        queue_auto_retry_limit=4,
        _current_bandwidth_limit_kbps=lambda: 0,
        _quality_value=lambda: "1080p",
    )
    controller = DownloadController(window)

    task = controller.build_task()

    assert task["verify_checksum"] is True
    assert task["virus_scan_after_download"] is True
    assert task["normalize_audio_postprocess"] is True
    assert task["use_ytdlp_api"] is True
    assert task["cookies_from_browser"] == "edge"
    assert task["rename_template"] == "Smart"
    assert task["use_native_engine"] is True
    assert task["embed_subs"] is False
    assert task["split_chapters"] is True
    assert task["whisper_fallback"] is True
    assert task["sponsorblock_enabled"] is True
    assert task["merge_opts"] == {
        "enabled": True,
        "video_codec": "libx264",
        "video_crf": 19,
        "audio_codec": "aac",
        "audio_bitrate": "256k",
        "hw_encoder": "auto",
        "force_reencode": True,
        "video_preset": "p6",
    }
    assert task["channel"] == "Preview Channel"
    assert task["is_live"] is True
    assert task["was_live"] is False
    assert task["live_status"] == "is_live"
    assert task["video_id"] == "vid-321"
    assert task["entry_id"] == "entry-321"
    assert task["playlist_url"] == "https://example.com/playlist?list=abc"
    assert task["playlist_index"] == 3
    assert task["playlist_title"] == "Preview Playlist"
    assert task["source"] == "browser_extension"
    assert task["trims"] == [{"start": "00:01", "end": "00:10", "title": "intro"}]
    assert task["category"] == "Clips"
    assert task["schedule_repeat"] == "daily"


def test_download_controller_handle_download_finished_auto_categorizes_output(monkeypatch, tmp_path):
    _ensure_qt_app()
    source_dir = tmp_path / "downloads"
    source_dir.mkdir()
    file_path = source_dir / "clip.mp4"
    subtitle_path = source_dir / "clip.en.srt"
    file_path.write_bytes(b"video")
    subtitle_path.write_text("subtitle", encoding="utf-8")

    window = _make_download_window(
        queue_items=[
            {
                "url": "https://example.com/file",
                "status": "running",
                "title": "Clip",
                "mode": "video",
                "post_action": "",
                "post_download_script": "",
            }
        ]
    )
    window.settings_view = SimpleNamespace(
        get_form_settings=lambda: {
            "auto_categorize_downloads": True,
            "auto_categorize_mode": "mode_then_extension",
        }
    )
    window._active_workers_count = lambda: 1
    window._show_tray_message = lambda *args, **kwargs: None
    controller = DownloadController(window)
    monkeypatch.setattr(controller, "append_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(controller, "_flush_progress_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "core.window_controllers.download_controller.write_nfo_for_download",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "core.window_controllers.download_controller.QTimer.singleShot",
        lambda *args, **kwargs: None,
    )

    event = SimpleNamespace(
        worker_id=0,
        success=True,
        message="done",
        data={"file_path": str(file_path)},
    )

    controller.handle_download_finished_event(event)

    expected_dir = source_dir / "Videos" / "MP4"
    expected_file = expected_dir / "clip.mp4"
    expected_subtitle = expected_dir / "clip.en.srt"
    assert expected_file.is_file()
    assert expected_subtitle.is_file()
    assert not file_path.exists()
    assert window.queue_manager.items[0]["file_path"] == str(expected_file)
    assert window.queue_manager.items[0]["last_output_path"] == str(expected_file)
    assert event.data["file_path"] == str(expected_file)
    assert any("تم تنظيم الملف تلقائيًا" in line for line in window.logs)


def test_download_controller_start_download_worker_prefers_task_scoped_execution_settings(monkeypatch):
    _ensure_qt_app()
    window = _make_download_window()
    window.settings_view = SimpleNamespace(
        get_form_settings=lambda: {
            "embed_subs": False,
            "split_chapters": False,
            "rename_template": "Default",
            "verify_checksum": False,
            "virus_scan_after_download": False,
            "normalize_audio_postprocess": False,
            "use_ytdlp_api": False,
            "cookies_from_browser": "none",
            "whisper_fallback": False,
            "sponsorblock_enabled": False,
            "custom_merge_enabled": False,
        }
    )
    controller = DownloadController(window)
    monkeypatch.setattr(controller, "check_storage_guard", lambda path, pause_on_low=False: True)
    monkeypatch.setattr(controller, "get_duplicate_report", lambda task, force=True: {"is_duplicate": False})
    captured = {}

    class _Signal:
        def connect(self, callback):
            return None

    class _StubWorker:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.progress = _Signal()
            self.log = _Signal()
            self.state = _Signal()
            self.finished = _Signal()

        def __setattr__(self, name, value):
            object.__setattr__(self, name, value)
            if name in {"sponsorblock_enabled", "whisper_fallback_enabled"}:
                captured[name] = value

        def start(self):
            captured["started"] = True

    monkeypatch.setattr("core.window_controllers.download_controller.DownloadWorker", _StubWorker)

    ok = controller.start_download_worker(
        {
            "url": "https://example.com/file",
            "mode": "video",
            "quality": "720p",
            "format": "mp4",
            "status": "pending",
            "out_dir": "D:/target",
            "embed_subs": True,
            "split_chapters": True,
            "whisper_fallback": True,
            "sponsorblock_enabled": True,
            "verify_checksum": True,
            "virus_scan_after_download": True,
            "normalize_audio_postprocess": True,
            "use_ytdlp_api": True,
            "cookies_from_browser": "firefox",
            "rename_template": "Archive",
            "use_native_engine": True,
            "is_live": True,
            "was_live": False,
            "live_status": "is_live",
            "merge_opts": {
                "enabled": True,
                "video_codec": "libx264",
                "video_crf": 20,
                "audio_codec": "aac",
                "audio_bitrate": "320k",
                "hw_encoder": "qsv",
                "force_reencode": True,
                "video_preset": "p7",
            },
        },
        worker_id=0,
    )

    assert ok is True
    assert captured["verify_checksum"] is True
    assert captured["virus_scan_after_download"] is True
    assert captured["normalize_audio_postprocess"] is True
    assert captured["use_ytdlp_api"] is True
    assert captured["cookies_from_browser"] == "firefox"
    assert captured["rename_template"] == "Archive"
    assert captured["use_native_engine"] is True
    assert captured["embed_subs"] is True
    assert captured["split_chapters"] is True
    assert captured["sponsorblock_enabled"] is True
    assert captured["whisper_fallback_enabled"] is True
    assert captured["is_live_hint"] is True
    assert captured["was_live_hint"] is False
    assert captured["live_status_hint"] == "is_live"
    assert captured["merge_opts"] == {
        "enabled": True,
        "video_codec": "libx264",
        "video_crf": 20,
        "audio_codec": "aac",
        "audio_bitrate": "320k",
        "hw_encoder": "qsv",
        "force_reencode": True,
        "video_preset": "p7",
    }
    assert captured["started"] is True


def test_download_controller_persist_progress_snapshot_batches_and_throttles_db_writes(monkeypatch):
    _ensure_qt_app()
    window = _make_download_window(queue_items=[{"url": "https://example.com/file", "task_uuid": "task-1"}])
    controller = DownloadController(window)
    calls = []
    timeline = iter([10.0, 10.5, 11.0, 12.5, 13.0])
    monkeypatch.setattr(
        "core.window_controllers.download_controller.update_task_states_fast_batch",
        lambda payload: calls.append(payload),
    )
    monkeypatch.setattr(
        "core.window_controllers.download_controller.time.monotonic",
        lambda: next(timeline),
    )

    controller._persist_progress_snapshot(0, 10.0, "1.0 MiB/s", "00:10", "running")
    controller._persist_progress_snapshot(0, 10.2, "1.1 MiB/s", "00:09", "running")
    controller._persist_progress_snapshot(0, 10.4, "1.2 MiB/s", "00:08", "running")
    controller._persist_progress_snapshot(0, 10.6, "1.3 MiB/s", "00:07", "running")
    controller._persist_progress_snapshot(0, 10.7, "1.4 MiB/s", "00:06", "success", force=True)

    assert calls == [
        [
            {
                "queue_index": 0,
                "task_uuid": "task-1",
                "progress": 10.7,
                "speed": "1.4 MiB/s",
                "eta": "00:06",
                "status": "success",
            }
        ],
    ]


def test_download_controller_flush_keeps_newer_progress_payload_that_arrives_mid_flush(monkeypatch):
    _ensure_qt_app()
    window = _make_download_window(queue_items=[{"url": "https://example.com/file", "task_uuid": "task-1"}])
    controller = DownloadController(window)
    initial_payload = {
        "queue_index": 0,
        "task_uuid": "task-1",
        "progress": 10.0,
        "speed": "1.0 MiB/s",
        "eta": "00:10",
        "status": "running",
    }
    newer_payload = {
        "queue_index": 0,
        "task_uuid": "task-1",
        "progress": 42.0,
        "speed": "4.2 MiB/s",
        "eta": "00:04",
        "status": "running",
    }
    with controller._db_progress_lock:
        controller._db_progress_dirty[0] = dict(initial_payload)
    calls = []

    def _batch_write(payloads):
        calls.append(payloads)
        with controller._db_progress_lock:
            controller._db_progress_dirty[0] = dict(newer_payload)

    monkeypatch.setattr(
        "core.window_controllers.download_controller.update_task_states_fast_batch",
        _batch_write,
    )

    controller._flush_pending_progress_writes()

    assert calls == [[initial_payload]]
    with controller._db_progress_lock:
        assert controller._db_progress_dirty == {0: newer_payload}


def test_download_controller_shutdown_stops_and_waits_active_workers():
    _ensure_qt_app()
    window = _make_download_window(queue_items=[{"url": "https://example.com/file", "status": "running"}])
    window.queue_running = True
    window.queue_paused = True

    class _Worker:
        def __init__(self):
            self.stop_calls = 0
            self.quit_calls = 0
            self.wait_calls = []
            self.deleted = False

        def stop(self):
            self.stop_calls += 1

        def quit(self):
            self.quit_calls += 1

        def wait(self, timeout_ms):
            self.wait_calls.append(int(timeout_ms))
            return True

        def deleteLater(self):
            self.deleted = True

    worker_a = _Worker()
    worker_b = _Worker()
    window.active_workers = {0: worker_a, 1: worker_b}
    controller = DownloadController(window)

    controller.shutdown()

    assert window.active_workers == {}
    assert worker_a.stop_calls == 1 and worker_b.stop_calls == 1
    assert worker_a.quit_calls == 1 and worker_b.quit_calls == 1
    assert worker_a.wait_calls == [3000] and worker_b.wait_calls == [3000]
    assert worker_a.deleted is True and worker_b.deleted is True
    assert window.queue_running is False
    assert window.queue_paused is False


def test_download_controller_worker_cleanup_fallback_runs_inline_without_spawning_thread(monkeypatch):
    _ensure_qt_app()
    window = _make_download_window(queue_items=[{"url": "https://example.com/file", "status": "running"}])
    controller = DownloadController(window)

    class _BrokenExecutor:
        def submit(self, _fn):
            raise RuntimeError("executor unavailable")

    class _Worker:
        def __init__(self):
            self.deleted = False

        def isFinished(self):
            return True

        def wait(self, _timeout_ms):
            return True

        def deleteLater(self):
            self.deleted = True

    worker = _Worker()
    window.active_workers = {0: worker}
    controller._worker_cleanup_executor = _BrokenExecutor()
    thread_ctor = Mock(side_effect=AssertionError("fallback thread should not be created"))
    monkeypatch.setattr("core.window_controllers.download_controller.threading.Thread", thread_ctor)
    monkeypatch.setattr("core.window_controllers.download_controller.run_on_qt_main_thread", lambda fn: False)

    controller.on_worker_thread_finished(0, worker)

    assert worker.deleted is True
    assert thread_ctor.call_count == 0


def test_download_controller_history_write_fallback_runs_inline_without_spawning_thread(monkeypatch):
    _ensure_qt_app()
    window = _make_download_window(queue_items=[{"url": "https://example.com/file", "status": "running"}])
    window.stats = {"download_history": [], "total_videos": 0, "total_audios": 0}
    window._save_stats_calls = 0
    window._save_stats = lambda: setattr(window, "_save_stats_calls", int(window._save_stats_calls) + 1)
    window._normalize_history_mode = lambda value: str(value or "")
    window._size_to_bytes = lambda _value: 0
    controller = DownloadController(window)

    class _BrokenExecutor:
        def submit(self, _fn):
            raise RuntimeError("executor unavailable")

    controller._history_write_executor = _BrokenExecutor()
    thread_ctor = Mock(side_effect=AssertionError("fallback thread should not be created"))
    monkeypatch.setattr("core.window_controllers.download_controller.threading.Thread", thread_ctor)
    monkeypatch.setattr("core.window_controllers.download_controller.insert_history", lambda _entry: None)
    monkeypatch.setattr(
        "core.window_controllers.download_controller.get_all_stats",
        lambda: {"total_videos": 1, "total_audios": 0},
    )
    monkeypatch.setattr("core.window_controllers.download_controller.close_thread_connection", lambda: None)
    monkeypatch.setattr(
        "core.window_controllers.download_controller.run_on_qt_main_thread",
        lambda fn: (fn(), True)[1],
    )

    controller.append_history(
        success=True,
        message="ok",
        payload={"timestamp": "2026-04-22T00:00:00", "file_path": "D:/downloads/a.mp4", "attempts": 1, "error": ""},
        task={"url": "https://example.com/file", "mode": "video", "format": "mp4", "quality": "720p", "thumbnail": "", "title": "A"},
    )

    assert thread_ctor.call_count == 0
    assert window._save_stats_calls == 1


def test_download_controller_update_downloads_dashboard_uses_queue_counts_without_snapshot(monkeypatch):
    _ensure_qt_app()
    window = _make_download_window()
    window.stats = {
        "download_history": [
            {"status": "success"},
            {"status": "completed"},
            {"status": "failed"},
        ]
    }
    captured = {}
    window.downloads_view = SimpleNamespace(
        set_dashboard_counts=lambda active, queued, completed, failed: captured.update(
            {
                "active": int(active),
                "queued": int(queued),
                "completed": int(completed),
                "failed": int(failed),
            }
        )
    )

    class _QueueCountsOnly:
        def get_dashboard_queue_counts(self):
            return {"active": 3, "queued": 5}

        def get_queue_items_snapshot(self):
            raise AssertionError("snapshot should not be used when queue counts are available")

    window.queue_manager = _QueueCountsOnly()
    controller = DownloadController(window)
    monkeypatch.setattr("core.window_controllers.download_controller.count_history_statuses", lambda _statuses: 0)
    monkeypatch.setattr("core.window_controllers.download_controller.count_history", lambda _status=None: 0)

    controller.update_downloads_dashboard()

    assert captured == {"active": 3, "queued": 5, "completed": 2, "failed": 1}


def test_download_controller_handle_download_finished_uses_item_count_without_snapshot(monkeypatch):
    _ensure_qt_app()
    window = _make_download_window()
    window.error_dashboard = SimpleNamespace(report_error=lambda **kwargs: None)
    window._show_tray_message = lambda *args, **kwargs: None
    window._set_controls_enabled = lambda _value: None

    class _CountOnlyQueueManager:
        def __init__(self):
            self.statuses = {}

        def get_item_count(self):
            return 1

        def get_queue_items_snapshot(self):
            raise AssertionError("queue snapshot should not be used in finished-event path")

        def get_task(self, index):
            if index == 0:
                return {"url": "https://example.com/file", "status": "running", "title": "Clip"}
            return None

        def set_task_status(self, index, status):
            self.statuses[int(index)] = str(status)

    window.queue_manager = _CountOnlyQueueManager()
    controller = DownloadController(window)
    monkeypatch.setattr(controller, "append_history", lambda *args, **kwargs: None)
    monkeypatch.setattr(controller, "_flush_progress_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr("core.window_controllers.download_controller.write_nfo_for_download", lambda *args, **kwargs: None)
    monkeypatch.setattr("core.window_controllers.download_controller.QTimer.singleShot", lambda *args, **kwargs: None)

    event = SimpleNamespace(
        worker_id=0,
        success=False,
        message="cancelled",
        data={"error": "cancelled"},
    )

    controller.handle_download_finished_event(event)

    assert window.queue_manager.statuses.get(0) == "cancelled"


def test_download_controller_dashboard_history_counts_are_cached(monkeypatch):
    _ensure_qt_app()
    window = _make_download_window()
    window.stats = {"download_history": []}
    captured = []
    window.downloads_view = SimpleNamespace(
        set_dashboard_counts=lambda active, queued, completed, failed: captured.append(
            (int(active), int(queued), int(completed), int(failed))
        )
    )

    class _QueueCountsOnly:
        def get_dashboard_queue_counts(self):
            return {"active": 1, "queued": 2}

    window.queue_manager = _QueueCountsOnly()
    controller = DownloadController(window)
    controller._history_dashboard_cache_ttl_seconds = 999.0

    calls = {"completed": 0, "failed": 0}

    def _count_completed(_statuses):
        calls["completed"] += 1
        return 5

    def _count_failed(_status=None):
        calls["failed"] += 1
        return 2

    monkeypatch.setattr("core.window_controllers.download_controller.count_history_statuses", _count_completed)
    monkeypatch.setattr("core.window_controllers.download_controller.count_history", _count_failed)

    controller.update_downloads_dashboard()
    controller.update_downloads_dashboard()

    assert calls == {"completed": 1, "failed": 1}
    assert captured[-1] == (1, 2, 5, 2)
