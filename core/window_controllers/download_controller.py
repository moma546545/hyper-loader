
from core.utils import get_app_data_dir, redact_url
import json
import logging
import os
import glob
import re
import sys
import threading
import time
from collections import deque
from contextlib import suppress
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from uuid import uuid4

try:
    import winreg
except ImportError:
    winreg = None

try:
    from PySide6.QtCore import QTimer, QTime, QRunnable, QThreadPool
    from PySide6.QtWidgets import QFileDialog, QLineEdit, QSpinBox, QCheckBox, QComboBox, QSystemTrayIcon, QMessageBox
except ImportError:
    from PyQt6.QtCore import QTimer, QTime, QRunnable, QThreadPool
    from PyQt6.QtWidgets import QFileDialog, QLineEdit, QSpinBox, QCheckBox, QComboBox, QSystemTrayIcon, QMessageBox

from core.audio_normalizer import normalize_folder
from core.anti_detection import anti_detection_engine
from core.bandwidth_scheduler import scheduler
from core.config import DEFAULT_SETTINGS, THEME_MODE_MAP, default_download_dir, estimate_file_size_bytes
from core.cookie_importer import auto_detect_and_export, encrypt_cookie_file_inplace
from core.database import (
    close_thread_connection,
    count_history,
    count_history_statuses,
    get_all_stats,
    increment_stat,
    insert_history,
    load_queue_items,
    load_session_settings,
    record_peak_speed,
    save_queue_items,
    update_task_resume_snapshot,
    save_session_settings,
    update_task_state_fast,
    update_task_states_fast_batch,
)
from core.download_organizer import organize_download_output, normalize_auto_categorize_mode
from core.downloader import DownloadWorker
from core.duplicate_finder import build_duplicate_report
from core.file_integrity_watcher import FileIntegrityWatcher
from core.media_engine import FormatDecisionEngine
from core.media_size import apply_estimated_size, coerce_size_bytes
from core.proxy_manager import proxy_manager
from core.storage_watchdog import format_bytes, has_enough_space, free_bytes
from core.sustainability import sustainability
from core.i18n import i18n, _
from core.error_handler import ErrorHandler
from core.task_types import DownloadHistoryEntry, DownloadTask, TaskStatus
from core.post_actions import PostDownloadManager
from core.nfo_writer import write_nfo_for_download
from core.qt_dispatch import run_on_qt_main_thread
from core.workers import AnalyzeWorker
from core.storage_watchdog import format_bytes, has_enough_space, free_bytes
from core.sustainability import sustainability
from core.i18n import i18n, _
from core.error_handler import ErrorHandler
from core.task_types import DownloadHistoryEntry, DownloadTask, TaskStatus
from core.post_actions import PostDownloadManager
from core.nfo_writer import write_nfo_for_download
from core.qt_dispatch import run_on_qt_main_thread
from core.workers import AnalyzeWorker
from ui.themes import THEMES
from ui.batch_duplicate_dialog import BatchDuplicateDialog

logger = logging.getLogger("SnapDownloader")
_STATUS_PENDING = TaskStatus.PENDING.value
_STATUS_QUEUED = TaskStatus.QUEUED.value
_STATUS_RUNNING = TaskStatus.RUNNING.value
_STATUS_DOWNLOADING = TaskStatus.DOWNLOADING.value
_STATUS_PAUSED = TaskStatus.PAUSED.value
_STATUS_CANCELLED = TaskStatus.CANCELLED.value
_STATUS_FAILED = TaskStatus.FAILED.value
_STATUS_SUCCESS = TaskStatus.SUCCESS.value
_STATUS_COMPLETED = TaskStatus.COMPLETED.value
_STATUS_ERROR = TaskStatus.ERROR.value
_STATUS_DELETED = TaskStatus.DELETED.value
_QUEUE_STATE_ALL = "all"
_RETRYABLE_QUEUE_STATUSES = {_STATUS_FAILED, _STATUS_CANCELLED, _STATUS_PAUSED}
_QUEUED_FILTER_STATUSES = {_STATUS_PENDING, _STATUS_PAUSED, _STATUS_FAILED, _STATUS_CANCELLED, _STATUS_QUEUED, "waiting"}


class _SafeRunnable(QRunnable):
    def __init__(self, fn, *, label: str = "task"):
        super().__init__()
        self._fn = fn
        self._label = str(label or "task")

    def run(self):
        try:
            self._fn()
        except Exception as exc:
            logger.debug(f"Background runnable failed ({self._label}): {exc}")


