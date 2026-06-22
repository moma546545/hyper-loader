import json
import csv
import os

from ui.layout_manager import build_main_window_ui, wire_views
from core.session_service import SessionService
import re
import shutil
import subprocess
import sys
import logging
import threading
import time
from collections import OrderedDict, deque
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
import qtawesome as qta

logger = logging.getLogger("SnapDownloader")

from core.qt_compat import (
    QThread, Qt, Signal, QUrl, QTimer, QPropertyAnimation, QEasingCurve, QDateTime,
    QStringListModel, QTime, QSize, QPoint, QObject, QEvent,
    QNetworkAccessManager, QNetworkRequest, QNetworkReply,
    QIcon, QAction, QPixmap, QPainter, QColor, QPen, QPainterPath,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QCompleter,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QRadioButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QTextEdit,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
    QGraphicsOpacityEffect,
    QSystemTrayIcon,
    QMenu,
    QStyle,
    QDateTimeEdit,
)

from core.downloader import DownloadWorker
from core.workers import AnalyzeWorker, FormatProbeWorker
from ui.models import DownloadListModel
from ui.widgets import SpeedGraphWidget, WheelEventFilter
from ui.trim_dialog import TrimView
from core.i18n import _, i18n
from core.task_types import DownloadHistoryEntry, DownloadTask, StatsState
from core.constants import (
    VIDEO_FORMATS,
    AUDIO_FORMATS,
    VIDEO_QUALITIES,
    AUDIO_QUALITIES,
    SUBTITLE_OPTIONS,
)
from core.extension_server import extension_server
from core.database import init_db, insert_history, fetch_history, count_history, delete_history, get_all_stats, increment_stat, record_peak_speed, migrate_from_json, save_queue_items, load_queue_items, save_session_settings, load_session_settings, close_thread_connection, get_app_data_dir
from core.duplicate_finder import build_duplicate_report
from core.cookie_importer import get_available_browsers, auto_detect_and_export
from core.hotkeys import setup_default_hotkeys
from core.smart_rename import TEMPLATES as RENAME_TEMPLATES
from core.proxy_manager import proxy_manager
from core.config_manager import ConfigManager
from core.config import DEFAULT_SETTINGS, THEME_MODE_MAP, normalize_video_quality_label, default_download_dir, estimate_file_size_bytes
from core.storage_watchdog import has_enough_space, format_bytes
from core.audio_normalizer import normalize_file, STREAMING_TARGET_LUFS
from core.anti_detection import anti_detection_engine
from core.bandwidth_scheduler import scheduler
from core.memory_guard import memory_guard
from core.sustainability import sustainability, ACTIONS as SUSTAINABILITY_ACTIONS
from core.queue_manager import QueueManager
from core.event_bus import ShowNotificationEvent, DownloadFinishedEvent, ExtensionLinkReceivedEvent
from core.thumbnail_manager import ThumbnailManager
from core.windows_toast import show_native_toast
from core.window_controllers import SettingsController, DownloadController, ImportController, ExtensionController, AnalyzeController, UpdateController, LifecycleController
from core.ui_controller import UIController
from core.window_mixins import PremiumWindowSystemMixin, PremiumWindowUtilityMixin
from core.session_service import SessionService
from core.trial_manager import TrialManager
from core.error_handler import ErrorHandler
from ui.themes import get_theme
from ui.clip_watcher import ClipWatcher
from ui.mini_mode import MiniModeWindow
from ui.error_dashboard import ErrorDashboard, analyze_error
from ui.overlay import NotificationOverlay
from ui.playlist_view import PlaylistView
from ui.stats_view import StatsView
from ui.views.title_bar import build_title_bar

from core.bootstrap import start_app
from core.utils import redact_url
from core.window_bootstrap import (
    active_workers_count,
    close_window,
    connect_bootstrap_signals,
    finish_startup,
    init_database_async,
    init_controllers_and_runtime,
    init_core_state,
    init_threads_and_timers,
    mark_bandwidth_restart_requested,
    mark_cancel_requested,
    mark_pause_requested,
    prune_inactive_workers,
    queue_is_paused,
    queue_is_running,
    set_queue_runtime_state,
    start_background_services,
    take_worker_request_state,
)
from core.window_ui_actions import (
    on_formats_probe_finished,
    on_formats_requested,
    on_mode_changed,
    toggle_advanced_options,
    toggle_trim_options,
)


class CompressedRotatingFileHandler(RotatingFileHandler):
    def doRollover(self):
        super().doRollover()
        try:
            import gzip

            for i in range(self.backupCount, 0, -1):
                filename = f"{self.baseFilename}.{i}"
                gz_filename = filename + ".gz"
                if os.path.exists(filename) and not os.path.exists(gz_filename):
                    with open(filename, "rb") as src, gzip.open(gz_filename, "wb") as dst:
                        dst.writelines(src)
                    os.remove(filename)
        except Exception:
            logger.warning("Failed to compress rotated logs", exc_info=True)