class DownloadController:
    def __init__(self, window):
        self.window = window
        self._last_downloads_refresh_ts = 0.0
        self._MIN_REFRESH_GAP_S = 0.08
        self._speed_history: dict[int, deque] = {}
        self._speed_history_max_workers: int = max(64, int(getattr(window, "_speed_history_max_workers", 512) or 512))
        self.window._speed_history = self._speed_history
        self._resume_last_save_ts: dict = {}
        self._duplicate_report_cache: dict = {}
        self._batch_duplicate_allowed_urls: set[str] = set()
        self._last_batch_duplicate_review_cancelled = False
        self._peak_speed_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="PeakSpeed")
        self._post_download_pool = QThreadPool()
        self._post_download_pool.setMaxThreadCount(2)
        self._post_download_pool.setExpiryTimeout(15_000)
        # Backward-compat hooks used by existing tests and legacy integrations.
        self._history_write_executor = None
        self._worker_cleanup_executor = None
        self._db_progress_state: dict[int, dict] = {}
        self._db_progress_dirty: dict[int, dict] = {}
        self._db_progress_lock = threading.Lock()
        self._db_progress_min_interval_seconds = 2.0
        self._db_progress_min_delta = 1.0
        self._db_progress_flush_interval_ms = 900
        self._db_progress_flush_timer = QTimer()
        self._db_progress_flush_timer.setSingleShot(True)
        self._db_progress_flush_timer.timeout.connect(self._flush_pending_progress_writes)
        self._history_dashboard_cache_ttl_seconds = 1.25
        self._history_dashboard_cache_ts = 0.0
        self._history_dashboard_completed = 0
        self._history_dashboard_failed = 0
        self._file_integrity_watcher = FileIntegrityWatcher()
        self._file_integrity_watcher.file_missing.connect(self._on_tracked_file_missing)
        self._format_decision_engine = FormatDecisionEngine()

    def _submit_post_download_task(self, fn, *, label: str, legacy_executor_attr: str | None = None):
        if legacy_executor_attr:
            executor = getattr(self, legacy_executor_attr, None)
            if executor is not None and hasattr(executor, "submit"):
                try:
                    executor.submit(fn)
                    return
                except Exception:
                    fn()
                    return
        runnable = _SafeRunnable(fn, label=label)
        try:
            self._post_download_pool.start(runnable)
        except Exception:
            fn()

    def _queue_is_running(self) -> bool:
        queue_manager = getattr(self.window, "queue_manager", None)
        if queue_manager is not None and hasattr(queue_manager, "is_running"):
            manager_running = bool(getattr(queue_manager, "is_running", False))
            # QueueManager is the source of truth when available.
            effective_running = manager_running
            with suppress(Exception):
                self.window.queue_running = effective_running
            if not effective_running:
                with suppress(Exception):
                    self.window.queue_paused = False
                if hasattr(queue_manager, "is_paused"):
                    with suppress(Exception):
                        queue_manager.is_paused = False
            return effective_running
        return bool(getattr(self.window, "queue_running", False))

    def _queue_is_paused(self) -> bool:
        queue_manager = getattr(self.window, "queue_manager", None)
        running = self._queue_is_running()
        if not running:
            with suppress(Exception):
                self.window.queue_paused = False
            if queue_manager is not None and hasattr(queue_manager, "is_paused"):
                with suppress(Exception):
                    queue_manager.is_paused = False
            return False
        if queue_manager is not None and hasattr(queue_manager, "is_paused"):
            manager_paused = bool(getattr(queue_manager, "is_paused", False))
            # QueueManager is the source of truth when available.
            effective_paused = manager_paused
            with suppress(Exception):
                self.window.queue_paused = effective_paused
            return effective_paused
        return bool(getattr(self.window, "queue_paused", False))

    def _set_queue_runtime_state(self, *, running: bool | None = None, paused: bool | None = None):
        if hasattr(self.window, "_set_queue_runtime_state"):
            self.window._set_queue_runtime_state(running=running, paused=paused)
            return
        queue_manager = getattr(self.window, "queue_manager", None)
        running_value = None if running is None else bool(running)
        paused_value = None if paused is None else bool(paused)
        if queue_manager is None:
            if running_value is not None:
                self.window.queue_running = running_value
            if paused_value is not None:
                active_running = running_value if running_value is not None else bool(getattr(self.window, "queue_running", False))
                self.window.queue_paused = paused_value if active_running else False
            return
        if hasattr(queue_manager, "set_runtime_state"):
            queue_manager.set_runtime_state(is_running=running_value, is_paused=paused_value)
        else:
            if running_value is not None and hasattr(queue_manager, "is_running"):
                queue_manager.is_running = running_value
            if paused_value is not None and hasattr(queue_manager, "is_paused"):
                active_running = running_value if running_value is not None else bool(getattr(queue_manager, "is_running", False))
                queue_manager.is_paused = paused_value if active_running else False
        self.window.queue_running = bool(getattr(queue_manager, "is_running", False))
        self.window.queue_paused = bool(getattr(queue_manager, "is_paused", False)) if self.window.queue_running else False

    def _queue_priority(self) -> str:
        sv = getattr(self.window, "settings_view", None)
        if sv is not None and hasattr(sv, "get_form_settings"):
            form = sv.get_form_settings()
            return str(form.get("queue_priority", "fifo") or "fifo")
        if sv is not None and hasattr(sv, "settings_queue_priority"):
            value = sv.settings_queue_priority.currentData()
            return str(value or "fifo")
        return "fifo"

    def _queue_requires_controller_evaluation(
        self,
        queue_manager,
        *,
        active_worker_ids: set[int] | None = None,
    ) -> bool:
        if queue_manager is None or not hasattr(queue_manager, "plan_parallel_start"):
            return False
        try:
            plan = queue_manager.plan_parallel_start(
                1,
                active_worker_ids=active_worker_ids,
                priority=self._queue_priority(),
                task_size_getter=self._estimate_task_bytes,
                now_ts=datetime.now().timestamp(),
                include_queue_items=False,
            )
        except Exception as exc:
            logger.debug(f"تعذر تقييم حالة الطابور بعد start/resume: {exc}")
            return False
        if list(plan.get("pending_indices", []) or []):
            return True
        if plan.get("next_retry_ts") is not None:
            return True
        status_counts = dict(plan.get("status_counts", {}) or {})
        if any(int(count or 0) > 0 for count in status_counts.values()):
            return True
        if hasattr(queue_manager, "get_item_count"):
            with suppress(Exception):
                return int(queue_manager.get_item_count() or 0) > 0
        return False

    def _queue_stopped_status_text(self) -> str:
        queue_manager = getattr(self.window, "queue_manager", None)
        if queue_manager is None or not hasattr(queue_manager, "get_queue_items_snapshot"):
            return "جاهز"
        try:
            snapshot = list(queue_manager.get_queue_items_snapshot() or [])
        except Exception:
            return "جاهز"
        has_runnable_or_waiting = False
        has_non_runnable = False
        for item in snapshot:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status", _STATUS_PENDING) or _STATUS_PENDING).strip().lower()
            if status in {
                _STATUS_PENDING,
                _STATUS_QUEUED,
                "waiting",
                _STATUS_RUNNING,
                "processing",
                "merging",
                "downloading",
                "",
            }:
                has_runnable_or_waiting = True
                break
            if status in {_STATUS_PAUSED, _STATUS_CANCELLED, _STATUS_FAILED}:
                has_non_runnable = True
        if has_non_runnable and not has_runnable_or_waiting:
            return "لا توجد عناصر قابلة للتشغيل"
        return "جاهز"

    def _estimate_task_bytes(self, task: DownloadTask) -> int:
        explicit = coerce_size_bytes((task or {}).get("estimated_size_bytes") or (task or {}).get("size_bytes"))
        if explicit > 0:
            return explicit
        return estimate_file_size_bytes(
            duration_seconds=int((task or {}).get("duration_seconds") or 0),
            mode=str((task or {}).get("mode", "video")),
            quality=str((task or {}).get("quality", "")),
        )

    def _maybe_auto_quality_for_storage(self, out_dir: str, quality_label: str) -> str:
        try:
            free = int(free_bytes(out_dir))
        except Exception:
            return str(quality_label or "1080p")
        quality = str(quality_label or "1080p")
        if free < (2 * 1024 * 1024 * 1024):
            return "480p"
        if free < (5 * 1024 * 1024 * 1024):
            return "720p" if quality not in {"144p", "240p", "360p", "480p"} else quality
        return quality

    def build_task(self, url=None, title=None, thumbnail="", fmt=None, quality=None) -> DownloadTask:
        mode = "audio" if (fmt in self.window.AUDIO_FORMATS) or self.window.search_view.is_audio_mode() else "video"
        task_url = str(url if url is not None else self.window.search_view.url_input.text()).strip()
        settings_form = {}
        sv = getattr(self.window, "settings_view", None)
        if sv is not None and hasattr(sv, "get_form_settings"):
            try:
                settings_form = sv.get_form_settings()
            except Exception:
                settings_form = {}
        retries_value = int(settings_form.get("retries", 3) or 3)
        bandwidth_limit_kbps = self.window._current_bandwidth_limit_kbps()
        if hasattr(self.window.search_view, "speed_limit_slider"):
            try:
                slider_val = int(self.window.search_view.speed_limit_slider.value())
                if slider_val > 0:
                    bandwidth_limit_kbps = slider_val
            except Exception:
                pass
        base_out_dir = self.window.search_view.out_dir_input.text().strip()
        category = ""
        if hasattr(self.window.search_view, "category_combo"):
            category = self.window.search_view.category_combo.currentText().strip()
        
        if not category or category in {"Auto", "تلقائي", "تلقائى"}:
            if mode == "audio":
                category = "Music"
            elif mode == "video":
                category = "Videos"
            else:
                category = "Other"

        out_dir = base_out_dir
        if category:
            out_dir = os.path.join(base_out_dir, category)
        # Do not auto-schedule tasks here.
        # Scheduling is handled explicitly in `_add_current_to_queue`.
        scheduled_at = 0.0
        schedule_repeat = "none"
        if hasattr(self.window.search_view, "schedule_picker"):
            try:
                schedule_settings = self.window.search_view.schedule_picker.get_schedule_settings()
                scheduled_at = float(schedule_settings.get("scheduled_at", 0) or 0)
                schedule_repeat = str(schedule_settings.get("schedule_repeat", "none") or "none")
            except Exception:
                scheduled_at = 0.0
                schedule_repeat = "none"
        elif hasattr(self.window.search_view, "schedule_repeat_combo"):
            data = self.window.search_view.schedule_repeat_combo.currentData()
            schedule_repeat = str(data or "none")
        use_aria2_audio = bool(settings_form.get("use_aria2", self.window.search_view.aria2_checkbox.isChecked()))
        post_action_value = PostDownloadManager.normalize_action("")
        if hasattr(self.window.search_view, "post_action_combo"):
            try:
                data = self.window.search_view.post_action_combo.currentData()
                post_action_value = PostDownloadManager.normalize_action(data)
            except Exception:
                post_action_value = PostDownloadManager.normalize_action("")
        post_script_value = str(settings_form.get("post_download_script", "") or "").strip()
        cookies_from_browser = str(settings_form.get("cookies_from_browser", "none") or "none").strip().lower()
        if hasattr(self.window.search_view, "post_script_input"):
            try:
                ui_script_value = str(self.window.search_view.post_script_input.text() or "").strip()
                if ui_script_value:
                    post_script_value = ui_script_value
            except Exception:
                pass
        preview_data = self.window.preview_data if url is None else {}
        merge_opts = self._build_merge_opts(settings_form)
        clean_metadata = bool(settings_form.get("clean_metadata", getattr(self.window, "clean_metadata_enabled", True)))
        post_process_pipeline = list(settings_form.get("post_process_pipeline", []) or [])
        if quality:
            task_quality = quality
        elif mode == "audio" and hasattr(self.window, "_audio_quality_value"):
            task_quality = self.window._audio_quality_value()
        else:
            task_quality = self.window.AUDIO_QUALITIES[0] if mode == "audio" else self.window._quality_value()
        if mode == "video":
            task_quality = self._maybe_auto_quality_for_storage(out_dir, task_quality)
        task = DownloadTask(
            url=task_url,
            out_dir=out_dir,
            mode=mode,
            quality=task_quality,
            format=fmt or self.window.search_view.format_combo.currentText().strip(),
            subtitle=self.window.search_view.subtitle_combo.currentText().strip(),
            start_time=self.window.search_view.start_input.text().strip(),
            end_time=self.window.search_view.end_input.text().strip(),
            retries=retries_value,
            auto_retry_delay_seconds=int(self.window.auto_retry_delay_seconds),
            queue_retry_limit=int(self.window.queue_auto_retry_limit),
            retry_count=0,
            next_retry_at=0,
            scheduled_at=scheduled_at,
            bandwidth_limit_kbps=int(bandwidth_limit_kbps),
            use_aria2=use_aria2_audio,
            title=str(title or preview_data.get("title", "") or task_url).strip(),
            thumbnail=thumbnail or str(preview_data.get("thumbnail", "") or ""),
            cookies_from_browser=cookies_from_browser,
            duration_seconds=int(preview_data.get("duration_seconds", 0) or 0),
            is_live=bool(preview_data.get("is_live", False)),
            was_live=bool(preview_data.get("was_live", False)),
            live_status=str(preview_data.get("live_status", "") or ""),
            status=TaskStatus.PENDING.value,
            category=category,
            schedule_repeat=schedule_repeat,
            channel=str(preview_data.get("channel", "") or ""),
            trims=list(preview_data.get("trims", []) or []) if isinstance(preview_data.get("trims", []), list) else [],
            video_id=str(preview_data.get("video_id", "") or ""),
            entry_id=str(preview_data.get("entry_id", "") or ""),
            playlist_url=str(preview_data.get("playlist_url", "") or preview_data.get("webpage_url", "") or ""),
            playlist_index=int(preview_data.get("playlist_index", 0) or 0),
            playlist_title=str(preview_data.get("playlist_title", "") or ""),
            source=str(preview_data.get("source", "") or ""),
            post_action=post_action_value,
            post_download_script=post_script_value,
            embed_subs=bool(settings_form.get("embed_subs", True)),
            split_chapters=bool(settings_form.get("split_chapters", False)),
            whisper_fallback=bool(settings_form.get("whisper_fallback", False)),
            sponsorblock_enabled=bool(settings_form.get("sponsorblock_enabled", False)),
            verify_checksum=bool(settings_form.get("verify_checksum", False)),
            virus_scan_after_download=bool(settings_form.get("virus_scan_after_download", False)),
            normalize_audio_postprocess=bool(settings_form.get("normalize_audio_postprocess", False)),
            use_ytdlp_api=bool(settings_form.get("use_ytdlp_api", False)),
            clean_metadata=clean_metadata,
            rename_template=str(settings_form.get("rename_template", "Default") or "Default"),
            use_native_engine=bool(settings_form.get("use_native_engine", False)),
            merge_opts=merge_opts,
            post_process_pipeline=post_process_pipeline,
        )
        apply_estimated_size(
            task,
            preview_data,
            duration_seconds=int(task.get("duration_seconds") or 0),
            mode=mode,
            quality=task_quality,
            fmt=str(task.get("format", "")),
        )
        return task

    def normalize_task(
        self,
        task: DownloadTask | None,
        *,
        url=None,
        title=None,
        thumbnail=None,
        fmt=None,
        quality=None,
        subtitle=None,
        duration_seconds=None,
        retries=None,
        out_dir=None,
        channel=None,
        mode=None,
        is_live=None,
        was_live=None,
        live_status=None,
    ) -> DownloadTask:
        normalized: DownloadTask = task if isinstance(task, dict) else DownloadTask()
        if url is not None:
            normalized["url"] = str(url).strip()
        if title is not None:
            normalized["title"] = str(title).strip()
        if thumbnail is not None:
            normalized["thumbnail"] = str(thumbnail).strip()
        if fmt is not None:
            normalized["format"] = str(fmt or normalized.get("format", "MP4")).strip() or str(normalized.get("format", "MP4"))
        if quality is not None:
            normalized["quality"] = str(quality or normalized.get("quality", "1080p")).strip() or str(normalized.get("quality", "1080p"))
        if subtitle is not None:
            normalized["subtitle"] = str(subtitle or normalized.get("subtitle", "None")).strip() or "None"
        if duration_seconds is not None:
            normalized["duration_seconds"] = int(duration_seconds or 0)
        if retries is not None:
            normalized["retries"] = int(retries or 0)
        if out_dir is not None:
            normalized["out_dir"] = str(out_dir).strip() or str(normalized.get("out_dir", "")).strip()
        if channel is not None:
            normalized["channel"] = str(channel).strip()
        if mode is not None:
            normalized["mode"] = str(mode or normalized.get("mode", "video")).strip() or str(normalized.get("mode", "video"))
        if is_live is not None:
            normalized["is_live"] = bool(is_live)
        if was_live is not None:
            normalized["was_live"] = bool(was_live)
        if live_status is not None:
            normalized["live_status"] = str(live_status).strip()
        return normalized

    def storage_guard_paths(self) -> list[str]:
        seen = set()
        paths = []
        with self.window._active_workers_lock:
            keys = list(self.window.active_workers.keys())
        for wid in keys:
            task = self.window.queue_manager.get_task(wid) if isinstance(wid, int) else None
            out_dir = str((task or {}).get("out_dir", "")).strip()
            if out_dir and out_dir not in seen:
                seen.add(out_dir)
                paths.append(out_dir)
        fallback = str(self.window.current_download_path or "").strip()
        if not fallback and hasattr(self.window, "out_dir_input"):
            fallback = self.window.search_view.out_dir_input.text().strip()
        if fallback and fallback not in seen:
            paths.append(fallback)
        return paths

    def storage_guard_message(self, path: str, free: int, threshold: int) -> str:
        return (
            f"⚠️ المساحة الحرة منخفضة في {path} — المتاح {format_bytes(free)} "
            f"والحد المطلوب {format_bytes(threshold)}. تم إيقاف التحميل مؤقتاً."
        )

    def pause_downloads_for_storage_guard(self, message: str):
        self._set_queue_runtime_state(paused=True)
        try:
            self.window.queue_manager.pause_queue()
        except Exception as exc:
            logger.debug(f"تعذر إيقاف الطابور بسبب Storage Guard: {exc}")
        with self.window._active_workers_lock:
            items = list(self.window.active_workers.items())
        for wid, worker in items:
            self._mark_pause_requested(wid)
            self._update_queue_task(wid, {"status": _STATUS_PAUSED, "next_retry_at": 0}, emit_changed=False)
            try:
                worker.stop()
            except Exception as exc:
                logger.debug(f"تعذر إيقاف worker {wid} بسبب Storage Guard: {exc}")
        if message != self.window._storage_guard_last_message or not self.window._storage_guard_alerted:
            self.window._append_log(message)
            self.window._warn(message)
            self.window._show_tray_message(
                "SnapDownloader",
                message[:120],
                QSystemTrayIcon.MessageIcon.Warning,
                3500,
            )
        self.window._storage_guard_alerted = True
        self.window._storage_guard_last_message = message
        self.window._set_status("المساحة منخفضة")
        self.window._save_session()
        self.window._refresh_downloads_list()

    def check_storage_guard(self, path: str = "", pause_on_low: bool = False) -> bool:
        if not self.window.storage_guard_enabled:
            self.window._storage_guard_alerted = False
            self.window._storage_guard_last_message = ""
            return True
        targets = [str(path).strip()] if str(path or "").strip() else self.storage_guard_paths()
        if not targets:
            return True
        for target in targets:
            has_space, free, threshold, resolved = has_enough_space(target, self.window.storage_min_free_gb)
            if has_space:
                continue
            message = self.storage_guard_message(resolved, free, threshold)
            if pause_on_low:
                self.pause_downloads_for_storage_guard(message)
            else:
                if message != self.window._storage_guard_last_message or not self.window._storage_guard_alerted:
                    self.window._append_log(message)
                    self.window._warn(message)
                self.window._storage_guard_alerted = True
                self.window._storage_guard_last_message = message
                self.window._set_status("المساحة منخفضة")
            return False
        self.window._storage_guard_alerted = False
        self.window._storage_guard_last_message = ""
        return True

    def run_storage_watchdog(self):
        if self.window._is_closing or not self.window.storage_guard_enabled:
            return
        if self.window._active_workers_count() == 0:
            return
        self.check_storage_guard(pause_on_low=True)

    def download_subscription_videos(self, urls: list, sub_dict: dict):
        if not urls:
            return
        out_dir = sub_dict.get("out_dir")
        if not out_dir:
            out_dir = self.window.search_view.out_dir_input.text().strip()
        else:
            if not os.path.isabs(out_dir):
                base_dir = self.window.search_view.out_dir_input.text().strip()
                out_dir = os.path.normpath(os.path.join(base_dir, out_dir))
        
        for url in urls:
            task = self.build_task(
                url=url,
                fmt=sub_dict.get("format"),
                quality=sub_dict.get("quality")
            )
            task["out_dir"] = out_dir
            if hasattr(self.window, "queue_manager") and self.window.queue_manager is not None:
                self.window.queue_manager.add_task(task)
        
        if not self._queue_is_running():
            self.start_queue_download()

    def start_queue_download(self):
        self.window._prune_inactive_workers()
        with self.window._active_workers_lock:
            active_worker_ids = set(self.window.active_workers.keys())
        queue_manager = getattr(self.window, "queue_manager", None)
        restored = 0
        if queue_manager is not None and hasattr(queue_manager, "restore_stale_running_tasks"):
            try:
                restored = int(queue_manager.restore_stale_running_tasks(active_worker_ids=active_worker_ids) or 0)
            except Exception as exc:
                logger.warning(f"تعذر استعادة عناصر الطابور اليتيمة: {exc}")
        if restored > 0:
            self.window._append_log(f"تمت استعادة {restored} عنصر قابل للتشغيل")
            self.window._save_session()
        if not self._review_queue_duplicates_before_start():
            self._set_queue_runtime_state(running=False, paused=False)
            self.window._refresh_downloads_list()
            return
        self.window._switch_view("downloads")
        self.window._set_downloads_filter("active")
        started_via_manager = False
        if queue_manager is not None:
            try:
                if bool(getattr(queue_manager, "is_running", False)) and bool(getattr(queue_manager, "is_paused", False)):
                    queue_manager.resume_queue()
                elif not bool(getattr(queue_manager, "is_running", False)):
                    queue_manager.start_queue()
                started_via_manager = bool(getattr(queue_manager, "is_running", False))
            except Exception as exc:
                logger.warning(f"تعذر بدء الطابور عبر QueueManager: {exc}")
        running_value = started_via_manager if queue_manager is not None else True
        if (
            queue_manager is not None
            and not running_value
            and self.window._active_workers_count() == 0
            and self._queue_requires_controller_evaluation(
                queue_manager,
                active_worker_ids=active_worker_ids,
            )
        ):
            running_value = True
        self._set_queue_runtime_state(running=running_value, paused=False)
        self.process_parallel_queue()
        QTimer.singleShot(120, lambda: self.window._set_downloads_filter("active"))

    def pause_queue_download(self):
        self.window.queue_manager.pause_queue()
        self._set_queue_runtime_state(paused=True)
        with self.window._active_workers_lock:
            workers = list(self.window.active_workers.values())
        for worker in workers:
            worker.stop()
        self.window._info(_("Queue paused"))

    def resume_queue_download(self):
        queue_manager = getattr(self.window, "queue_manager", None)
        with self.window._active_workers_lock:
            active_worker_ids = {
                wid for wid in self.window.active_workers.keys() if isinstance(wid, int)
            }
        resumed_via_manager = False
        if queue_manager is not None:
            try:
                if bool(getattr(queue_manager, "is_running", False)) and bool(getattr(queue_manager, "is_paused", False)):
                    queue_manager.resume_queue()
                elif not bool(getattr(queue_manager, "is_running", False)):
                    queue_manager.start_queue()
                resumed_via_manager = bool(getattr(queue_manager, "is_running", False))
            except Exception as exc:
                logger.warning(f"تعذر استئناف الطابور عبر QueueManager: {exc}")
        running_value = resumed_via_manager if queue_manager is not None else True
        if (
            queue_manager is not None
            and not running_value
            and self.window._active_workers_count() == 0
            and self._queue_requires_controller_evaluation(
                queue_manager,
                active_worker_ids=active_worker_ids,
            )
        ):
            running_value = True
        self._set_queue_runtime_state(running=running_value, paused=False)
        self.process_parallel_queue()

    def pause_queue_item(self, item_index: int):
        item = self.window.queue_manager.get_task(item_index)
        if not item:
            return
        status = str(item.get("status", _STATUS_PENDING) or _STATUS_PENDING).lower()
        if status == _STATUS_CANCELLED:
            self.window._warn("العنصر ملغي، استخدم Retry أولاً")
            return
        if status == _STATUS_FAILED:
            self.window._warn("العنصر فشل، استخدم Retry أولاً")
            return
        if status == _STATUS_PAUSED:
            self.window._warn("العنصر متوقف مؤقتاً بالفعل")
            return
        if status == _STATUS_RUNNING:
            with self.window._active_workers_lock:
                worker = self.window.active_workers.get(item_index)
            self._mark_pause_requested(item_index)
            if worker is not None:
                worker.stop()
        self._update_queue_task(item_index, {"status": _STATUS_PAUSED, "next_retry_at": 0})
        self.window._append_log("تم إيقاف عنصر مؤقتاً")
        self.window._save_session()
        self.window._refresh_downloads_list()
        self.window._info(_("تم إيقاف العنصر مؤقتاً"))

    def _duplicate_task_key(self, task: dict) -> str:
        task_uuid = str((task or {}).get("task_uuid", "") or "").strip()
        if task_uuid:
            return f"uuid:{task_uuid}"
        return "|".join(
            [
                str((task or {}).get("url", "") or "").strip(),
                str((task or {}).get("title", "") or "").strip(),
                str((task or {}).get("format", "") or "").strip(),
                str((task or {}).get("quality", "") or "").strip(),
                str((task or {}).get("out_dir", "") or "").strip(),
            ]
        )

    def _review_queue_duplicates_before_start(self) -> bool:
        """
        Review duplicate candidates in one batch dialog before queue start.
        Returns False only when user cancels from the review dialog.
        """
        queue_manager = getattr(self.window, "queue_manager", None)
        if queue_manager is None or not hasattr(queue_manager, "get_queue_items_snapshot"):
            return True
        try:
            snapshot = list(queue_manager.get_queue_items_snapshot() or [])
        except Exception as exc:
            logger.debug(f"[BatchDup] failed to read queue snapshot: {exc}")
            return True
        review_candidates: list[dict] = []
        key_to_indices: dict[str, list[int]] = {}
        for idx, task in enumerate(snapshot):
            if not isinstance(task, dict):
                continue
            status = str(task.get("status", _STATUS_PENDING) or _STATUS_PENDING).lower()
            if status not in {_STATUS_PENDING, _STATUS_QUEUED, ""}:
                continue
            url = str(task.get("url", "") or "").strip()
            if not url:
                continue
            review_candidates.append(task)
            key = self._duplicate_task_key(task)
            key_to_indices.setdefault(key, []).append(idx)
        if not review_candidates:
            self._batch_duplicate_allowed_urls = set()
            return True
        self._batch_duplicate_allowed_urls = set()
        self._last_batch_duplicate_review_cancelled = False
        allowed, skipped = self.show_batch_duplicate_review(
            review_candidates,
            allowed_url_set=self._batch_duplicate_allowed_urls,
        )
        if self._last_batch_duplicate_review_cancelled:
            self.window._append_log("تم إلغاء بدء الطابور من نافذة مراجعة التكرارات.")
            return False
        skipped_count = 0
        for task in skipped:
            key = self._duplicate_task_key(task)
            index_candidates = key_to_indices.get(key) or []
            if not index_candidates:
                continue
            queue_index = index_candidates.pop(0)
            updated = queue_manager.update_task_fields(
                queue_index,
                {"status": _STATUS_PAUSED, "next_retry_at": 0},
                emit_changed=False,
            )
            if updated:
                skipped_count += 1
        if skipped_count:
            if hasattr(queue_manager, "queue_changed"):
                queue_manager.queue_changed.emit()
            self.window._append_log(f"تم تخطي {skipped_count} عنصر مكرر (Batch Review).")
            self.window._save_session()
        return True

    def resume_queue_item(self, item_index: int):
        item = self.window.queue_manager.get_task(item_index)
        if not item:
            return
        if str(item.get("status", _STATUS_PENDING) or _STATUS_PENDING).lower() != _STATUS_PAUSED:
            self.window._warn("العنصر ليس في وضع الإيقاف المؤقت")
            return
        self._update_queue_task(item_index, {"status": _STATUS_PENDING, "next_retry_at": 0})
        self.window._append_log("تم استكمال عنصر من الطابور")
        if not self._queue_is_running() and self.window._active_workers_count() == 0:
            self._set_queue_runtime_state(running=True, paused=False)
        self.window._save_session()
        self.window._switch_view("downloads")
        self.window._set_downloads_filter("active")
        self.process_parallel_queue()

    def cancel_queue_item(self, item_index: int):
        item = self.window.queue_manager.get_task(item_index)
        if not item:
            return
        status = str(item.get("status", _STATUS_PENDING) or _STATUS_PENDING).lower()
        if status == _STATUS_CANCELLED:
            self.window._warn("العنصر ملغي بالفعل")
            return
        if status == _STATUS_RUNNING:
            with self.window._active_workers_lock:
                worker = self.window.active_workers.get(item_index)
            self._mark_cancel_requested(item_index)
            if worker is not None:
                worker.stop()
        self._update_queue_task(item_index, {"status": _STATUS_CANCELLED, "next_retry_at": 0})
        self.window._append_log("تم إلغاء عنصر من الطابور")
        self.window._save_session()
        self.window._refresh_downloads_list()
        self.window._info(_("تم إلغاء العنصر"))
        if self._queue_is_running() and not self._queue_is_paused():
            QTimer.singleShot(250, self.process_parallel_queue)

    def delete_queue_item(self, item_index: int):
        item = self.window.queue_manager.get_task(item_index)
        if not item:
            return
        status = str(item.get("status", _STATUS_PENDING) or _STATUS_PENDING).lower()
        if status == _STATUS_RUNNING:
            self.window._warn("مينفعش حذف عنصر شغال. اعمل Cancel الأول.")
            return
        self._update_queue_task(item_index, {"status": _STATUS_DELETED, "next_retry_at": 0})
        self.window._save_session()
        self.window._refresh_downloads_list()

    def retry_queue_item(self, item_index: int):
        item = self.window.queue_manager.get_task(item_index)
        if not item:
            return
        status = str(item.get("status", _STATUS_PENDING) or _STATUS_PENDING).lower()
        if status not in _RETRYABLE_QUEUE_STATUSES:
            self.window._warn("العنصر لا يحتاج Retry حالياً")
            return
        self._update_queue_task(item_index, {"status": _STATUS_PENDING, "retry_count": 0, "next_retry_at": 0})
        self.window._append_log("تمت إعادة عنصر للطابور يدوياً")
        if not self._queue_is_running() and self.window._active_workers_count() == 0:
            self._set_queue_runtime_state(running=True, paused=False)
        self.window._save_session()
        self.window._switch_view("downloads")
        self.window._set_downloads_filter("active")
        self.process_parallel_queue()

    def locate_queue_item_file(self, item_index: int) -> bool:
        item = self.window.queue_manager.get_task(item_index)
        if not isinstance(item, dict):
            return False
        candidate = str(item.get("file_path") or item.get("last_output_path") or item.get("out_dir") or "").strip()
        if not candidate:
            self.window._warn("لا يوجد مسار متاح لهذا العنصر")
            return False
        try:
            self.window._open_folder(candidate)
            return True
        except Exception as exc:
            logger.debug(f"تعذر فتح مسار العنصر {item_index}: {exc}")
            self.window._warn("تعذر فتح مسار الملف")
            return False

    def _update_history_path_cache_for_task(self, task: dict, new_path: str) -> None:
        history = self.window.stats.get("download_history", [])
        if not isinstance(history, list):
            return
        task_url = str(task.get("url", "") or "").strip()
        task_title = str(task.get("title", "") or "").strip()
        for entry in reversed(history):
            if not isinstance(entry, dict):
                continue
            if task_url and str(entry.get("url", "") or "").strip() != task_url:
                continue
            if task_title and str(entry.get("title", "") or "").strip() != task_title:
                continue
            entry["file_path"] = str(new_path)
            break

    def relocate_queue_item_file(self, item_index: int) -> bool:
        task = self.window.queue_manager.get_task(item_index)
        if not isinstance(task, dict):
            return False
        current_path = str(task.get("file_path") or task.get("last_output_path") or "").strip()
        initial_dir = ""
        if current_path:
            if os.path.isfile(current_path):
                initial_dir = os.path.dirname(current_path)
            else:
                initial_dir = current_path if os.path.isdir(current_path) else os.path.dirname(current_path)
        if not initial_dir:
            initial_dir = str(task.get("out_dir", "") or "").strip()
        if not initial_dir:
            initial_dir = default_download_dir()
        selected_path, _ = QFileDialog.getOpenFileName(
            self.window,
            "Select relocated file",
            initial_dir,
            "All Files (*)",
        )
        selected_path = str(selected_path or "").strip()
        if not selected_path:
            return False
        selected_path = os.path.abspath(selected_path)
        if not os.path.isfile(selected_path):
            self.window._warn("الملف المختار غير موجود")
            return False
        fields = {
            "file_path": selected_path,
            "last_output_path": selected_path,
            "file_missing": False,
            "integrity_state": "present",
            "integrity_note": "",
        }
        if not self._update_queue_task(item_index, fields):
            self.window._warn("تعذر تحديث مسار الملف للعُنصر")
            return False
        self._update_history_path_cache_for_task(task, selected_path)
        self.window._append_log(f"تم ربط الملف من جديد: {os.path.basename(selected_path)}")
        self.window._save_session()
        self.window._refresh_downloads_list()
        return True

    def redownload_queue_item(self, item_index: int) -> bool:
        task = self.window.queue_manager.get_task(item_index)
        if not isinstance(task, dict):
            return False
        status = str(task.get("status", _STATUS_PENDING) or _STATUS_PENDING).lower()
        if status == _STATUS_RUNNING:
            self.window._warn("لا يمكن إعادة التحميل أثناء تشغيل العنصر")
            return False
        fields = {
            "status": _STATUS_PENDING,
            "retry_count": 0,
            "next_retry_at": 0,
            "progress": 0.0,
            "speed": "--",
            "eta": "--:--",
            "error_msg": "",
            "file_missing": False,
            "integrity_state": "",
            "integrity_note": "",
        }
        updated = self._update_queue_task(item_index, fields)
        if not updated:
            return False
        self.window._append_log("تمت إضافة العنصر لإعادة التحميل")
        if not self._queue_is_running() and self.window._active_workers_count() == 0:
            self._set_queue_runtime_state(running=True, paused=False)
        self.window._save_session()
        self.window._switch_view("downloads")
        self.window._set_downloads_filter("active")
        self.process_parallel_queue()
        return True

    def process_parallel_queue(self):
        if not self._queue_is_running() or self._queue_is_paused():
            return
        self.window._prune_inactive_workers()
        effective_max = max(1, int(self.window.max_concurrent or 1))
        with self.window._active_workers_lock:
            workers_snapshot = list(self.window.active_workers.items())
        active_worker_ids = {
            wid
            for wid, w in workers_snapshot
            if isinstance(wid, int) and w is not None and (w.isRunning() or not w.isFinished())
        }
        active_count = len(active_worker_ids)
        if active_count >= effective_max:
            return
        slots_available = effective_max - active_count
        priority = self._queue_priority()
        now_ts = datetime.now().timestamp()
        queue_manager = getattr(self.window, "queue_manager", None)
        if queue_manager is None:
            return
        if hasattr(queue_manager, "dispatch_parallel_ready_tasks"):
            plan = queue_manager.dispatch_parallel_ready_tasks(
                slots_available,
                active_worker_ids=active_worker_ids,
                priority=priority,
                task_size_getter=self._estimate_task_bytes,
                now_ts=now_ts,
                include_queue_items=False,
            )
        elif hasattr(queue_manager, "plan_parallel_start"):
            plan = queue_manager.plan_parallel_start(
                slots_available,
                active_worker_ids=active_worker_ids,
                priority=priority,
                task_size_getter=self._estimate_task_bytes,
                now_ts=now_ts,
                include_queue_items=False,
            )
            fallback_attempted = False
            dispatched_indices = []
            for queue_index in list(plan.get("pending_indices", []) or []):
                task = queue_manager.get_task(queue_index) if hasattr(queue_manager, "get_task") else None
                if not isinstance(task, dict):
                    continue
                fallback_attempted = True
                if self.start_download_from_queue(task, queue_index):
                    dispatched_indices.append(queue_index)
            fallback_plan = dict(plan)
            fallback_plan["dispatched_indices"] = dispatched_indices
            if fallback_attempted:
                if dispatched_indices:
                    self.window._refresh_downloads_list()
                return
            plan = fallback_plan
        else:
            return
        status_counts = dict(plan.get("status_counts", {}) or {})
        dispatched_indices = list(plan.get("dispatched_indices", []) or [])
        next_retry_ts = plan.get("next_retry_ts")

        if not dispatched_indices and active_count == 0:
            if next_retry_ts is not None:
                delay_ms = max(300, int((next_retry_ts - now_ts) * 1000))
                QTimer.singleShot(delay_ms, self.process_parallel_queue)
                self.window._set_status("بانتظار إعادة المحاولة")
                self.window._refresh_downloads_list()
                return
            paused_count = int(status_counts.get(_STATUS_PAUSED, 0) or 0)
            cancelled_count = int(status_counts.get(_STATUS_CANCELLED, 0) or 0)
            failed_count = int(status_counts.get(_STATUS_FAILED, 0) or 0)
            if not status_counts:
                # Backward-compat for older plan payloads that don't expose status_counts.
                queue_items = list(plan.get("queue_items", []) or [])
                paused_count = 0
                cancelled_count = 0
                failed_count = 0
                for item in queue_items:
                    status_value = str((item or {}).get("status", "")).lower()
                    if status_value == _STATUS_PAUSED:
                        paused_count += 1
                    elif status_value == _STATUS_CANCELLED:
                        cancelled_count += 1
                    elif status_value == _STATUS_FAILED:
                        failed_count += 1
            self._set_queue_runtime_state(running=False, paused=False)
            if paused_count > 0 or cancelled_count > 0 or failed_count > 0:
                self.window._set_status("لا توجد عناصر قابلة للتشغيل")
            else:
                self.window._set_status("جاهز")
            try:
                sv = getattr(self.window, "search_view", None)
                bar = getattr(sv, "progress_bar", None) if sv is not None else None
                container = getattr(sv, "status_container", None) if sv is not None else None
                if container is not None:
                    container.hide()
                if bar is not None:
                    bar.setValue(0)
                    bar.set_status("idle")
            except Exception:
                pass
            self.window._refresh_downloads_list()
            if paused_count > 0 or cancelled_count > 0 or failed_count > 0:
                self.window._warn("الطابور لا يحتوي على عناصر Pending. استخدم Retry/Resume أو أضف عناصر جديدة.")
            else:
                self.window._info("اكتمل تنفيذ الطابور")
            return
        self.window._refresh_downloads_list()

    def start_download_worker(self, task: DownloadTask, worker_id=None):
        wid = worker_id if worker_id is not None else f"single_{datetime.now().timestamp()}"
        if not isinstance(task, dict):
            self.window._warn("تعذر بدء التحميل: بيانات المهمة غير صالحة")
            return False
        out_dir = self._resolve_task_out_dir(task)
        if not self.check_storage_guard(out_dir, pause_on_low=self.window._active_workers_count() > 0 or self._queue_is_running()):
            self._sync_task_fields(wid, task, {"status": _STATUS_PAUSED, "next_retry_at": 0})
            self.window._save_session()
            self.window._refresh_downloads_list()
            return False
        os.makedirs(out_dir, exist_ok=True)
        effective_bandwidth_limit_kbps = self._resolve_effective_bandwidth_limit_kbps(wid, task)
        self._sync_task_fields(wid, task, {"out_dir": out_dir, "bandwidth_limit_kbps": effective_bandwidth_limit_kbps}, emit_changed=False)
        try:
            dup = self.get_duplicate_report(task, force=True)
            if dup["is_duplicate"]:
                task_url = str(task.get("url", "") or "").strip()
                allow_via_batch = (
                    isinstance(wid, int)
                    and bool(task_url)
                    and task_url in self._batch_duplicate_allowed_urls
                )
                if allow_via_batch:
                    self.window._append_log("تم السماح بعنصر مكرر بناءً على Batch Review.")
                    self.window._info("⚠️ تكرار محتمل: تم المتابعة حسب قرارك في المراجعة المجمعة.")
                else:
                    # Single-task path or queue item outside batch approval.
                    # Show the per-task confirmation.
                    existing = dup.get("url_duplicate") or {}
                    local = dup.get("local_files", [])
                    visual = dup.get("visual_duplicate") or {}
                    msg_parts = []
                    if existing:
                        msg_parts.append(f"سبق تحميله بتاريخ {existing.get('timestamp', '')[:10]}")
                    if local:
                        msg_parts.append(f"يوجد ملف محلي: {os.path.basename(local[0])}")
                    if isinstance(visual, dict) and visual:
                        visual_title = str(visual.get("title", "") or "").strip()
                        visual_distance = int(visual.get("distance", 0) or 0)
                        if visual_title:
                            msg_parts.append(f"تشابه بصري مع: {visual_title} (distance={visual_distance})")
                        else:
                            msg_parts.append(f"تشابه بصري منخفض المسافة (distance={visual_distance})")
                    question_text = "تم اكتشاف تكرار محتمل.\n" + "\n".join(f"- {part}" for part in msg_parts if part)
                    response = QMessageBox.question(
                        self.window,
                        "تكرار محتمل",
                        question_text + "\n\nهل تريد المتابعة رغم ذلك؟",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    if response != QMessageBox.StandardButton.Yes:
                        self._sync_task_fields(wid, task, {"status": _STATUS_PAUSED, "next_retry_at": 0})
                        self.window._append_log("تم إيقاف العنصر بسبب تكرار محتمل بانتظار تأكيد المستخدم")
                        self.window._refresh_downloads_list()
                        return False
                    self.window._info(f"⚠️ تحذير: تكرار! {' | '.join(msg_parts)}")
        except Exception as exc:
            logger.warning(f"فشل فحص التكرار للمهمة الحالية: {exc}")
        self.window._set_status("جاري التحميل")
        redacted_task_url = redact_url(task.get("url", ""))
        self.window._append_log(f"بدء التحميل: {redacted_task_url}")
        settings_form = self._get_settings_form()
        task_merge_opts = task.get("merge_opts")
        merge_opts = dict(task_merge_opts) if isinstance(task_merge_opts, dict) else self._build_merge_opts(settings_form)
        verify_checksum = bool(task.get("verify_checksum", settings_form.get("verify_checksum", False)))
        virus_scan_after_download = bool(task.get("virus_scan_after_download", settings_form.get("virus_scan_after_download", False)))
        use_ytdlp_api = bool(task.get("use_ytdlp_api", settings_form.get("use_ytdlp_api", False)))
        embed_subs = bool(task.get("embed_subs", settings_form.get("embed_subs", True)))
        split_chapters = bool(task.get("split_chapters", settings_form.get("split_chapters", False)))
        normalize_audio_postprocess = bool(
            task.get("normalize_audio_postprocess", settings_form.get("normalize_audio_postprocess", False))
        )
        clean_metadata = bool(task.get("clean_metadata", settings_form.get("clean_metadata", getattr(self.window, "clean_metadata_enabled", True))))
        use_native_engine = bool(task.get("use_native_engine", settings_form.get("use_native_engine", False)))
        rename_template = str(task.get("rename_template", settings_form.get("rename_template", "Default")) or "Default")
        worker = DownloadWorker(
            target_url=task["url"],
            out_dir=out_dir,
            mode=task["mode"],
            quality=task["quality"],
            fmt=task["format"],
            subtitle_lang=task.get("subtitle", "None"),
            start_time=task.get("start_time", ""),
            end_time=task.get("end_time", ""),
            retries=task.get("retries", 3),
            retry_delay_seconds=task.get("auto_retry_delay_seconds", self.window.auto_retry_delay_seconds),
            use_aria2=task.get("use_aria2", True),
            cookies_file=self.window.cookies_path,
            cookies_from_browser=task.get("cookies_from_browser", settings_form.get("cookies_from_browser", "none")),
            embed_subs=embed_subs,
            split_chapters=split_chapters,
            rename_template=rename_template,
            channel=task.get("channel", ""),
            verify_checksum=verify_checksum,
            virus_scan_after_download=virus_scan_after_download,
            normalize_audio_postprocess=normalize_audio_postprocess,
            bandwidth_limit_kbps=effective_bandwidth_limit_kbps,
            use_ytdlp_api=use_ytdlp_api,
            clean_metadata=clean_metadata,
            frozen_title=str(task.get("title", "") or ""),
            merge_opts=merge_opts,
            use_native_engine=use_native_engine,
            is_live_hint=bool(task.get("is_live", False)),
            was_live_hint=bool(task.get("was_live", False)),
            live_status_hint=str(task.get("live_status", "") or ""),
            format_decision_engine=self._format_decision_engine,
        )
        worker.worker_id = wid
        worker.extra_args = anti_detection_engine.get_yt_dlp_options()
        worker.sponsorblock_enabled = bool(task.get("sponsorblock_enabled", settings_form.get("sponsorblock_enabled", False)))
        worker.whisper_fallback_enabled = bool(task.get("whisper_fallback", settings_form.get("whisper_fallback", False)))
        try:
            transport_profile = anti_detection_engine.get_transport_fingerprint()
            worker.tls_transport_profile = str(transport_profile.get("transport_impersonate", "") or "").strip()
        except Exception:
            worker.tls_transport_profile = ""
        self._register_active_worker(wid, worker)
        if self.window._display_progress_wid is None:
            self.window._display_progress_wid = wid
        try:
            sv = getattr(self.window, "search_view", None)
            if sv is not None and hasattr(sv, "status_container"):
                sv.status_container.show()
            bar = getattr(sv, "progress_bar", None) if sv is not None else None
            if bar is not None:
                bar.set_status(_STATUS_DOWNLOADING)
        except Exception:
            pass
        self._connect_download_worker_signals(wid, worker)
        worker.start()
        logger.info(f"Started worker {wid} for {redacted_task_url}")
        return True

    def _resolve_task_out_dir(self, task: DownloadTask) -> str:
        out_dir = str((task or {}).get("out_dir", "")).strip()
        if out_dir:
            return out_dir
        try:
            out_dir = str(self.window.search_view.out_dir_input.text() or "").strip()
        except Exception:
            out_dir = ""
        return out_dir or default_download_dir()

    def _resolve_effective_bandwidth_limit_kbps(self, wid, task: DownloadTask) -> int:
        value = int((task or {}).get("bandwidth_limit_kbps", 0) or 0)
        if self.window.bandwidth_scheduler_enabled:
            value = int(self.window._current_bandwidth_limit_kbps() or 0)
            self._sync_task_fields(wid, task, {"bandwidth_limit_kbps": value}, emit_changed=False)
        return value

    def _get_settings_form(self) -> dict:
        sv = getattr(self.window, "settings_view", None)
        if sv is None or not hasattr(sv, "get_form_settings"):
            return {}
        try:
            form = sv.get_form_settings()
        except Exception:
            return {}
        return form if isinstance(form, dict) else {}

    def _build_merge_opts(self, settings_form: dict) -> dict:
        form = settings_form if isinstance(settings_form, dict) else {}
        if not bool(form.get("custom_merge_enabled", False)):
            return {}
        hw_encoder = str(form.get("custom_merge_hw_encoder", "off") or "off").strip().lower()
        if hw_encoder not in {"off", "auto", "nvenc", "qsv", "amf"}:
            hw_encoder = "off"
        video_preset = str(form.get("custom_merge_video_preset", "p5") or "p5").strip().lower()
        if not video_preset:
            video_preset = "p5"
        return {
            "enabled": True,
            "video_codec": str(form.get("custom_merge_video_codec", "copy") or "copy"),
            "video_crf": int(form.get("custom_merge_video_crf", 23) or 23),
            "audio_codec": str(form.get("custom_merge_audio_codec", "aac") or "aac"),
            "audio_bitrate": str(form.get("custom_merge_audio_bitrate", "192k") or "192k"),
            "hw_encoder": hw_encoder,
            "force_reencode": bool(form.get("custom_merge_force_reencode", False)),
            "video_preset": video_preset,
        }

    def describe_resume_snapshot(self, task: DownloadTask | None) -> str:
        resume = (task or {}).get("resume")
        if not isinstance(resume, dict):
            return ""
        partials_count = max(0, int(resume.get("partials_count", 0) or 0))
        partials_total = max(0, int(resume.get("partials_total_bytes", 0) or 0))
        if partials_count <= 0 and partials_total <= 0:
            return ""
        parts = []
        if partials_count > 0:
            parts.append(f"{partials_count} جزء")
        if partials_total > 0:
            parts.append(format_bytes(partials_total))
        output_path = str(resume.get("output_path", "") or (task or {}).get("last_output_path", "") or "").strip()
        if output_path:
            parts.append(os.path.basename(output_path))
        return f"استئناف جاهز: {' | '.join(parts)}"

    def _duplicate_cache_key(self, task: DownloadTask | None) -> tuple:
        return (
            str((task or {}).get("url", "") or "").strip(),
            str((task or {}).get("out_dir", "") or "").strip(),
            str((task or {}).get("title", "") or "").strip(),
        )

    def get_duplicate_report(self, task: DownloadTask | None, *, force: bool = False, skip_visual: bool = False) -> dict:
        cache_key = self._duplicate_cache_key(task)
        now = time.time()
        cached = self._duplicate_report_cache.get(cache_key)
        if not force and isinstance(cached, tuple) and len(cached) == 2:
            cached_ts, cached_report = cached
            if (now - float(cached_ts or 0.0)) <= 20.0 and isinstance(cached_report, dict):
                return dict(cached_report)
        try:
            report = build_duplicate_report(
                url=str((task or {}).get("url", "") or ""),
                out_dir=str((task or {}).get("out_dir", "") or self.window.search_view.out_dir_input.text() or ""),
                title=str((task or {}).get("title", "") or ""),
                thumbnail="" if skip_visual else str((task or {}).get("thumbnail", "") or ""),
            )
        except Exception as exc:
            logger.warning(f"فشل فحص التكرار للمهمة الحالية: {exc}")
            report = {"url_duplicate": None, "local_files": [], "visual_duplicate": None, "is_duplicate": False}
        self._duplicate_report_cache[cache_key] = (now, dict(report))
        while len(self._duplicate_report_cache) > 160:
            stale_key = next(iter(self._duplicate_report_cache.keys()))
            self._duplicate_report_cache.pop(stale_key, None)
        return dict(report)

    def describe_duplicate_report(self, task: DownloadTask | None) -> str:
        report = self.get_duplicate_report(task)
        if not bool(report.get("is_duplicate")):
            return ""
        parts = []
        url_dup = report.get("url_duplicate") or {}
        local_files = report.get("local_files") or []
        visual_dup = report.get("visual_duplicate") or {}
        if isinstance(url_dup, dict) and url_dup:
            ts = str(url_dup.get("timestamp", "") or "").strip()
            if ts:
                parts.append(f"الرابط موجود بالسجل {ts[:10]}")
            else:
                parts.append("الرابط موجود بالسجل")
        if local_files:
            parts.append(f"{len(local_files)} ملف محلي مشابه")
        if isinstance(visual_dup, dict) and visual_dup:
            title = str(visual_dup.get("title", "") or "").strip()
            distance = int(visual_dup.get("distance", 0) or 0)
            if title:
                parts.append(f"تشابه بصري مع: {title[:36]} (d={distance})")
            else:
                parts.append(f"تشابه بصري منخفض المسافة (d={distance})")
        return f"تكرار محتمل: {' | '.join(parts)}" if parts else "تكرار محتمل"

    def _collect_duplicate_tasks(self, tasks: list) -> list:
        """
        Scan *tasks* and return a list of (task, report) tuples for tasks
        that have at least one detected duplicate.  Non-duplicates are skipped.
        Does NOT open any dialog - call show_batch_duplicate_review() for that.
        """
        entries = []
        for task in tasks or []:
            if not isinstance(task, dict):
                continue
            try:
                report = self.get_duplicate_report(task, force=True, skip_visual=True)
            except Exception as exc:
                import logging as _lg
                _lg.getLogger("SnapDownloader").debug(
                    f"[BatchDup] report failed for {task.get('url', '')}: {exc}"
                )
                continue
            if bool(report.get("is_duplicate")):
                entries.append((task, report))
        return entries

    def show_batch_duplicate_review(
        self,
        tasks: list,
        *,
        allowed_url_set=None,
    ) -> tuple:
        """
        Collect all duplicates from *tasks* and, if any are found, open a
        single BatchDuplicateDialog.  Returns (allowed, skipped).
        If the user cancels, returns ([], tasks) to abort the batch.
        """
        self._last_batch_duplicate_review_cancelled = False
        clean_tasks = [t for t in (tasks or []) if isinstance(t, dict)]
        dup_entries = self._collect_duplicate_tasks(clean_tasks)
        non_dup_tasks = [t for t in clean_tasks if not any(t is e[0] for e in dup_entries)]

        if not dup_entries:
            return list(clean_tasks), []

        dialog = BatchDuplicateDialog(dup_entries, parent=self.window)
        try:
            from PySide6.QtWidgets import QDialog
        except ImportError:
            from PyQt6.QtWidgets import QDialog

        result_code = dialog.exec()
        try:
            accepted_code = QDialog.DialogCode.Accepted
        except AttributeError:
            accepted_code = 1

        if result_code != accepted_code:
            self._last_batch_duplicate_review_cancelled = True
            return [], list(clean_tasks)

        allowed_from_dups = dialog.get_allowed_tasks()
        skipped_from_dups = dialog.get_skipped_tasks()

        if allowed_url_set is not None:
            for t in allowed_from_dups:
                url = str((t or {}).get("url", "") or "").strip()
                if url:
                    allowed_url_set.add(url)

        allowed = non_dup_tasks + allowed_from_dups
        skipped = skipped_from_dups
        n_skipped = len(skipped_from_dups)
        if n_skipped:
            import logging as _lg
            _lg.getLogger("SnapDownloader").info(
                f"Batch duplicate review: {len(allowed_from_dups)} approved, {n_skipped} skipped"
            )
        return allowed, skipped


    def set_queue_item_bandwidth_limit(self, item_index: int, limit_kbps: int) -> bool:
        item = self.window.queue_manager.get_task(item_index)
        if not item:
            return False
        limit = max(0, int(limit_kbps or 0))
        self._sync_task_fields(item_index, item, {"bandwidth_limit_kbps": limit, "next_retry_at": 0})
        status = str(item.get("status", _STATUS_PENDING) or _STATUS_PENDING).lower()
        limit_text = self.window._format_bandwidth_limit(limit)
        if status == _STATUS_RUNNING:
            with self.window._active_workers_lock:
                worker = self.window.active_workers.get(item_index)
            self._mark_bandwidth_restart_requested(item_index)
            self._mark_pause_requested(item_index)
            if worker is not None:
                try:
                    worker.bandwidth_limit_kbps = limit
                except Exception:
                    pass
                try:
                    worker.stop()
                except Exception as exc:
                    logger.debug(f"تعذر إعادة تشغيل worker {item_index} لتطبيق حد السرعة: {exc}")
            self.window._append_log(f"سيتم تطبيق حد السرعة الجديد للعنصر بعد الاستئناف: {limit_text}")
            self.window._info(f"سيتم تطبيق حد السرعة الجديد: {limit_text}")
        else:
            self.window._append_log(f"تم تحديث حد السرعة للعنصر إلى {limit_text}")
        self.window._save_session()
        self.window._refresh_downloads_list()
        return True

    def _register_active_worker(self, wid, worker):
        with self.window._active_workers_lock:
            self.window.active_workers[wid] = worker

    def _mark_pause_requested(self, wid: int):
        mark_fn = getattr(self.window, "_mark_pause_requested", None)
        if callable(mark_fn):
            mark_fn(wid)
            return
        with self.window._active_workers_lock:
            self.window.pause_requested_workers.add(wid)

    def _mark_cancel_requested(self, wid: int):
        mark_fn = getattr(self.window, "_mark_cancel_requested", None)
        if callable(mark_fn):
            mark_fn(wid)
            return
        with self.window._active_workers_lock:
            self.window.cancel_requested_workers.add(wid)

    def _mark_bandwidth_restart_requested(self, wid: int):
        mark_fn = getattr(self.window, "_mark_bandwidth_restart_requested", None)
        if callable(mark_fn):
            mark_fn(wid)
            return
        with self.window._active_workers_lock:
            self.window.bandwidth_restart_requested_workers.add(wid)

    def _take_worker_request_state(self, wid: int) -> tuple[bool, bool, bool]:
        take_fn = getattr(self.window, "_take_worker_request_state", None)
        if callable(take_fn):
            return take_fn(wid)
        with self.window._active_workers_lock:
            cancelled_by_user = wid in self.window.cancel_requested_workers
            bandwidth_restart_requested = wid in self.window.bandwidth_restart_requested_workers
            paused_by_user = wid in self.window.pause_requested_workers
            if cancelled_by_user:
                self.window.cancel_requested_workers.discard(wid)
            if bandwidth_restart_requested:
                self.window.bandwidth_restart_requested_workers.discard(wid)
            if paused_by_user:
                self.window.pause_requested_workers.discard(wid)
        return cancelled_by_user, bandwidth_restart_requested, paused_by_user

    def _confirm_post_action(self, action: str) -> bool:
        action_name = str(action or "").strip().lower()
        if action_name != "shutdown":
            return True
        result = QMessageBox.question(
            self.window,
            "تأكيد إيقاف الجهاز",
            "تم طلب إجراء إيقاف الجهاز بعد اكتمال التحميل. هل تريد المتابعة؟",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return result == QMessageBox.StandardButton.Yes

    def _persist_progress_snapshot(self, wid, percent, speed, eta, status, force: bool = False):
        if not isinstance(wid, int):
            return
        task = self.window.queue_manager.get_task(wid)
        task_uuid = str((task or {}).get("task_uuid", "") or "").strip()
        try:
            progress_value = float(percent or 0.0)
        except (TypeError, ValueError, OverflowError):
            progress_value = 0.0
        snapshot = {
            "progress": progress_value,
            "speed": str(speed or "--"),
            "eta": str(eta or "--:--"),
            "status": str(status or _STATUS_PENDING),
        }
        with self._db_progress_lock:
            previous = dict(self._db_progress_state.get(wid) or {})
        now = time.monotonic()
        should_write = force or not previous
        if not should_write:
            previous_progress = float(previous.get("progress", 0.0) or 0.0)
            previous_status = str(previous.get("status", _STATUS_PENDING) or _STATUS_PENDING)
            last_saved_at = float(previous.get("saved_at", 0.0) or 0.0)
            if snapshot["status"] != previous_status:
                should_write = True
            elif abs(snapshot["progress"] - previous_progress) >= self._db_progress_min_delta:
                should_write = True
            elif (now - last_saved_at) >= self._db_progress_min_interval_seconds:
                should_write = True
        if should_write:
            snapshot["saved_at"] = now
            snapshot["task_uuid"] = task_uuid
            snapshot["queue_index"] = wid
            self._queue_progress_db_write(wid, snapshot, flush_now=force)
        else:
            snapshot["saved_at"] = float(previous.get("saved_at", 0.0) or 0.0)
        with self._db_progress_lock:
            self._db_progress_state[wid] = snapshot

    def _queue_progress_db_write(self, wid: int, snapshot: dict, *, flush_now: bool = False):
        if not isinstance(wid, int) or not isinstance(snapshot, dict):
            return
        payload = {
            "queue_index": int(wid),
            "task_uuid": str(snapshot.get("task_uuid", "") or "").strip(),
            "progress": float(snapshot.get("progress", 0.0) or 0.0),
            "speed": str(snapshot.get("speed", "--") or "--"),
            "eta": str(snapshot.get("eta", "--:--") or "--:--"),
            "status": str(snapshot.get("status", _STATUS_PENDING) or _STATUS_PENDING),
        }
        with self._db_progress_lock:
            self._db_progress_dirty[wid] = payload
        if flush_now:
            self._flush_pending_progress_writes()
            return
        if not self._db_progress_flush_timer.isActive():
            self._db_progress_flush_timer.start(max(100, int(self._db_progress_flush_interval_ms)))

    def _flush_pending_progress_writes(self):
        with self._db_progress_lock:
            pending = list(self._db_progress_dirty.items())
        if not pending:
            return
        updates = [dict(payload) for _wid, payload in pending if isinstance(payload, dict)]
        if not updates:
            with self._db_progress_lock:
                self._db_progress_dirty.clear()
            return
        try:
            update_task_states_fast_batch(updates)
        except Exception as exc:
            logger.warning(f"تعذر flush مجمع لتقدم الطابور، سيتم المحاولة فردياً: {exc}")
            for wid, payload in pending:
                try:
                    update_task_state_fast(
                        payload.get("queue_index"),
                        payload.get("progress", 0.0),
                        payload.get("speed", "--"),
                        payload.get("eta", "--:--"),
                        payload.get("status", _STATUS_PENDING),
                        task_uuid=str(payload.get("task_uuid", "") or "").strip(),
                    )
                except Exception as item_exc:
                    logger.warning(f"تعذر حفظ تقدم العنصر {wid}: {item_exc}")
                    continue
                with self._db_progress_lock:
                    current_payload = self._db_progress_dirty.get(wid)
                    if current_payload == payload:
                        self._db_progress_dirty.pop(wid, None)
            with self._db_progress_lock:
                has_remaining = bool(self._db_progress_dirty)
            if has_remaining and not self._db_progress_flush_timer.isActive():
                self._db_progress_flush_timer.start(max(150, int(self._db_progress_flush_interval_ms)))
            return
        for wid, _payload in pending:
            with self._db_progress_lock:
                current_payload = self._db_progress_dirty.get(wid)
                if current_payload == _payload:
                    self._db_progress_dirty.pop(wid, None)

    def _flush_progress_snapshot(self, wid, task: DownloadTask | None = None, *, status: str | None = None):
        if not isinstance(wid, int):
            return
        with self._db_progress_lock:
            cached = dict(self._db_progress_state.get(wid) or {})
        task_data = task if isinstance(task, dict) else {}
        try:
            progress_value = float(task_data.get("progress", cached.get("progress", 0.0)) or 0.0)
        except (TypeError, ValueError, OverflowError):
            progress_value = 0.0
        speed_value = str(task_data.get("speed", cached.get("speed", "--")) or "--")
        eta_value = str(task_data.get("eta", cached.get("eta", "--:--")) or "--:--")
        status_value = str(status or task_data.get("status", cached.get("status", _STATUS_PENDING)) or _STATUS_PENDING)
        try:
            self._persist_progress_snapshot(wid, progress_value, speed_value, eta_value, status_value, force=True)
        except Exception:
            pass
        finally:
            with self._db_progress_lock:
                self._db_progress_state.pop(wid, None)

    def _update_queue_task(self, task_index, fields: dict, emit_changed: bool = True) -> bool:
        if not isinstance(task_index, int):
            return False
        if not isinstance(fields, dict) or not fields:
            return False
        return self.window.queue_manager.update_task_fields(task_index, fields, emit_changed=emit_changed)

    def _sync_task_fields(self, task_index, task: DownloadTask | None, fields: dict, emit_changed: bool = True) -> bool:
        if not isinstance(fields, dict) or not fields:
            return False
        updated = self._update_queue_task(task_index, fields, emit_changed=emit_changed)
        if isinstance(task, dict):
            task.update(fields)
        return updated

    def _connect_download_worker_signals(self, wid, worker):
        worker.progress.connect(lambda _, p, s, eta, w=wid: self.window._on_download_progress(w, p, s, eta))
        worker.log.connect(self.window._on_download_log)
        worker.state.connect(self.window._on_download_state)
        worker.finished.connect(lambda w=wid, ref=worker: self.window._on_worker_thread_finished(w, ref))
        if hasattr(worker, "output_path_changed"):
            worker.output_path_changed.connect(lambda p, w=wid: self._on_worker_output_path(w, p))
        if hasattr(worker, "resume_snapshot"):
            worker.resume_snapshot.connect(lambda payload, w=wid: self._on_worker_resume_snapshot(w, payload))
        if hasattr(worker, "engine_detected"):
            worker.engine_detected.connect(lambda engine, w=wid: self._on_worker_engine_detected(w, engine))

    def _on_worker_engine_detected(self, wid, engine_name: str):
        if not isinstance(wid, int) or not engine_name:
            return
        # Update the task in memory so it can be retrieved by the UI
        self._update_queue_task(wid, {"engine": str(engine_name)}, emit_changed=False)
        
        # Update the UI badge if it's currently rendered
        try:
            refs = self.window._active_download_card_refs.get(wid)
            if refs and "engine_label" in refs:
                lbl = refs["engine_label"]
                lbl.setText(str(engine_name))
                lbl.show()
        except Exception:
            pass

    def _on_worker_output_path(self, wid, path: str):
        if not isinstance(wid, int):
            return
        if not self._update_queue_task(wid, {"last_output_path": str(path or "").strip()}, emit_changed=False):
            return
        now = datetime.now().timestamp()
        last = float(self._resume_last_save_ts.get((wid, "path"), 0) or 0)
        if (now - last) >= 2.0:
            self._resume_last_save_ts[(wid, "path")] = now
            # Last output path is updated in memory but doesn't strictly need a full queue force-save immediately.
            # We rely on progress queue fast batch to pick up other state, removing _save_session() here.
            pass

    @staticmethod
    def _normalize_integrity_path(path: str) -> str:
        raw_path = str(path or "").strip()
        if not raw_path:
            return ""
        try:
            return os.path.normcase(os.path.abspath(raw_path))
        except Exception:
            return os.path.normcase(raw_path)

    def _track_completed_output(self, wid: int, path: str) -> None:
        if not isinstance(wid, int):
            return
        normalized_path = str(path or "").strip()
        if not normalized_path:
            return
        try:
            self._file_integrity_watcher.track_completed_file(wid, normalized_path)
        except Exception as exc:
            logger.debug(f"تعذر تتبع سلامة الملف {normalized_path}: {exc}")

    def _current_auto_categorize_settings(self) -> tuple[bool, str]:
        form = {}
        settings_view = getattr(self.window, "settings_view", None)
        if settings_view is not None and hasattr(settings_view, "get_form_settings"):
            try:
                form = settings_view.get_form_settings() or {}
            except Exception:
                form = {}
        enabled = bool(form.get("auto_categorize_downloads", DEFAULT_SETTINGS.get("auto_categorize_downloads", False)))
        mode = normalize_auto_categorize_mode(
            form.get("auto_categorize_mode", DEFAULT_SETTINGS.get("auto_categorize_mode", "off"))
        )
        if not enabled:
            mode = "off"
        return enabled, mode

    def _maybe_auto_categorize_output(self, file_path: str, task: DownloadTask | None) -> dict:
        source_path = str(file_path or "").strip()
        enabled, mode = self._current_auto_categorize_settings()
        if not source_path or not enabled or mode == "off":
            return {"moved": False, "file_path": source_path, "target_dir": "", "moved_paths": []}
        try:
            result = organize_download_output(source_path, mode, task if isinstance(task, dict) else None)
        except Exception as exc:
            logger.warning(f"فشل التنظيم التلقائي للملف {source_path}: {exc}")
            with suppress(Exception):
                self.window._append_log(f"تعذر تنظيم الملف تلقائيًا: {os.path.basename(source_path)}")
            return {"moved": False, "file_path": source_path, "target_dir": "", "moved_paths": []}
        if result.get("moved"):
            moved_paths = list(result.get("moved_paths", []) or [])
            target_dir = str(result.get("target_dir", "") or "").strip()
            moved_count = max(1, len(moved_paths))
            with suppress(Exception):
                self.window._append_log(
                    f"تم تنظيم الملف تلقائيًا إلى: {target_dir} ({moved_count} عنصر)"
                )
        return result

    def _on_tracked_file_missing(self, wid: int, path: str) -> None:
        if not isinstance(wid, int):
            return
        task = self.window.queue_manager.get_task(wid)
        if not isinstance(task, dict):
            return
        normalized_missing_path = self._normalize_integrity_path(path)
        candidate_paths = {
            self._normalize_integrity_path(task.get("file_path", "")),
            self._normalize_integrity_path(task.get("last_output_path", "")),
        }
        if normalized_missing_path and normalized_missing_path not in candidate_paths:
            return
        fields = {
            "file_missing": True,
            "integrity_state": "missing",
            "integrity_note": "Output file is missing from disk",
        }
        self._sync_task_fields(wid, task, fields, emit_changed=False)
        with suppress(Exception):
            self.window._append_log(f"⚠️ الملف لم يعد موجودًا على القرص: {os.path.basename(path)}")
        with suppress(Exception):
            self.window._refresh_downloads_list()

    def _on_worker_resume_snapshot(self, wid, payload: dict):
        if not isinstance(wid, int):
            return
        if isinstance(payload, dict):
            fields = {"resume": payload}
            try:
                fields["resume_json"] = json.dumps(payload, ensure_ascii=False)
            except Exception:
                fields["resume_json"] = ""
            if not self._update_queue_task(wid, fields, emit_changed=False):
                return
        now = datetime.now().timestamp()
        last = float(self._resume_last_save_ts.get((wid, "resume"), 0) or 0)
        if (now - last) >= 6.0:
            self._resume_last_save_ts[(wid, "resume")] = now
            task = self.window.queue_manager.get_task(wid)
            task_uuid = str((task or {}).get("task_uuid", "") or "").strip()
            # Fast DB write for resume buffer to avoid rewriting the entire queue JSON
            try:
                update_task_resume_snapshot(task_uuid, wid, fields.get("resume_json", ""))
            except Exception:
                pass

    def start_download_from_queue(self, task: DownloadTask, queue_index: int):
        if not isinstance(task, dict):
            self.window._warn("تعذر بدء التحميل: بيانات المهمة غير صالحة")
            return False
        if not str(task.get("url", "") or "").strip():
            self._update_queue_task(queue_index, {"status": _STATUS_FAILED, "error_msg": "Missing URL"})
            self.window._refresh_downloads_list()
            return False
        return self.start_download_worker(task, worker_id=queue_index)

    def on_worker_thread_finished(self, wid, worker):
        with self.window._active_workers_lock:
            existing = self.window.active_workers.get(wid)
            if existing is worker:
                del self.window.active_workers[wid]
            if self.window._display_progress_wid == wid:
                self.window._display_progress_wid = next(iter(self.window.active_workers.keys()), None)
        if self.window.current_worker is worker:
            self.window.current_worker = None
        self._speed_history.pop(wid, None)
        self._resume_last_save_ts.pop((wid, "path"), None)
        self._resume_last_save_ts.pop((wid, "resume"), None)
        # H-02: ensure thread is truly done before deleteLater to avoid zombie threads
        # We do the wait in a short background thread so we don't block the UI
        def _wait_then_schedule_cleanup():
            try:
                if not worker.isFinished():
                    worker.wait(3000)  # wait up to 3s
            except Exception as exc:
                logger.debug(f"[H-02] worker.wait() failed: {exc}")
            try:
                self._safe_delete_worker(worker)
            except Exception as exc:
                logger.debug(f"تعذر تنظيف worker بعد الانتهاء: {exc}")
        self._submit_post_download_task(
            _wait_then_schedule_cleanup,
            label="worker_cleanup",
            legacy_executor_attr="_worker_cleanup_executor",
        )

    def on_download_progress(self, wid, percent, speed, eta):
        if hasattr(self.window, "progress_bus") and self.window.progress_bus is not None:
            self.window.progress_bus.post(wid, percent, speed, eta)
        else:
            self.window.queue_manager.set_task_progress(wid, percent, speed, eta)
            self._process_download_progress(wid, percent, speed, eta)

    def _process_download_progress(self, wid, percent, speed, eta):
        try:
            self._persist_progress_snapshot(wid, percent, speed, eta, _STATUS_RUNNING)
        except Exception:
            pass
        raw_percent = float(percent or 0)
        if 0 < raw_percent < 1:
            value = 1
        else:
            value = max(0, min(100, int(round(raw_percent))))
        if self.window._display_progress_wid is None:
            self.window._display_progress_wid = wid
        speed_kib = None
        try:
            kbps_match = re.search(r"([\d.]+)([KMG])iB/s", str(speed))
            if kbps_match:
                val, unit = float(kbps_match.group(1)), kbps_match.group(2)
                speed_kib = val * {"K": 1, "M": 1024, "G": 1024 * 1024}.get(unit, 1)
        except Exception:
            speed_kib = None

        if wid == self.window._display_progress_wid:
            if hasattr(self.window.search_view, "update_progress"):
                self.window.search_view.update_progress(value)
            else:
                self.window.search_view.progress_bar.setValue(value)
            if hasattr(self.window.search_view, "update_speed"):
                self.window.search_view.update_speed(speed, speed_kib)
            else:
                self.window.search_view.speed_label.setText(f"Speed: {speed}")
            if hasattr(self.window.search_view, "update_eta"):
                self.window.search_view.update_eta(eta)
            else:
                self.window.search_view.eta_label.setText(f"ETA: {eta}")
            if hasattr(self.window.search_view, "update_status"):
                self.window.search_view.update_status(_STATUS_RUNNING)
        try:
            if speed_kib is not None:
                self._peak_speed_executor.submit(record_peak_speed, speed_kib)
                self.update_speed_history(wid, speed_kib)
                history = list(self.get_speed_history(wid))[-12:]
                if history:
                    avg_kib = float(sum(history)) / float(len(history))
                    task = self.window.queue_manager.get_task(wid)
                    remaining = 100.0 - float(percent or 0.0)
                    estimated_total_bytes = self._estimate_task_bytes(task or {})
                    if remaining > 0 and estimated_total_bytes > 0 and avg_kib > 0:
                        remaining_bytes = estimated_total_bytes * (remaining / 100.0)
                        eta_seconds = int(remaining_bytes / (avg_kib * 1024.0))
                        if eta_seconds > 0 and wid == self.window._display_progress_wid and hasattr(self.window.search_view, "update_eta"):
                            mins, secs = divmod(eta_seconds, 60)
                            hours, mins = divmod(mins, 60)
                            if hours > 0:
                                predicted = f"{hours:02d}:{mins:02d}:{secs:02d}"
                            else:
                                predicted = f"{mins:02d}:{secs:02d}"
                            self.window.search_view.update_eta(predicted)
        except Exception as exc:
            logger.debug(f"تعذر تسجيل peak speed: {exc}")
        if hasattr(self.window, "mini_window") and self.window.mini_window.isVisible():
            active_title = ""
            task = self.window.queue_manager.get_task(wid)
            if task:
                active_title = task.get("title", "")
            self.window.mini_window.update_progress(
                title=active_title,
                progress=value,
                speed=speed,
                eta=eta,
                total_active=self.window._active_workers_count(),
            )
        self.window._notify_tray_progress()

    def update_speed_history(self, wid, kbps: float) -> None:
        try:
            key = int(wid)
        except (TypeError, ValueError, OverflowError):
            return
        history = self._speed_history.get(key)
        if history is None:
            history = deque(maxlen=60)
            self._speed_history[key] = history
        try:
            value = float(kbps)
        except (TypeError, ValueError, OverflowError):
            value = 0.0
        history.append(max(0.0, value))
        self._evict_stale_speed_history(protected_key=key)

    def get_speed_history(self, wid) -> deque:
        try:
            key = int(wid)
        except (TypeError, ValueError, OverflowError):
            return deque(maxlen=60)
        return self._speed_history.get(key, deque(maxlen=60))

    def _evict_stale_speed_history(self, protected_key: int) -> None:
        if len(self._speed_history) <= self._speed_history_max_workers:
            return
        with self.window._active_workers_lock:
            active_ids = set(self.window.active_workers.keys())
        protected_ids = set(active_ids)
        protected_ids.add(int(protected_key))
        display_wid = getattr(self.window, "_display_progress_wid", None)
        if display_wid is not None:
            protected_ids.add(display_wid)
        stale_candidates = [wid for wid in self._speed_history.keys() if wid not in protected_ids]
        overflow = len(self._speed_history) - self._speed_history_max_workers
        for stale_wid in stale_candidates[: max(0, overflow)]:
            self._speed_history.pop(stale_wid, None)

    def _shutdown_worker(self, wid, worker):
        if worker is None:
            return
        try:
            if hasattr(worker, "request_stop"):
                worker.request_stop()
            elif hasattr(worker, "stop"):
                worker.stop()
            elif hasattr(worker, "cancel"):
                worker.cancel()
            elif hasattr(worker, "requestInterruption"):
                worker.requestInterruption()
        except Exception as exc:
            logger.debug(f"تعذر إرسال إشارة إيقاف للـ worker {wid}: {exc}")
        try:
            if hasattr(worker, "quit"):
                worker.quit()
        except Exception as exc:
            logger.debug(f"تعذر استدعاء quit للـ worker {wid}: {exc}")

    def _await_shutdown_worker(self, wid, worker, timeout_ms: int = 3000):
        if worker is None:
            return
        try:
            if hasattr(worker, "wait_for_stop"):
                worker.wait_for_stop(timeout_ms)
            elif hasattr(worker, "wait"):
                worker.wait(timeout_ms)
        except Exception as exc:
            logger.debug(f"تعذر wait للـ worker {wid}: {exc}")
        try:
            if hasattr(worker, "isRunning") and worker.isRunning():
                logger.warning(f"Worker {wid} is still running during shutdown after cooperative stop request")
        except Exception:
            pass
        try:
            self._safe_delete_worker(worker)
        except Exception as exc:
            logger.debug(f"تعذر deleteLater للـ worker {wid}: {exc}")

    def _safe_delete_worker(self, worker):
        if worker is None:
            return
        try:
            if hasattr(worker, "deleteLater"):
                from core.qt_compat import QObject, QThread, QApplication
                app = QApplication.instance()
                is_main = (app is not None and QThread.currentThread() == app.thread())
                if is_main or not isinstance(worker, QObject) or not run_on_qt_main_thread(worker.deleteLater):
                    worker.deleteLater()
        except Exception as exc:
            logger.debug(f"Failed to safely delete worker: {exc}")

    def shutdown(self):
        with self.window._active_workers_lock:
            active_workers = list(self.window.active_workers.items())
            self.window.active_workers.clear()
        for wid, worker in active_workers:
            self._shutdown_worker(wid, worker)
        for wid, worker in active_workers:
            self._await_shutdown_worker(wid, worker)
        self._set_queue_runtime_state(running=False, paused=False)
        with suppress(Exception):
            self.window._display_progress_wid = None
        with suppress(Exception):
            self.window.current_worker = None
        try:
            if self._db_progress_flush_timer.isActive():
                self._db_progress_flush_timer.stop()
            self._flush_pending_progress_writes()
        except Exception as exc:
            logger.debug(f"تعذر flush تقدم الطابور أثناء الإغلاق: {exc}")
        try:
            self._peak_speed_executor.shutdown(wait=False, cancel_futures=True)
        except Exception as exc:
            logger.debug(f"تعذر إيقاف peak speed executor: {exc}")
        try:
            self._post_download_pool.waitForDone(3000)
        except Exception as exc:
            logger.debug(f"تعذر إيقاف post-download QThreadPool: {exc}")
        if hasattr(self.window, "progress_bus") and self.window.progress_bus is not None:
            try:
                self.window.progress_bus.shutdown()
            except Exception as exc:
                logger.debug(f"Failed to shutdown progress bus: {exc}")

    def extract_size_text(self, line: str):
        try:
            current_match = re.search(r"(\d+(?:\.\d+)?)\s*([KMG])i?B\s+at", line)
            total_match = re.search(r"of\s+(\d+(?:\.\d+)?)\s*([KMG])i?B", line)
            if current_match:
                current = f"{current_match.group(1)} {current_match.group(2)}B"
            else:
                current = self.window.progress_size or "--"
            if total_match:
                return f"{current} / {total_match.group(1)} {total_match.group(2)}B"
            return current
        except Exception:
            return self.window.progress_size or "--"

    def on_download_log(self, line: str):
        text = str(line or "").strip()
        if not text:
            return
        self.window._append_log(text)
        self.window.progress_size = self.extract_size_text(text)
        self.window.search_view.size_label.setText(self.window.progress_size)

    def on_download_state(self, state: str):
        s = str(state or "").lower()
        sv = getattr(self.window, "search_view", None)
        bar = getattr(sv, "progress_bar", None) if sv is not None else None
        container = getattr(sv, "status_container", None) if sv is not None else None
        if sv is not None and hasattr(sv, "update_status"):
            mapped = _STATUS_RUNNING
            if s == _STATUS_SUCCESS:
                mapped = _STATUS_COMPLETED
            elif s in {_STATUS_FAILED, _STATUS_CANCELLED}:
                mapped = _STATUS_ERROR
            elif s == _STATUS_PAUSED:
                mapped = _STATUS_PAUSED
            sv.update_status(mapped)
        if s == _STATUS_RUNNING:
            self.window._set_status("جاري التحميل")
            if container is not None:
                container.show()
            if bar is not None:
                bar.set_status(_STATUS_DOWNLOADING)
        elif s == _STATUS_SUCCESS:
            self.window._set_status("اكتمل التحميل")
            if bar is not None:
                bar.set_status(_STATUS_COMPLETED)
        elif s == _STATUS_FAILED:
            self.window._set_status("فشل التحميل")
            if bar is not None:
                bar.set_status(_STATUS_ERROR)
        elif s == _STATUS_CANCELLED:
            self.window._set_status("تم الإلغاء")
            if bar is not None:
                bar.set_status(_STATUS_ERROR)
        elif s == _STATUS_PAUSED:
            self.window._set_status("تم الإيقاف المؤقت")
            if bar is not None:
                bar.set_status(_STATUS_PAUSED)

    def set_downloads_filter(self, key: str):
        self.window.downloads_filter = key
        self.window.downloads_page = 1
        self.window.downloads_view.set_active_filter(key)
        self.window._refresh_downloads_list()

    def set_downloads_sort(self, value: str):
        self.window.downloads_sort = value or "Date (Newest)"
        self.window.downloads_page = 1
        self.window._refresh_downloads_list()

    def set_queue_state_filter(self, value: str):
        mapping = {
            "Queue: All": _QUEUE_STATE_ALL,
            "Queue: Pending": _STATUS_PENDING,
            "Queue: Running": _STATUS_RUNNING,
            "Queue: Paused": _STATUS_PAUSED,
            "Queue: Failed": _STATUS_FAILED,
            "Queue: Cancelled": _STATUS_CANCELLED,
            "الكل": _QUEUE_STATE_ALL,
            "في الانتظار": _STATUS_PENDING,
            "قيد التشغيل": _STATUS_RUNNING,
            "متوقف مؤقتاً": _STATUS_PAUSED,
            "فشل": _STATUS_FAILED,
            "ملغى": _STATUS_CANCELLED,
        }
        self.window.queue_state_filter = mapping.get(value, _QUEUE_STATE_ALL)
        self.window.downloads_page = 1
        self.window._refresh_downloads_list()

    def set_downloads_page(self, value: str):
        try:
            self.window.downloads_page = max(1, int(value))
        except (TypeError, ValueError, OverflowError):
            self.window.downloads_page = 1
        self.window._refresh_downloads_list()

    def update_downloads_dashboard(self):
        queue_manager = getattr(self.window, "queue_manager", None)
        active = 0
        queued = 0
        if queue_manager is not None and hasattr(queue_manager, "get_dashboard_queue_counts"):
            try:
                queue_counts = dict(queue_manager.get_dashboard_queue_counts() or {})
                active = int(queue_counts.get("active", 0) or 0)
                queued = int(queue_counts.get("queued", 0) or 0)
            except Exception:
                active = 0
                queued = 0
        elif queue_manager is not None and hasattr(queue_manager, "get_queue_items_snapshot"):
            items = queue_manager.get_queue_items_snapshot()
            active = len([q for q in items if str(q.get("status", "")).lower() == _STATUS_RUNNING])
            queued = len([q for q in items if str(q.get("status", _STATUS_PENDING)).lower() in {_STATUS_PENDING, _STATUS_PAUSED, _STATUS_QUEUED, "waiting"}])
        completed, failed = self._get_dashboard_history_counts()
        self.window.downloads_view.set_dashboard_counts(active, queued, completed, failed)

    def _get_dashboard_history_counts(self) -> tuple[int, int]:
        now_ts = float(time.monotonic())
        cache_ttl = max(0.0, float(getattr(self, "_history_dashboard_cache_ttl_seconds", 1.25) or 1.25))
        if now_ts - float(getattr(self, "_history_dashboard_cache_ts", 0.0) or 0.0) <= cache_ttl:
            return (
                int(getattr(self, "_history_dashboard_completed", 0) or 0),
                int(getattr(self, "_history_dashboard_failed", 0) or 0),
            )

        stats_history = self.window.stats.get("download_history", []) or []
        stats_completed = len(
            [
                h
                for h in stats_history
                if str(h.get("status", "")).lower() in {_STATUS_SUCCESS, _STATUS_COMPLETED}
            ]
        )
        stats_failed = len(
            [h for h in stats_history if str(h.get("status", "")).lower() == _STATUS_FAILED]
        )

        db_completed = 0
        db_failed = 0
        try:
            db_completed = int(count_history_statuses([_STATUS_SUCCESS, _STATUS_COMPLETED]) or 0)
            db_failed = int(count_history(_STATUS_FAILED) or 0)
        except Exception:
            db_completed = 0
            db_failed = 0

        # Keep dashboard resilient even if DB is unavailable or stats are newer in-memory.
        completed = max(db_completed, stats_completed)
        failed = max(db_failed, stats_failed)
        self._history_dashboard_completed = int(completed)
        self._history_dashboard_failed = int(failed)
        self._history_dashboard_cache_ts = now_ts
        return int(completed), int(failed)

    def retry_failed_items(self):
        items = self.window.queue_manager.get_queue_items_snapshot()
        restored = 0
        for i, item in enumerate(items):
            if str(item.get("status", "")).lower() != _STATUS_FAILED:
                continue
            ok = self.window.queue_manager.update_task_fields(
                i,
                {"status": _STATUS_PENDING, "retry_count": 0, "next_retry_at": 0},
                emit_changed=False,
            )
            if ok:
                restored += 1
        if restored == 0:
            self.window._warn("لا توجد عناصر فاشلة لإعادة المحاولة")
            return
        self.window.queue_manager.queue_changed.emit()
        self.window._save_stats()
        self.window._append_log(f"تمت إعادة {restored} عنصر فاشل إلى الطابور")
        self.set_downloads_filter("queued")
        if restored > 0 and not self._queue_is_running():
            self.window._start_queue_download()

    def schedule_downloads_refresh(self, delay_ms: int = 220):
        now = time.monotonic()
        last_ts = getattr(self, "_last_downloads_refresh_ts", 0.0)
        if now - last_ts < self._MIN_REFRESH_GAP_S:
            return
        self._last_downloads_refresh_ts = now

        def _run():
            self.window._refresh_downloads_list()

        QTimer.singleShot(max(0, int(delay_ms)), _run)

    def refresh_queue_list(self):
        self.schedule_downloads_refresh(80)

    def on_queue_progress_updated(self, index: int, progress: float, speed: str, eta: str):
        if self.window.active_view != "downloads":
            return
        if self.window.downloads_filter != "active":
            return
        refs = self.window._active_download_card_refs.get(int(index))
        if not isinstance(refs, dict):
            return
        try:
            raw_progress = float(progress or 0)
        except (TypeError, ValueError, OverflowError):
            raw_progress = 0.0
        if 0 < raw_progress < 1:
            progress_val = 1
        else:
            progress_val = max(0, min(100, int(round(raw_progress))))
        speed_text = str(speed or "--")
        eta_text = str(eta or "--:--")
        try:
            card_ref = refs.get("card")
            if card_ref is not None:
                _card_obj_name = card_ref.objectName()
            details_label = refs.get("details_label")
            speed_label = refs.get("speed_label")
            progress_bar = refs.get("progress_bar")
            prev = refs.get("_last_ui_tuple")
            now_ts = time.monotonic()
            if isinstance(prev, tuple) and len(prev) == 3:
                prev_progress, prev_speed, prev_eta = prev
                last_ts = float(refs.get("_last_ui_ts", 0.0) or 0.0)
                if (
                    int(prev_progress) == int(progress_val)
                    and str(prev_speed) == speed_text
                    and str(prev_eta) == eta_text
                ):
                    return
                if int(prev_progress) == int(progress_val) and (now_ts - last_ts) < 0.35:
                    return
                if abs(int(progress_val) - int(prev_progress)) <= 1 and (now_ts - last_ts) < 0.12:
                    return
            if details_label is not None:
                details_label.setText(f"{_('Downloading')} ({progress_val}%) | {_('Time left:')} {eta_text}")
            if speed_label is not None:
                speed_label.setText(speed_text)
            if progress_bar is not None:
                progress_bar.setValue(progress_val)
            refs["_last_ui_tuple"] = (progress_val, speed_text, eta_text)
            refs["_last_ui_ts"] = now_ts
        except RuntimeError:
            self.window._active_download_card_refs.pop(int(index), None)

    def build_queue_entries(self, items: list[DownloadTask], downloads_filter: str, now_ts: float) -> list[DownloadTask]:
        out = []
        view = str(downloads_filter or "").strip().lower()
        if view == "active":
            for i, e in enumerate(items):
                if str(e.get("status", "")).lower() == _STATUS_RUNNING:
                    out.append(dict(e, queue_index=i))
            return out
        if view == "queued":
            for i, e in enumerate(items):
                status = str(e.get("status", _STATUS_PENDING) or _STATUS_PENDING).lower()
                if status in _QUEUED_FILTER_STATUSES:
                    sched = e.get("scheduled_at", 0)
                    if sched > now_ts and status == _STATUS_PENDING:
                        continue
                    out.append(dict(e, queue_index=i))
            return out
        if view == "scheduled":
            for i, e in enumerate(items):
                sched = e.get("scheduled_at", 0)
                if sched > now_ts and str(e.get("status", "")).lower() == _STATUS_PENDING:
                    e_copy = dict(e, queue_index=i)
                    e_copy["status"] = "scheduled"
                    out.append(e_copy)
            return out
        return out

    def filter_completed_history(self, history: list[DownloadHistoryEntry], history_filters: dict | None) -> list[DownloadHistoryEntry]:
        hf = dict(history_filters or {})
        mode_filter = str(hf.get("mode") or "all")
        format_filter = str(hf.get("format") or "all")
        date_filter = str(hf.get("date") or "all")
        out = list(history or [])
        if mode_filter and mode_filter != "all":
            out = [h for h in out if self.window._normalize_history_mode(h.get("mode", "")) == mode_filter]
        if format_filter and format_filter != "all":
            out = [h for h in out if str(h.get("format", "")).upper() == str(format_filter).upper()]
        if date_filter and date_filter != "all":
            now_dt = datetime.now()
            if date_filter == "24h":
                cutoff = now_dt - timedelta(days=1)
            elif date_filter == "7d":
                cutoff = now_dt - timedelta(days=7)
            else:
                cutoff = now_dt - timedelta(days=30)
            filtered = []
            for h in out:
                ts_text = str(h.get("timestamp", "") or "")
                try:
                    ts_dt = datetime.fromisoformat(ts_text)
                except (TypeError, ValueError):
                    try:
                        ts_dt = datetime.fromisoformat(ts_text.replace("Z", "+00:00"))
                    except (TypeError, ValueError):
                        continue
                if ts_dt >= cutoff:
                    filtered.append(h)
            out = filtered
        return out

    def sort_history(self, history: list[DownloadHistoryEntry], sort_key: str) -> list[DownloadHistoryEntry]:
        data: list[DownloadHistoryEntry] = list(history or [])
        key = str(sort_key or "Date (Newest)")
        if key == "Date (Oldest)":
            data.sort(key=lambda x: str(x.get("timestamp", "")))
        elif key == "Alphabetical (A → Z)":
            data.sort(key=lambda x: str(x.get("title", "")).lower())
        elif key == "Alphabetical (Z → A)":
            data.sort(key=lambda x: str(x.get("title", "")).lower(), reverse=True)
        elif key == "Size (Largest)":
            data.sort(key=lambda x: self.window._size_to_bytes(x.get("size", "--")), reverse=True)
        elif key == "Size (Smallest)":
            data.sort(key=lambda x: self.window._size_to_bytes(x.get("size", "--")))
        else:
            data.sort(key=lambda x: str(x.get("timestamp", "")), reverse=True)
        return data

    def download_entry_cache_key(self, item: dict, row_index: int) -> str:
        queue_index = (item or {}).get("queue_index")
        if isinstance(queue_index, int):
            return f"queue:{queue_index}"
        timestamp = str((item or {}).get("timestamp", "") or "").strip()
        url = str((item or {}).get("url", "") or "").strip()
        title = str((item or {}).get("title", "") or "").strip()
        if timestamp or url or title:
            return f"history:{timestamp}|{url}|{title}"
        return f"row:{int(row_index)}"

    def download_entry_render_signature(self, item: dict):
        resume = (item or {}).get("resume")
        if isinstance(resume, dict):
            resume_sig = (
                int(resume.get("partials_count") or 0),
                int(resume.get("partials_total_bytes") or 0),
            )
        else:
            resume_sig = (0, 0)
        return (
            str((item or {}).get("title", "")),
            str((item or {}).get("url", "")),
            str((item or {}).get("thumbnail", "")),
            str((item or {}).get("status", "")),
            bool((item or {}).get("is_live", False)),
            bool((item or {}).get("was_live", False)),
            str((item or {}).get("live_status", "")),
            str((item or {}).get("format", "")),
            str((item or {}).get("size", "")),
            int((item or {}).get("size_bytes") or 0),
            int((item or {}).get("estimated_size_bytes") or 0),
            bool((item or {}).get("size_is_estimate", True)),
            str((item or {}).get("speed", "")),
            str((item or {}).get("eta", "")),
            int((item or {}).get("duration_seconds") or 0),
            int((item or {}).get("scheduled_at") or 0),
            int((item or {}).get("bandwidth_limit_kbps") or 0),
            str((item or {}).get("last_output_path", "")),
            int(round(float((item or {}).get("progress", 0) or 0))),
            resume_sig,
        )

    def build_downloads_refresh_fingerprint(
        self,
        entries: list,
        theme: str,
        downloads_filter: str,
        queue_state_filter: str,
        media_filter: str,
        query: str,
        downloads_page: int,
    ):
        entry_fingerprints = []
        active_keys = set()
        for idx, entry in enumerate(list(entries or [])):
            key = self.download_entry_cache_key(entry, idx)
            sig = self.download_entry_render_signature(entry)
            entry_fingerprints.append((key, sig))
            active_keys.add(key)
        refresh_fingerprint = (
            str(theme or "default"),
            str(downloads_filter or ""),
            str(queue_state_filter or ""),
            str(media_filter or "all"),
            str(query or ""),
            int(downloads_page or 1),
            tuple(entry_fingerprints),
        )
        return refresh_fingerprint, entry_fingerprints, active_keys

    def append_history(self, success: bool, message: str, payload: dict, task: DownloadTask):
        status = _STATUS_SUCCESS if success else (_STATUS_CANCELLED if payload.get("error") == _STATUS_CANCELLED else _STATUS_FAILED)
        entry = DownloadHistoryEntry(
            timestamp=payload.get("timestamp", datetime.now().isoformat()),
            title=task.get("title", task.get("url", "")),
            url=task.get("url", ""),
            mode=task.get("mode", "video"),
            format=task.get("format", "--"),
            quality=task.get("quality", "--"),
            size=self.window.progress_size or "--",
            thumbnail=task.get("thumbnail", ""),
            file_path=payload.get("file_path", ""),
            status=status,
            message=message,
            attempts=payload.get("attempts", 1),
            error=payload.get("error", ""),
        )
        try:
            entry["size_text"] = entry["size"]
            entry["size_bytes"] = self.window._size_to_bytes(entry["size"])
        except Exception:
            entry["size_text"] = entry.get("size") or "--"
            entry["size_bytes"] = 0

        def _db_task():
            ok = True
            err = ""
            total_videos = 0
            total_audios = 0
            try:
                insert_history(entry)
                if success:
                    if task.get("mode") == "video":
                        increment_stat("total_videos", 1)
                    else:
                        increment_stat("total_audios", 1)
                db_stats = get_all_stats()
                total_videos = int(db_stats.get("total_videos", 0))
                total_audios = int(db_stats.get("total_audios", 0))
            except Exception as exc:
                ok = False
                err = str(exc)
            finally:
                close_thread_connection()

            def _apply():
                if not ok:
                    self.window._append_log(f"تعذر حفظ السجل في قاعدة البيانات: {err}")
                else:
                    mode_value = entry.get("mode", "video")
                    if hasattr(self.window, "_normalize_history_mode"):
                        normalized_mode = self.window._normalize_history_mode(mode_value)
                    else:
                        normalized_mode = "audio" if str(mode_value or "").strip().lower() in {"audio", "صوت"} else "video"
                    new_item = {
                        "timestamp": entry.get("timestamp", ""),
                        "title": entry.get("title", ""),
                        "url": entry.get("url", ""),
                        "mode": normalized_mode,
                        "format": entry.get("format", "--"),
                        "quality": entry.get("quality", "--"),
                        "size": entry.get("size_text", entry.get("size", "--")),
                        "thumbnail": entry.get("thumbnail", ""),
                        "file_path": entry.get("file_path", ""),
                        "status": entry.get("status", _STATUS_SUCCESS),
                        "message": entry.get("message", ""),
                        "attempts": entry.get("attempts", 1),
                        "error": entry.get("error", ""),
                    }
                    existing_history = list(self.window.stats.get("download_history", []) or [])
                    merged_history = [new_item]
                    dedupe_key = (
                        str(new_item.get("timestamp", "")),
                        str(new_item.get("url", "")),
                        str(new_item.get("title", "")),
                        str(new_item.get("status", "")),
                    )
                    for item in existing_history:
                        if not isinstance(item, dict):
                            continue
                        key = (
                            str(item.get("timestamp", "")),
                            str(item.get("url", "")),
                            str(item.get("title", "")),
                            str(item.get("status", "")),
                        )
                        if key == dedupe_key:
                            continue
                        merged_history.append(item)
                        if len(merged_history) >= 250:
                            break
                    self.window.stats["download_history"] = merged_history
                    self.window.stats["total_videos"] = total_videos
                    self.window.stats["total_audios"] = total_audios
                self._history_dashboard_cache_ts = 0.0
                self.window._save_stats()

            if not run_on_qt_main_thread(_apply):
                _apply()

        self._submit_post_download_task(
            _db_task,
            label="history_write",
            legacy_executor_attr="_history_write_executor",
        )

    def on_download_finished_event(self, event):
        self.window.download_finished_ui.emit(event)

    def handle_download_finished_event(self, event):
        wid, success, msg, data = event.worker_id, event.success, event.message, event.data
        running_others = self.window._active_workers_count()
        queue_manager = getattr(self.window, "queue_manager", None)
        queue_item_count = 0
        if queue_manager is not None:
            if hasattr(queue_manager, "get_item_count"):
                try:
                    queue_item_count = max(0, int(queue_manager.get_item_count() or 0))
                except Exception:
                    queue_item_count = 0
            elif hasattr(queue_manager, "get_queue_items_snapshot"):
                try:
                    queue_item_count = len(queue_manager.get_queue_items_snapshot() or [])
                except Exception:
                    queue_item_count = 0
        is_queue_worker = isinstance(wid, int) and 0 <= wid < queue_item_count
        cancelled_by_user, bandwidth_restart_requested, paused_by_user = self._take_worker_request_state(wid)
        task = self.window.queue_manager.get_task(wid) if is_queue_worker else {}
        if not isinstance(task, dict):
            task = {}
        if paused_by_user and not cancelled_by_user:
            if bandwidth_restart_requested and is_queue_worker:
                self._sync_task_fields(wid, task, {"status": _STATUS_PENDING, "next_retry_at": 0}, emit_changed=False)
                self._flush_progress_snapshot(wid, task, status=_STATUS_PENDING)
                self.window._append_log(
                    f"تمت إعادة تشغيل العنصر لتطبيق حد السرعة الجديد: {self.window._format_bandwidth_limit(int(task.get('bandwidth_limit_kbps', 0) or 0))}"
                )
                self.window._save_session()
                self.window._refresh_downloads_list()
                if not self._queue_is_running() and self.window._active_workers_count() == 0:
                    self._set_queue_runtime_state(running=True, paused=False)
                QTimer.singleShot(150, self.process_parallel_queue)
                return
            if is_queue_worker:
                self.window.queue_manager.set_task_status(wid, _STATUS_PAUSED)
                self._flush_progress_snapshot(wid, task, status=_STATUS_PAUSED)
            self.window._append_log("تم إيقاف عنصر مؤقتاً")
            self.window._save_session()
            self.window._refresh_downloads_list()
            if self._queue_is_running() and not self._queue_is_paused():
                QTimer.singleShot(250, self.process_parallel_queue)
            elif running_others <= 0:
                self.window._set_status("الطابور متوقف مؤقتاً")
            return
        status = _STATUS_SUCCESS if success else (_STATUS_CANCELLED if data.get("error") == _STATUS_CANCELLED else _STATUS_FAILED)
        if is_queue_worker:
            self.window.queue_manager.set_task_status(wid, status)
        if status == _STATUS_SUCCESS:
            file_path = str(data.get("file_path", "") or "").strip()
            if file_path:
                organize_result = self._maybe_auto_categorize_output(file_path, task)
                file_path = str(organize_result.get("file_path", "") or file_path).strip()
                data["file_path"] = file_path
                self._sync_task_fields(
                    wid,
                    task,
                    {
                        "file_path": file_path,
                        "last_output_path": file_path,
                        "file_missing": False,
                        "integrity_state": "present",
                        "integrity_note": "",
                    },
                    emit_changed=False,
                )
                self._track_completed_output(wid, file_path)
                action = str(task.get("post_action", "") or "").strip()
                script_path = str(task.get("post_download_script", "") or "").strip()
                pipeline = list(task.get("post_process_pipeline", []) or [])
                if pipeline:
                    try:
                        PostDownloadManager.execute_pipeline(
                            file_path,
                            pipeline,
                            confirm_callback=self._confirm_post_action,
                        )
                    except Exception as exc:
                        logger.warning("فشل تنفيذ pipeline ما بعد التحميل: %s", exc)
                elif action:
                    try:
                        PostDownloadManager.execute_action(
                            action,
                            file_path,
                            script_path=script_path,
                            confirm_callback=self._confirm_post_action,
                        )
                    except Exception as exc:
                        logger.warning(f"فشل تنفيذ إجراء ما بعد التحميل '{action}': {exc}")
                elif script_path:
                    try:
                        PostDownloadManager.execute_script(script_path, file_path)
                    except Exception as exc:
                        logger.warning(f"فشل تنفيذ سكربت ما بعد التحميل: {exc}")
                try:
                    write_nfo_for_download(file_path, task, data)
                except Exception as exc:
                    logger.debug(f"تعذر إنشاء NFO: {exc}")
        if status == _STATUS_FAILED and is_queue_worker:
            retry_count = int(task.get("retry_count", 0) or 0)
            retry_limit = int(task.get("queue_retry_limit", self.window.queue_auto_retry_limit) or self.window.queue_auto_retry_limit)
            if retry_count < retry_limit:
                base_delay = int(task.get("auto_retry_delay_seconds", self.window.auto_retry_delay_seconds) or self.window.auto_retry_delay_seconds)
                next_retry_count = retry_count + 1
                delay_seconds = min(300, max(1, base_delay) * (2 ** (next_retry_count - 1)))
                next_retry_at = datetime.now().timestamp() + delay_seconds
                self._sync_task_fields(
                    wid,
                    task,
                    {"retry_count": next_retry_count, "next_retry_at": next_retry_at, "status": _STATUS_PENDING},
                    emit_changed=False,
                )
                text = f"فشل العنصر وسيتم إعادة المحاولة تلقائياً بعد {delay_seconds} ثانية ({next_retry_count}/{retry_limit})"
                self.window._append_log(text)
                self.window._info(text)
                self._flush_progress_snapshot(wid, task, status=_STATUS_PENDING)
                self.window._save_session()
                self.window._refresh_downloads_list()
                if self._queue_is_running():
                    QTimer.singleShot(500, self.process_parallel_queue)
                return
        if status == _STATUS_SUCCESS and is_queue_worker:
            self._sync_task_fields(wid, task, {"retry_count": 0, "next_retry_at": 0}, emit_changed=False)
            repeat_mode = str(task.get("schedule_repeat", "none") or "none")
            if repeat_mode in {"daily", "weekly"}:
                base_ts = float(task.get("scheduled_at", 0) or 0)
                if base_ts <= 0:
                    base_ts = datetime.now().timestamp()
                days = 1 if repeat_mode == "daily" else 7
                next_ts = base_ts + days * 24 * 3600
                cloned = dict(task)
                cloned["task_uuid"] = str(uuid4())
                cloned["status"] = _STATUS_PENDING
                cloned["retry_count"] = 0
                cloned["next_retry_at"] = 0
                cloned["scheduled_at"] = next_ts
                self.window.queue_manager.add_task(cloned)
                self.window._append_log(
                    f"تمت جدولة العنصر '{task.get('title', '')}' للتكرار ({repeat_mode}) في {datetime.fromtimestamp(next_ts).isoformat(sep=' ')}"
                )
        will_retry_via_anti_detect = False
        if not success and not cancelled_by_user and is_queue_worker:
            if anti_detection_engine.on_error(msg):
                retry_count = task.get("retry_count", 0)
                if retry_count < 5:
                    self.window._append_log(f"[Anti-Detect] Re-queueing task {task.get('title')} after rate-limit. Retry #{retry_count + 1}")
                    self._sync_task_fields(
                        wid,
                        task,
                        {"retry_count": retry_count + 1, "status": _STATUS_PENDING, "next_retry_at": 0},
                        emit_changed=False,
                    )
                    self._flush_progress_snapshot(wid, task, status=_STATUS_PENDING)
                    will_retry_via_anti_detect = True
                else:
                    self.window._append_log(f"[Anti-Detect] Max retries for {task.get('title')} reached. Won't re-queue.")
        if is_queue_worker and not will_retry_via_anti_detect:
            self._flush_progress_snapshot(wid, task, status=status)
        if not will_retry_via_anti_detect:
            self.append_history(success, msg, data, task)
        self.window._append_log(msg)
        self.window._save_session()
        self.window._refresh_downloads_list()
        if status == _STATUS_SUCCESS:
            completed_title = str(task.get("title", "") or "").strip() or "Download"
            self.window._show_tray_message(
                "SnapDownloader",
                f"اكتمل التحميل: {completed_title[:64]}",
                QSystemTrayIcon.MessageIcon.Information,
                2500,
            )
        if not success and data.get("error") != _STATUS_CANCELLED:
            try:
                self.window._record_error(
                    data.get("error", msg),
                    url=task.get("url", ""),
                    title=task.get("title", ""),
                    source="download",
                )
                if proxy_manager.is_enabled():
                    proxy_manager.on_failure(proxy_manager.get_current_proxy() or "")
            except Exception as exc:
                logger.warning(f"تعذر تسجيل الخطأ في Error Dashboard: {exc}")
        if self._queue_is_running() and not self._queue_is_paused():
            QTimer.singleShot(200, self.process_parallel_queue)
        if running_others <= 0 and not self._queue_is_running():
            self.window._set_status(self._queue_stopped_status_text())
            self.window._set_controls_enabled(True)
            self.window._tray_progress_bucket = -1
            try:
                # C-05: wire confirm_callback so destructive actions require user approval via dialog
                def _confirm_action(label: str) -> bool:
                    try:
                        from PySide6.QtWidgets import QMessageBox
                    except ImportError:
                        from PyQt6.QtWidgets import QMessageBox
                    resp = QMessageBox.question(
                        self.window,
                        "تأكيد الإجراء",
                        f"\u0647\u0644 \u062a\u0631\u064a\u062f \u062a\u0646\u0641\u064a\u0630: {label}?\n\u0647\u0630\u0627 \u0627\u0644\u0625\u062c\u0631\u0627\u0621 \u0633\u064a\u062a\u0645 \u0628\u0639\u062f {sustainability.delay_seconds} \u062b\u0627\u0646\u064a\u0629.",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    return resp == QMessageBox.StandardButton.Yes
                sustainability.confirm_callback = _confirm_action
                sustainability.on_queue_complete(notify_callback=self.window._info)
            except Exception as exc:
                logger.warning(f"\u062a\u0639\u0630\u0631 \u062a\u0646\u0641\u064a\u0630 \u0625\u062c\u0631\u0627\u0621 \u0627\u0644\u0627\u0633\u062a\u062f\u0627\u0645\u0629 \u0628\u0639\u062f \u0627\u0643\u062a\u0645\u0627\u0644 \u0627\u0644\u0637\u0627\u0628\u0648\u0631: {exc}")
        if success:
            self.window._info(msg)
        elif data.get("error") != _STATUS_CANCELLED:
            self.window._warn(msg)