def setup_logging():
    try:
        base_dir = get_app_data_dir()
    except Exception:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    logs_dir = os.path.join(base_dir, "logs")
    try:
        os.makedirs(logs_dir, exist_ok=True)
    except Exception:
        pass
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    session_log = os.path.join(logs_dir, f"session-{ts}.log")

    handler = CompressedRotatingFileHandler(
        session_log,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    handler.setFormatter(formatter)

    root = logging.getLogger()
    if not any(isinstance(h, CompressedRotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)
    if root.level == logging.NOTSET:
        root.setLevel(logging.INFO)


class PremiumWindow(PremiumWindowSystemMixin, PremiumWindowUtilityMixin, QMainWindow):
    download_finished_ui = Signal(object)
    bulk_import_finished_ui = Signal(object)
    session_loaded_ui = Signal(object)
    history_item_path_resolved_ui = Signal(str, str)

    def _bulk_import(self):
        self.import_controller.bulk_import()

    def _bulk_import_worker(self, file_path: str):
        self.import_controller.bulk_import_worker(file_path)

    def _handle_bulk_import_result(self, payload: dict):
        self.import_controller.handle_bulk_import_result(payload)

    def _on_playlist_view_analyze_requested(self, url: str):
        self.analyze_controller.on_playlist_view_analyze_requested(url)

    def _on_playlist_view_force_analyze_requested(self, url: str):
        self.analyze_controller.on_playlist_view_analyze_requested(url, force=True)

    def _on_playlist_analyze_finished(self, success: bool, message: str, payload: dict, items: list):
        self.analyze_controller.on_playlist_analyze_finished(success, message, payload, items)

    def _start_analyze(self):
        self.analyze_controller.start_analyze()

    def _force_reset_search_ui(self):
        self.analyze_controller.force_reset_search_ui()

    def _update_search_spinner(self):
        self.analyze_controller.update_search_spinner()

    def _on_analyze_finished(self, success: bool, message: str, payload: dict, items: list):
        self.analyze_controller.on_analyze_finished(success, message, payload, items)

    def _on_playlist_view_download_requested(self, tasks: list):
        self.analyze_controller.on_playlist_view_download_requested(tasks)

    def _pulse_widget(self, btn):
        return

    def _wire_views(self):
        wire_views(self)

    def _save_session(self, sync: bool = False):
        self.settings_controller.save_session(sync=sync)

    def _load_session_async(self):
        if hasattr(self, "session_service") and self.session_service is not None:
            self.session_service.load_session_async()
            return
        self.settings_controller.load_session_async()

    def _handle_session_load_result(self, result: dict):
        self.settings_controller.handle_session_load_result(result)

    def _migrate_legacy_session_json(self):
        if hasattr(self, "session_service") and self.session_service is not None:
            self.session_service.migrate_legacy_session_json()
            return
        self.settings_controller.migrate_legacy_session_json()

    def __init__(self):
        super().__init__()
        connect_bootstrap_signals(self)
        init_core_state(self)
        init_controllers_and_runtime(self)
        init_threads_and_timers(self)
        start_background_services(self)
        finish_startup(self)

    def _queue_is_running(self) -> bool:
        return queue_is_running(self)

    def _queue_is_paused(self) -> bool:
        return queue_is_paused(self)

    def _set_queue_runtime_state(self, *, running: bool | None = None, paused: bool | None = None):
        set_queue_runtime_state(self, running=running, paused=paused)

    def _build_ui(self):
        build_main_window_ui(self)

    def _refresh_title_bar(self):
        central = self.centralWidget()
        if central is None:
            return
        root_layout = central.layout()
        if root_layout is None:
            return
        old_title_bar = getattr(self, "title_bar", None)
        new_title_bar = self._build_title_bar()
        self.title_bar = new_title_bar
        if old_title_bar is not None:
            try:
                root_layout.replaceWidget(old_title_bar, new_title_bar)
                old_title_bar.setParent(None)
                old_title_bar.deleteLater()
            except Exception:
                root_layout.insertWidget(0, new_title_bar)
        else:
            root_layout.insertWidget(0, new_title_bar)

    def _refresh_translations(self):
        self._refresh_title_bar()
        self.setWindowTitle(_("SnapDownloader"))
        for attr_name in (
            "sidebar",
            "search_view",
            "browser_view",
            "downloads_view",
            "settings_view",
            "tools_view",
            "playlist_view",
            "subscriptions_view",
            "stats_view",
        ):
            view = getattr(self, attr_name, None)
            if view is None or not hasattr(view, "retranslate_ui"):
                continue
            try:
                view.retranslate_ui()
            except Exception as exc:
                logger.debug(f"تعذر تحديث النصوص في {attr_name}: {exc}")
        if hasattr(self, "_refresh_downloads_list"):
            try:
                self._refresh_downloads_list()
            except Exception as exc:
                logger.debug(f"تعذر تحديث قائمة التنزيلات بعد تغيير اللغة: {exc}")

    def _init_database_async(self):
        init_database_async(self, logger)

    def _detect_system_theme_mode(self):
        return self.settings_controller.detect_system_theme_mode()

    def _set_system_theme_sync_enabled(self, enabled: bool):
        self.settings_controller.set_system_theme_sync_enabled(enabled)

    def _apply_theme(self, theme_name: str, persist: bool = False):
        self.settings_controller.apply_theme(theme_name, persist=persist)

    def _refresh_system_theme(self, force: bool = False, persist: bool = False):
        self.settings_controller.refresh_system_theme(force=force, persist=persist)

    def _toggle_theme(self):
        self.settings_controller.toggle_theme()

    def _toggle_dark_light_mode(self):
        self.settings_controller.toggle_dark_light_mode()


    def _qss(self):
        from ui.themes import get_style_sheet
        return get_style_sheet(self.theme)

    def _build_title_bar(self):
        return build_title_bar(self)

    def _prune_inactive_workers(self):
        return prune_inactive_workers(self)

    def _active_workers_count(self):
        return active_workers_count(self)

    def _mark_pause_requested(self, wid: int):
        mark_pause_requested(self, wid)

    def _mark_cancel_requested(self, wid: int):
        mark_cancel_requested(self, wid)

    def _mark_bandwidth_restart_requested(self, wid: int):
        mark_bandwidth_restart_requested(self, wid)

    def _take_worker_request_state(self, wid: int) -> tuple[bool, bool, bool]:
        """
        Atomically read and clear request flags for a worker.
        Returns: (cancelled_by_user, bandwidth_restart_requested, paused_by_user)
        """
        return take_worker_request_state(self, wid)

    def _close_window(self):
        close_window(self)

    def _show_subscriptions(self):
        self._switch_view("subscriptions")
        subs_view = getattr(self, "subscriptions_view", None)
        field = getattr(subs_view, "url_input", None)
        if field is not None:
            QTimer.singleShot(0, field.setFocus)

    def _set_ui_language_from_menu(self, lang_code: str):
        code = str(lang_code or "").strip().lower()
        self.settings_controller.set_ui_language(code, persist=True, notify=False)
        labels = i18n.available_languages()
        self._info(_("Settings saved for UI language: {language}").format(language=labels.get(code, code)))

    def _open_web_url(self, url: str):
        target = str(url or "").strip()
        if not target:
            self._warn(_("Invalid URL"))
            return
        try:
            try:
                from PySide6.QtGui import QDesktopServices
            except ImportError:
                from PyQt6.QtGui import QDesktopServices
            if not QDesktopServices.openUrl(QUrl(target)):
                raise RuntimeError("openUrl returned False")
        except Exception as exc:
            self._warn(_("Failed to open link: {error}").format(error=exc))

    def _export_subscriptions(self):
        from core.channel_subscriptions import SUBS_PATH, subscription_manager

        subscription_manager.save()
        if not os.path.exists(SUBS_PATH):
            self._warn(_("No subscriptions available to export right now"))
            return

        default_path = os.path.join(os.path.expanduser("~"), "subscriptions-backup.json")
        target_path, _ = QFileDialog.getSaveFileName(
            self,
            "تصدير الاشتراكات",
            default_path,
            "JSON Files (*.json);;All Files (*)",
        )
        if not target_path:
            return
        try:
            shutil.copyfile(SUBS_PATH, target_path)
            self._info(_("Subscriptions exported successfully"))
        except Exception as exc:
            self._warn(_("Failed to export subscriptions: {error}").format(error=exc))

    def _import_subscriptions(self):
        from core.channel_subscriptions import SUBS_PATH, subscription_manager

        source_path, _ = QFileDialog.getOpenFileName(
            self,
            "استيراد الاشتراكات",
            "",
            "JSON Files (*.json);;All Files (*)",
        )
        if not source_path:
            return
        try:
            os.makedirs(os.path.dirname(SUBS_PATH), exist_ok=True)
            shutil.copyfile(source_path, SUBS_PATH)
            subscription_manager.load()
            subs_view = getattr(self, "subscriptions_view", None)
            if subs_view is not None and hasattr(subs_view, "_refresh_list"):
                subs_view._refresh_list()
            self._show_subscriptions()
            self._info(_("Subscriptions imported successfully"))
        except Exception as exc:
            self._warn(_("Failed to import subscriptions: {error}").format(error=exc))

    def _cancel_all_active_downloads(self):
        with self._active_workers_lock:
            active_ids = [wid for wid in self.active_workers.keys() if isinstance(wid, int)]
        if not active_ids:
            self._warn("لا توجد تنزيلات نشطة لإلغائها")
            return
        for wid in active_ids:
            self.download_controller.cancel_queue_item(wid)
        self._info(f"تم إرسال إلغاء إلى {len(active_ids)} تنزيل/تنزيلات نشطة")

    def _clear_finished_or_failed_downloads(self):
        removable_statuses = {"success", "completed", "failed", "error", "cancelled"}
        cleared = 0
        total_items = self.queue_manager.get_item_count()
        for idx in range(total_items):
            item = self.queue_manager.get_task(idx)
            if not isinstance(item, dict):
                continue
            status = str(item.get("status", "") or "").strip().lower()
            if status not in removable_statuses:
                continue
            self.queue_manager.update_task_fields(
                idx,
                {"status": "deleted", "next_retry_at": 0},
                emit_changed=False,
            )
            cleared += 1

        if cleared == 0:
            self._clear_completed_history()
            self._warn("لا توجد تنزيلات مكتملة أو فاشلة لمسحها")
            return

        self.queue_manager.queue_changed.emit()
        self._clear_completed_history()
        self._save_session()
        self._refresh_downloads_list()
        self._info(f"تم تنظيف {cleared} عنصر من التنزيلات المكتملة أو الفاشلة")


    def changeEvent(self, event):
        """Handle window state changes."""
        if event.type() == QEvent.Type.WindowStateChange:
            minimized = bool(self.windowState() & Qt.WindowState.WindowMinimized)
            liquid_timer = getattr(self, "liquid_timer", None)
            if minimized:
                if liquid_timer is not None and liquid_timer.isActive():
                    liquid_timer.stop()
            else:
                if hasattr(self, "_update_liquid_timer_state"):
                    try:
                        self._update_liquid_timer_state()
                    except Exception as exc:
                        logger.debug(f"تعذر تحديث حالة liquid timer: {exc}")
        super().changeEvent(event)

    def _on_mode_changed(self):
        on_mode_changed(self)

    # Dragging is handled by the custom title bar widget.

    def _toggle_advanced_options(self):
        toggle_advanced_options(self)

    def _toggle_trim_options(self):
        toggle_trim_options(self)

    def _on_formats_requested(self, url: str):
        on_formats_requested(self, url)

    def _on_formats_probe_finished(self, success: bool, output: str, error: str):
        on_formats_probe_finished(self, success, output, error)

    def _process_metadata_fetch_queue(self):
        if self._metadata_fetch_worker is not None or not self._metadata_fetch_queue:
            return
        
        task = self._metadata_fetch_queue.pop(0)
        url = task.get("url")
        self._metadata_fetch_worker = AnalyzeWorker(
            url,
            cookies_file=self.cookies_path,
            extra_args=anti_detection_engine.get_yt_dlp_analysis_options(),
        )
        self._metadata_fetch_worker.finished.connect(lambda s, m, p, i, t=task: self._on_metadata_fetch_finished(s, m, p, i, t))
        self._metadata_fetch_worker.start()

    def _on_metadata_fetch_finished(self, success, message, payload, items, task):
        self._metadata_fetch_worker = None
        if success and payload.get("kind") == "single":
            task["title"] = payload.get("title", task["title"])
            task["thumbnail"] = payload.get("thumbnail", task["thumbnail"])
            task["duration_seconds"] = payload.get("duration_seconds", 0)
            self._save_session()
            self._refresh_downloads_list()
        
        # Process next
        QTimer.singleShot(100, self._process_metadata_fetch_queue)



    def _apply_downloads_inline_theme(self):
        self.downloads_view.apply_theme({"theme": self.theme}, get_theme)














    def _find_best_file_path(self, item: dict) -> str:
        return self.history_playback_controller.find_best_file_path(item)

    def _open_history_item_file(self, item: dict):
        self.history_playback_controller.open_history_item_file(item)

    def _open_history_item_folder(self, item: dict):
        self.history_playback_controller.open_history_item_folder(item)

    def _resolve_history_item_path_async(self, item: dict, on_ready):
        self.history_playback_controller.resolve_history_item_path_async(item, on_ready)

    def _on_history_item_path_resolved(self, request_id: str, target: str):
        self.history_playback_controller.on_history_item_path_resolved(request_id, target)

    def _switch_view(self, key: str):
        self.ui_controller.switch_view(key)

    def _animate_view_change(self):
        self.ui_controller.animate_view_change()
        
    def _init_trial_timer(self):
        if not self.trial_enabled:
            if hasattr(self, "trial_label"):
                self.trial_label.hide()
            return
        self._update_trial_banner()
        self.trial_timer = QTimer(self)
        self.trial_timer.timeout.connect(self._update_trial_banner)
        self.trial_timer.start(86_400_000)  # refresh once per day (24h)

    def _init_trial_state(self):
        state = self.trial_manager.load_state()
        self.trial_started_at = state.started_at
        self.trial_total_days = int(state.total_days)
        self.trial_days_remaining = int(state.days_remaining)

    def _recompute_trial_days(self):
        state = self.trial_manager.recompute(
            started_at=self.trial_started_at,
            total_days=self.trial_total_days,
        )
        self.trial_started_at = state.started_at
        self.trial_total_days = int(state.total_days)
        self.trial_days_remaining = int(state.days_remaining)

    def _update_trial_banner(self):
        if not hasattr(self, "trial_label"):
            return
        if not self.trial_enabled:
            self.trial_label.hide()
            return
        self._recompute_trial_days()
        self.trial_label.setText(self.trial_manager.banner_text(self.trial_days_remaining))

    def _init_toast(self):
        # This method is now a placeholder as NotificationOverlay handles its own setup.
        pass

    def _show_toast(self, message: str, level: str = "info", title: str = None):
        text = str(message or "").strip()
        if not text:
            return

        if title is None:
            if level == "success": title = "Success"
            elif level == "warn": title = "Warning"
            elif level == "error": title = "Error"
            else: title = "Information"

        if show_native_toast(title, text, app_id="VidDownloader"):
            return

        # Fallback to internal notification overlay
        if hasattr(self, "notification_overlay"):
            self.notification_overlay.show_message(title, text, level)

    def _show_toast_from_thread(self, message: str, level: str = "info", title: str = None):
        # Helper to be called from non-GUI threads via signals/slots
        self._show_toast(message, level, title)

    def _pick_conv_file(self):
        self.media_tools_controller.pick_conv_file()

    def _start_conversion(self):
        self.media_tools_controller.start_conversion()

    def _fetch_channel(self):
        self.media_tools_controller.fetch_channel()

    def _set_search_state(self, state: str):
        self.analyze_controller.set_search_state(state)

    def _setup_search_history_completer(self):
        completer = QCompleter(self.search_history_model, self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.search_view.url_input.setCompleter(completer)

    def _normalize_search_history(self, values):
        return self.analyze_controller.normalize_search_history(values)

    def _record_search_history(self, value: str):
        self.analyze_controller.record_search_history(value)

    def _trim_thumbnail_cache(self):
        self.thumbnail_controller.trim_thumbnail_cache()

    def _trim_thumbnail_failed_locked(self, now: float | None = None):
        self.thumbnail_controller.trim_thumbnail_failed_locked(now=now)

    def _thumbnail_failed_contains(self, cache_key: str) -> bool:
        return self.thumbnail_controller.thumbnail_failed_contains(cache_key)

    def _clear_thumbnail_failed(self, cache_key: str):
        self.thumbnail_controller.clear_thumbnail_failed(cache_key)

    def _mark_thumbnail_failed(self, cache_key: str):
        self.thumbnail_controller.mark_thumbnail_failed(cache_key)

    def _clear_search_history(self):
        self.analyze_controller.clear_search_history()

    # NOTE: _on_mode_toggle was a duplicate/dead handler never connected to any signal.
    # _on_mode_changed (connected in _build_search_single_state) is the authoritative handler.

    def _build_task(self, url=None, title=None, thumbnail="", fmt=None, quality=None) -> DownloadTask:
        return self.download_controller.build_task(url=url, title=title, thumbnail=thumbnail, fmt=fmt, quality=quality)

    def _normalize_task(self, task: DownloadTask | None, **kwargs) -> DownloadTask:
        return self.download_controller.normalize_task(task, **kwargs)

    def _format_bandwidth_limit(self, limit_kbps: int) -> str:
        return self.settings_controller.format_bandwidth_limit(limit_kbps)

    def _current_bandwidth_limit_kbps(self) -> int:
        return self.settings_controller.current_bandwidth_limit_kbps()

    def _refresh_bandwidth_schedule(self, notify: bool = False):
        self.settings_controller.refresh_bandwidth_schedule(notify=notify)

    def _bandwidth_schedule_editor_text(self) -> str:
        return self.settings_controller.bandwidth_schedule_editor_text()


    def _get_bandwidth_schedule_editor_rules(self) -> list[dict]:
        return self.settings_controller.get_bandwidth_schedule_editor_rules()

    def _set_bandwidth_schedule_editor_rules(self, rules: list[dict], selected_index: int | None = None):
        self.settings_controller.set_bandwidth_schedule_editor_rules(rules, selected_index=selected_index)


    def _set_bandwidth_rule_form(self, rule: dict | None = None):
        self.settings_controller.set_bandwidth_rule_form(rule)


    def _load_selected_bandwidth_rule(self, row: int):
        self.settings_controller.load_selected_bandwidth_rule(row)





    def _check_storage_guard(self, path: str = "", pause_on_low: bool = False) -> bool:
        return self.download_controller.check_storage_guard(path=path, pause_on_low=pause_on_low)

    def _run_storage_watchdog(self):
        self.download_controller.run_storage_watchdog()

    def _fade_widget(self, widget, delay_ms=0, duration_ms=280):
        self.ui_controller.fade_widget(widget, delay_ms, duration_ms)

    def _pulse_widget(self, widget):
        self.ui_controller.pulse_widget(widget)

    def _animate_single_state_widgets(self):
        widgets = [
            getattr(self, "single_player_card", None),
            getattr(self, "single_settings_card", None),
            getattr(self, "adv_toggle_btn", None),
            getattr(self, "trim_btn", None),
            getattr(self, "schedule_btn", None),
            getattr(self, "download_btn", None),
            getattr(self, "log_text", None),
        ]
        for idx, widget in enumerate(widgets):
            self._fade_widget(widget, delay_ms=idx * 45, duration_ms=300)

    def _set_single_preview(self, payload: dict):
        self.analyze_controller.set_single_preview(payload)

    def _on_thumbnail_loaded(self, reply: QNetworkReply):
        self.analyze_controller.on_single_preview_thumbnail_loaded(reply)

    def _add_current_to_queue(self):
        task = self._build_task()
        if not self._validate_task(task):
            return
        
        schedule_settings = {}
        if hasattr(self.search_view, "schedule_picker"):
            schedule_settings = self.search_view.schedule_picker.get_schedule_settings()
        scheduled_at = float(schedule_settings.get("scheduled_at", 0) or 0)
        if scheduled_at > datetime.now().timestamp():
            task["scheduled_at"] = scheduled_at
            task["schedule_repeat"] = str(schedule_settings.get("schedule_repeat", "none") or "none")
            task["status"] = "pending"
            scheduled_dt = datetime.fromtimestamp(scheduled_at)
            self._append_log(_("تم جدولة التحميل لبدء في: {when}").format(when=scheduled_dt.strftime("%Y-%m-%d %H:%M:%S")))
        else:
            task["scheduled_at"] = 0
            self._append_log(_("تمت إضافة عنصر للطابور (بدأ فوراً)"))

        idx = self.queue_manager.add_task(task)
        if idx == -1:
            self._warn(_("تعذر إضافة العنصر: الطابور ممتلئ"))
            return
        self._info(_("تمت إضافة العنصر للطابور"))
        if task.get("scheduled_at", 0):
            QTimer.singleShot(0, lambda: self._update_scheduler_timer_state(force_refresh=True))
            self._switch_view("downloads")
            self._set_downloads_filter("scheduled")
        elif not self._queue_is_running():
            self._start_queue_download()

    def _start_single_download(self):
        if self._single_download_locked:
            self._warn(_("تحميل مفرد قيد البدء بالفعل"))
            return
        self._set_single_download_lock(True)
        task = self._build_task()
        if not self._validate_task(task):
            self._set_single_download_lock(False)
            return
        task["scheduled_at"] = 0
        task["schedule_repeat"] = "none"
        idx = self.queue_manager.add_task(task)
        if idx == -1:
            self._warn(_("تعذر بدء التحميل: الطابور ممتلئ"))
            self._set_single_download_lock(False)
            return
        self.queue_manager.set_task_status(idx, "running")
        queue_task = self.queue_manager.get_task(idx)
        if not queue_task:
            self._warn(_("تعذر بدء التحميل: لم يتم العثور على العنصر في الطابور"))
            self._set_single_download_lock(False)
            return
        self._set_queue_runtime_state(running=False, paused=False)
        self.current_queue_index = -1
        started = self._start_download_worker(queue_task, worker_id=idx)
        if not started:
            self._set_single_download_lock(False)
            return
        self._update_scheduler_timer_state(force_refresh=True)
        self._set_single_download_lock(True, worker_id=idx)
        self._switch_view("downloads")
        self._set_downloads_filter("active")

    def _start_queue_download(self):
        self.download_controller.start_queue_download()
        QTimer.singleShot(0, lambda: self._update_scheduler_timer_state(force_refresh=True))

    def _pause_queue_download(self):
        self.download_controller.pause_queue_download()
        QTimer.singleShot(0, self._update_scheduler_timer_state)

    def _resume_queue_download(self):
        self.download_controller.resume_queue_download()
        QTimer.singleShot(0, lambda: self._update_scheduler_timer_state(force_refresh=True))

    def _pause_queue_item(self, item_index: int):
        self.download_controller.pause_queue_item(item_index)

    def _resume_queue_item(self, item_index: int):
        self.download_controller.resume_queue_item(item_index)

    def _set_queue_item_bandwidth_limit(self, item_index: int, limit_kbps: int):
        self.download_controller.set_queue_item_bandwidth_limit(item_index, limit_kbps)

    def _cancel_active_display_item(self):
        wid = self._display_progress_wid
        if not isinstance(wid, int):
            self._warn("لا يوجد عنصر نشط لإلغائه حالياً")
            return
        self._cancel_queue_item(wid)

    def _on_queue_reorder_requested(self, from_row: int, to_row: int):
        if self.downloads_filter not in {"active", "queued", "scheduled"}:
            return
        src = self.downloads_view.downloads_model.get_item(int(from_row))
        if not isinstance(src, dict):
            return
        src_index = src.get("queue_index")
        if not isinstance(src_index, int):
            return
        if to_row is None:
            return
        to_row = int(to_row)
        if to_row < 0:
            to_index = self.queue_manager.get_item_count() - 1
        else:
            dst = self.downloads_view.downloads_model.get_item(to_row)
            dst_index = dst.get("queue_index") if isinstance(dst, dict) else None
            if not isinstance(dst_index, int):
                return
            to_index = dst_index
        if to_index < 0:
            return
        moved = self.queue_manager.move_task(src_index, to_index)
        if not moved:
            return
        self._save_session()
        self._refresh_downloads_list()

    def _cancel_queue_item(self, item_index: int):
        self.download_controller.cancel_queue_item(item_index)

    def _delete_queue_item(self, item_index: int):
        self.download_controller.delete_queue_item(item_index)

    def _open_trim_for_queue_item(self, item_index: int):
        self.analyze_controller.open_trim_for_queue_item(item_index)

    def _on_trim_view_saved(self, data: dict):
        self.analyze_controller.on_trim_view_saved(data)

    def _on_trim_view_back(self):
        self.analyze_controller.on_trim_view_back()

    def _retry_queue_item(self, item_index: int):
        self.download_controller.retry_queue_item(item_index)

    def _locate_queue_item_file(self, item_index: int):
        self.download_controller.locate_queue_item_file(item_index)

    def _relocate_queue_item_file(self, item_index: int):
        self.download_controller.relocate_queue_item_file(item_index)

    def _redownload_queue_item(self, item_index: int):
        self.download_controller.redownload_queue_item(item_index)

    def _process_parallel_queue(self):
        self.download_controller.process_parallel_queue()

    def _start_download_worker(self, task, worker_id=None):
        started = self.download_controller.start_download_worker(task, worker_id=worker_id)
        if started:
            self._update_scheduler_timer_state(force_refresh=True)
        return started

    def _start_download_from_queue(self, task: dict, queue_index: int):
        self.download_controller.start_download_from_queue(task, queue_index)
        QTimer.singleShot(0, lambda: self._update_scheduler_timer_state(force_refresh=True))

    def _on_worker_thread_finished(self, wid, worker):
        self.download_controller.on_worker_thread_finished(wid, worker)
        QTimer.singleShot(0, self._update_scheduler_timer_state)


    def _on_download_progress(self, wid, percent, speed, eta):
        self.download_controller.on_download_progress(wid, percent, speed, eta)

    def _extract_size_text(self, line: str):
        return self.download_controller.extract_size_text(line)

    def _on_download_log(self, line: str):
        self.download_controller.on_download_log(line)

    def _on_download_state(self, state: str):
        self.download_controller.on_download_state(state)

    def _append_history(self, success: bool, message: str, payload: dict, task: dict):
        self.download_controller.append_history(success, message, payload, task)

    def _on_download_finished_event(self, event: DownloadFinishedEvent):
        self.download_controller.on_download_finished_event(event)
        QTimer.singleShot(0, self._update_scheduler_timer_state)

    def _handle_download_finished_event(self, event: DownloadFinishedEvent):
        self.download_controller.handle_download_finished_event(event)
        self._release_single_download_lock(getattr(event, "worker_id", None))

    def _cancel_active(self):
        if self.current_worker is not None:
            self.current_worker.stop()
        self._set_queue_runtime_state(running=False, paused=False)
        self._append_log(_("تم إرسال طلب الإلغاء"))

    def _set_controls_enabled(self, enabled: bool):
        self.search_view.search_btn.setEnabled(enabled)
        self.download_btn.setEnabled(bool(enabled) and not self._single_download_locked)


    def _set_single_download_lock(self, locked: bool, worker_id=None):
        self._single_download_locked = bool(locked)
        if locked:
            self._single_download_worker_id = worker_id
        else:
            self._single_download_worker_id = None
        sv = getattr(self, "search_view", None)
        if sv is not None and hasattr(sv, "set_download_button"):
            if locked:
                sv.set_download_button(text=_("Starting..."), enabled=False)
            else:
                sv.set_download_button(text=_("Download"), enabled=True)
        elif hasattr(self, "download_btn"):
            self.download_btn.setEnabled(not locked)

    def _release_single_download_lock(self, worker_id=None):
        tracked = self._single_download_worker_id
        if worker_id is not None and tracked is not None and worker_id != tracked:
            return
        self._set_single_download_lock(False)

    def _on_queue_limit_exceeded(self, rejected_count: int):
        """H-07: Warn user when queue is full and items were rejected."""
        max_size = self.queue_manager.MAX_QUEUE_SIZE
        self._warn(f"\u26a0\ufe0f \u062a\u0645 \u0631\u0641\u0636 {rejected_count} \u0639\u0646\u0635\u0631 \u2014 \u0627\u0644\u0637\u0627\u0628\u0648\u0631 \u0648\u0635\u0644 \u0644\u0644\u062d\u062f \u0627\u0644\u0623\u0642\u0635\u0649 ({max_size} \u0639\u0646\u0635\u0631)")

    def _schedule_settings_autosave(self, *_):
        self.settings_controller.schedule_settings_autosave()

    def _apply_settings_to_search(self, silent: bool = False):
        self.settings_controller.apply_settings_to_search(silent=silent)


    # ── Mini Mode ───────────────────────────────────────────────────────────
    def _toggle_mini_mode(self):
        self.ui_controller.toggle_mini_mode()

    def _show_from_mini(self):
        self.ui_controller.show_from_mini()

    # ── Global Hotkey Handlers ─────────────────────────────────────────────
    def _paste_and_analyze(self):
        """Hotkey: Ctrl+Shift+V — paste from clipboard and analyze."""
        try:
            clip = QApplication.clipboard()
            url = (clip.text() or "").strip()
            if not url.startswith("http"):
                self._warn(_("الرابط في الحافظة غير صالح أو غير مسموح"))
                return
            self.search_view.url_input.setText(url)
            self._start_analyze()
            self.showNormal()
            try:
                self.raise_()
            except Exception:
                pass
            self.activateWindow()
        except (RuntimeError, AttributeError, TypeError) as exc:
            logger.warning(f"فشل تنفيذ اللصق والتحليل من الاختصار: {exc}")

    def _hotkey_quick_download(self):
        """Hotkey: Ctrl+Shift+D — quick download whatever is in the clipboard."""
        try:
            clip = QApplication.clipboard()
            url = (clip.text() or "").strip()
            if url.startswith("http"):
                self._quick_download(url)
            else:
                self._warn(_("الرابط في الحافظة غير صالح أو غير مسموح"))
        except (RuntimeError, AttributeError, TypeError) as exc:
            logger.warning(f"فشل تنفيذ التحميل السريع من الاختصار: {exc}")

    # ── Audio Normalizer ────────────────────────────────────────────────
    def _normalize_downloads_folder(self):
        self.media_tools_controller.normalize_downloads_folder()

    def _update_ytdlp(self):
        self.update_controller.update_ytdlp_manual()

    def _check_app_updates_manual(self):
        self.update_controller.check_updates_manual()

    def _import_settings_from_file(self):
        self.settings_controller.import_settings()

    # ── Proxy Helpers ────────────────────────────────────────────────────────
    def _add_proxy(self):
        self.settings_controller.add_proxy()

    def _test_proxy(self):
        self.settings_controller.test_proxy()

    # ── Sustainability ────────────────────────────────────────────────────────

    def _normalize_history_mode(self, mode_value: str) -> str:
        return self.history_data_controller.normalize_history_mode(mode_value)

    def _fetch_db_history(self, status: str = None, limit: int = 2500, offset: int = 0) -> list[DownloadHistoryEntry]:
        return self.history_data_controller.fetch_db_history(status=status, limit=limit, offset=offset)

    def _load_stats(self):
        self.history_data_controller.load_stats()

    def _save_stats(self):
        # Legacy callers still invoke _save_stats; persist via unified session pipeline.
        self._save_session()







    def _save_search_history_only(self):
        self.settings_controller.save_settings_only()






    def _set_status(self, value: str):
        self.ui_controller.set_status(value)

    def _set_downloads_filter(self, key: str):
        self.download_controller.set_downloads_filter(key)

    def _set_downloads_sort(self, value: str):
        self.download_controller.set_downloads_sort(value)

    def _set_queue_state_filter(self, value: str):
        self.download_controller.set_queue_state_filter(value)

    def _set_downloads_page(self, value: str):
        self.download_controller.set_downloads_page(value)

    def _update_downloads_dashboard(self):
        self.download_controller.update_downloads_dashboard()

    def _retry_failed_items(self):
        self.download_controller.retry_failed_items()

    def _clear_completed_history(self):
        self.history_data_controller.clear_completed_history()

    def _export_history_csv(self):
        self.history_data_controller.export_history_csv()

    def _export_history_txt(self):
        self.history_data_controller.export_history_txt()

    def _export_queue_to_file(self):
        self.queue_transfer_controller.export_queue_to_file()

    def _choose_queue_export_mode(self) -> str | None:
        return self.queue_transfer_controller.choose_queue_export_mode()

    def _import_queue_from_file(self):
        self.queue_transfer_controller.import_queue_from_file()

    def _retry_all_failed_queue_items(self):
        self.queue_optimization_controller.retry_all_failed_queue_items()

    def _auto_optimize_queue(self, silent: bool = False):
        self.queue_optimization_controller.auto_optimize_queue(silent=silent)

    def _on_auto_optimize_queue_failed(self, request_id: int, exc: Exception, silent: bool):
        self.queue_optimization_controller.on_auto_optimize_queue_failed(request_id, exc, silent)

    def _apply_auto_optimized_queue(self, request_id: int, source_snapshot: list[dict], optimized: list[dict], silent: bool):
        self.queue_optimization_controller.apply_auto_optimized_queue(request_id, source_snapshot, optimized, silent)

    def _open_downloads_folder(self):
        self.file_manager_controller.open_downloads_folder()

    def _open_folder(self, file_path: str):
        self.file_manager_controller.open_folder(file_path)

    def _open_queue_item_folder(self, item_index: int):
        self.file_manager_controller.open_queue_item_folder(item_index)

    def _open_path_in_file_manager(self, folder: str):
        self.file_manager_controller.open_path_in_file_manager(folder)

    def _load_thumbnail(self, url: str, width: int = 132, height: int = 74):
        return self.thumbnail_controller.load_thumbnail(url, width=width, height=height)

    def _queue_download_thumbnail(self, model_index, item: dict, label: QLabel, width: int, height: int):
        self.thumbnail_controller.queue_download_thumbnail(model_index, item, label, width, height)

    def _schedule_visible_thumbnail_load(self, delay_ms: int = 0):
        self.thumbnail_controller.schedule_visible_thumbnail_load(delay_ms=delay_ms)

    def _process_visible_thumbnail_jobs(self):
        self.thumbnail_controller.process_visible_thumbnail_jobs()

    def _set_thumb_placeholder(self, label: QLabel):
        self.thumbnail_controller.set_thumb_placeholder(label)

    def _set_download_card_thumbnail(self, item: dict, label: QLabel, width: int, height: int):
        self.thumbnail_controller.set_download_card_thumbnail(item, label, width, height)

    def _on_download_thumbnail_loaded(self, reply: QNetworkReply, thumb_url: str, width: int, height: int):
        self.thumbnail_controller.on_download_thumbnail_loaded(reply, thumb_url, width, height)

    def _process_pending_thumbnails(self):
        self.thumbnail_controller.process_pending_thumbnails()

    def _cleanup_stale_thumbnail_waiters(self):
        self.thumbnail_controller.cleanup_stale_thumbnail_waiters()

    def _refresh_downloads_list(self):
        self.downloads_list_controller.refresh_downloads_list()

    def _clear_rendered_download_widgets(self, drop_cache: bool = False):
        self.downloads_list_controller.clear_rendered_download_widgets(drop_cache=drop_cache)

    def _drop_rendered_download_rows(self, row_indices):
        self.downloads_list_controller.drop_rendered_download_rows(row_indices)

    def _visible_download_rows_window(self, buffer_rows: int = 8) -> tuple[int, int]:
        return self.downloads_render_controller.visible_download_rows_window(buffer_rows=buffer_rows)

    def _render_download_entries_batch(self, generation: int, batch_size: int = 16):
        self.downloads_render_controller.render_download_entries_batch(generation, batch_size=batch_size)

    def _on_downloads_list_scrolled(self, _value: int):
        self.downloads_render_controller.on_downloads_list_scrolled(_value)

    def _on_downloads_list_range_changed(self, _min_value: int, _max_value: int):
        self.downloads_render_controller.on_downloads_list_range_changed(_min_value, _max_value)

    def _maybe_render_more_download_entries(self, generation: int, force: bool = False):
        self.downloads_render_controller.maybe_render_more_download_entries(generation, force=force)

    def _add_error_card(self, row_index: int, error_msg: str):
        self.downloads_render_controller.add_error_card(row_index, error_msg)

    def _downloads_style_pack(self, t: dict) -> dict:
        return self.downloads_render_controller.downloads_style_pack(t)

    def _add_download_entry_card(self, item: dict, row_index: int):
        self.downloads_render_controller.add_download_entry_card(item, row_index)

    def _download_entry_cache_key(self, item: dict, row_index: int) -> str:
        return self.download_controller.download_entry_cache_key(item, row_index)

    def _download_entry_render_signature(self, item: dict):
        return self.download_controller.download_entry_render_signature(item)
    def _quick_download(self, url):
        self.search_view.url_input.setText(url)
        self._start_analyze()

    def _schedule_downloads_refresh(self, delay_ms: int = 220):
        self.download_controller.schedule_downloads_refresh(delay_ms)

    def _refresh_queue_list(self):
        self._update_tray_menu_stats()
        self.download_controller.refresh_queue_list()

    def _on_queue_progress_updated(self, index: int, progress: float, speed: str, eta: str):
        self.download_controller.on_queue_progress_updated(index, progress, speed, eta)

    def _is_allowed_bulk_url(self, value: str) -> bool:
        text = str(value or "").strip()
        return text.startswith(("http://", "https://"))

    def dragEnterEvent(self, event):
        mime = event.mimeData()
        if mime.hasUrls() or mime.hasText():
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        mime = event.mimeData()
        dropped_paths = []
        if mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile():
                    dropped_paths.append(url.toLocalFile())
                else:
                    text = url.toString().strip()
                    if self._is_allowed_bulk_url(text):
                        self.search_view.url_input.setText(text)
                        self._switch_view("search")
                        self._start_analyze()
                        event.acceptProposedAction()
                        return
        text = mime.text().strip() if mime.hasText() else ""
        if self._is_allowed_bulk_url(text):
            self.search_view.url_input.setText(text)
            self._switch_view("search")
            self._start_analyze()
            event.acceptProposedAction()
            return
        for path in dropped_paths:
            lowered = str(path or "").strip().lower()
            if lowered.endswith((".txt", ".json", ".csv", ".xlsx")):
                self.import_controller.bulk_import_worker(path)
                event.acceptProposedAction()
                return
        self._warn(_("تم تجاهل العنصر المسحوب لأنه ليس رابط وسائط صالحاً أو ملف استيراد مدعوماً"))
        event.ignore()

    def _on_show_notification_event(self, event: ShowNotificationEvent):
        self._show_toast(event.message, event.level, event.title)

    def _on_extension_link_event(self, event: ExtensionLinkReceivedEvent):
        payload = dict(event.payload or {})
        QTimer.singleShot(0, lambda p=payload: self.extension_controller.handle_extension_link(p))

    def _show_tray_message(self, title: str, message: str, icon=QSystemTrayIcon.MessageIcon.Information, timeout: int = 1800):
        self.tray_manager.show_message(title, message, icon=icon, timeout=timeout)

    def _update_tray_menu_stats(self):
        self.tray_manager.update_stats()

    def _notify_tray_progress(self):
        self.tray_manager.notify_progress()




    def _update_speed_history(self, wid, kbps):
        self.download_controller.update_speed_history(wid, kbps)

MainWindow = PremiumWindow


def main():
    setup_logging()
    return start_app(PremiumWindow)


if __name__ == "__main__":
    main()
